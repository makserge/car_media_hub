"""Trip upload API for the car app (docs/TRIPS_API.md): validation, handlers, stats and HA sensors.

The handlers are registered on the sync API's ``web.Application`` (same port, same bearer token,
see :func:`car_media_hub.sync.create_sync_app`). A trip is identified by its uuid, so uploading the
same trip again replaces it (idempotent). After an accepted upload or a deletion the Home Assistant
sensors are published again (:class:`TripPublisher`).
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from aiohttp import web

from .db import Database
from .ha import HAClient

log = logging.getLogger(__name__)

MAX_BATCH = 1000  # trips per POST /api/trips
MAX_BODY_BYTES = 5 * 1024 * 1024
MAX_SAMPLES = 20000  # samples per POST /api/trips/{id}/samples
MAX_SHAPE_CHARS = 500_000
DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
STATS_DAYS = 30
MAX_REASON_ITEMS = 100  # per-item reasons returned for one batch (the counts always cover everything)
SOURCE_DEFAULT = "car-app"

_POLYLINE = re.compile(r"[\x3f-\x7e]*\Z")  # precision-6 polyline characters are ASCII 63..126

# name -> (minimum, maximum, nullable) for the plain number fields
_RANGES: dict[str, tuple[float, float]] = {
    "start_lat": (-90.0, 90.0),
    "start_lon": (-180.0, 180.0),
    "end_lat": (-90.0, 90.0),
    "end_lon": (-180.0, 180.0),
    "distance_m": (0.0, 5_000_000.0),
    "energy_kwh": (-100.0, 1000.0),  # negative: net regeneration
    "soc_start": (0.0, 100.0),
    "soc_end": (0.0, 100.0),
    "avg_consumption_kwh_per_100km": (-200.0, 1000.0),
    "ext_temp_c": (-90.0, 70.0),
}
_REQUIRED = ("id", "started_at", "ended_at", "start_lat", "start_lon", "end_lat", "end_lon",
             "distance_m", "duration_s")
_OPTIONAL_NUMBERS = ("energy_kwh", "soc_start", "soc_end", "avg_consumption_kwh_per_100km", "ext_temp_c")
MAX_DURATION_S = 7 * 86400
MAX_TEXT = 64

ENTITY_PREFIX = "sensor.car_media_hub_"  # like the notification ids and events: car_media_hub_*


class Invalid(ValueError):
    """A field that does not fit the contract; the message is the per-item reason."""


# -- validation ----------------------------------------------------------------------------------


def _number(value: Any, name: str, lo: float, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Invalid(f"{name} must be a number")
    value = float(value)
    if not math.isfinite(value):
        raise Invalid(f"{name} must be finite")
    if not lo <= value <= hi:
        raise Invalid(f"{name} out of range ({lo:g}..{hi:g})")
    return value


def parse_time(value: Any, name: str) -> tuple[str, float]:
    """``(normalised UTC text, epoch seconds)`` of an ISO 8601 timestamp that states its zone."""
    if not isinstance(value, str) or not value:
        raise Invalid(f"{name} must be an ISO 8601 UTC timestamp")
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError:
        raise Invalid(f"{name} is not a valid ISO 8601 timestamp") from None
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise Invalid(f"{name} must state its time zone (UTC, e.g. 2026-01-02T03:04:05Z)")
    moment = moment.astimezone(timezone.utc)
    if not 2000 <= moment.year <= 2100:
        raise Invalid(f"{name} is out of range (years 2000..2100)")
    text = moment.isoformat(timespec="milliseconds" if moment.microsecond else "seconds")
    return text.replace("+00:00", "Z"), moment.timestamp()


def _text(value: Any, name: str, nullable: bool) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_TEXT:
        raise Invalid(f"{name} must be a non-empty string of at most {MAX_TEXT} characters")
    if any(ch < " " or ch == "\x7f" for ch in value):
        raise Invalid(f"{name} must not contain control characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise Invalid(f"{name} is not valid text") from None
    return value.strip()


def validate_trip(raw: Any) -> tuple[dict[str, Any], float, float]:
    """``(row, started_ts, ended_ts)`` for a trip object, or raises :class:`Invalid`."""
    if not isinstance(raw, dict):
        raise Invalid("trip must be a JSON object")
    for name in _REQUIRED:
        if raw.get(name) is None:
            raise Invalid(f"{name} is required")
    trip: dict[str, Any] = {}
    raw_id = raw["id"]
    try:
        if not isinstance(raw_id, str):
            raise ValueError
        trip["id"] = str(uuid.UUID(raw_id.strip()))
    except ValueError:
        raise Invalid("id must be a uuid string") from None
    trip["started_at"], started = parse_time(raw["started_at"], "started_at")
    trip["ended_at"], ended = parse_time(raw["ended_at"], "ended_at")
    if ended < started:
        raise Invalid("ended_at is before started_at")
    for name in ("start_lat", "start_lon", "end_lat", "end_lon", "distance_m"):
        trip[name] = _number(raw[name], name, *_RANGES[name])
    duration = raw["duration_s"]
    if isinstance(duration, float) and duration.is_integer():
        duration = int(duration)
    if isinstance(duration, bool) or not isinstance(duration, int):
        raise Invalid("duration_s must be a whole number of seconds")
    if not 0 <= duration <= MAX_DURATION_S:
        raise Invalid(f"duration_s out of range (0..{MAX_DURATION_S})")
    trip["duration_s"] = duration
    for name in _OPTIONAL_NUMBERS:
        value = raw.get(name)
        trip[name] = None if value is None else _number(value, name, *_RANGES[name])
    shape = raw.get("shape")
    if shape is None:
        trip["shape"] = None
    else:
        if not isinstance(shape, str) or len(shape) > MAX_SHAPE_CHARS or not _POLYLINE.match(shape):
            raise Invalid("shape must be a polyline (precision 6) string")
        trip["shape"] = shape or None
    source = raw.get("source")
    trip["source"] = SOURCE_DEFAULT if source is None else _text(source, "source", False)
    trip["vehicle"] = _text(raw.get("vehicle"), "vehicle", True)
    return trip, started, ended


def validate_sample(raw: Any) -> tuple[dict[str, Any], float]:
    """``(row, epoch seconds)`` for a sample object, or raises :class:`Invalid`."""
    if not isinstance(raw, dict):
        raise Invalid("sample must be a JSON object")
    if raw.get("t") is None:
        raise Invalid("t is required")
    text, ts = parse_time(raw["t"], "t")
    sample: dict[str, Any] = {"t": text}
    limits = {"soc": (0.0, 100.0), "power_kw": (-1000.0, 1000.0), "speed_kmh": (0.0, 500.0),
              "lat": (-90.0, 90.0), "lon": (-180.0, 180.0)}
    for name, (lo, hi) in limits.items():
        value = raw.get(name)
        sample[name] = None if value is None else _number(value, name, lo, hi)
    return sample, ts


# -- statistics ----------------------------------------------------------------------------------


def _summary(totals: dict[str, Any]) -> dict[str, Any]:
    energy_km = totals["energy_distance_m"] / 1000.0
    average = totals["energy_kwh"] / energy_km * 100.0 if energy_km > 0 else None
    return {
        "trips": totals["trips"],
        "km": round(totals["distance_m"] / 1000.0, 3),
        "kwh": round(totals["energy_kwh"], 3),
        "avg_kwh_per_100km": None if average is None else round(average, 2),
    }


def compute_stats(db: Database, now: float | None = None) -> dict[str, Any]:
    """Totals over all trips and over the last 30 days. The average is kWh over the km of the trips with kWh."""
    now = time.time() if now is None else now
    return {
        "total": _summary(db.trip_totals()),
        "last_30_days": _summary(db.trip_totals(since_ts=now - STATS_DAYS * 86400)),
    }


def month_start(now: float) -> float:
    moment = datetime.fromtimestamp(now, timezone.utc)
    return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()


# -- Home Assistant sensors ----------------------------------------------------------------------


class TripPublisher:
    """Publishes the trip sensors through :meth:`HAClient.set_state` (never raises)."""

    def __init__(self, db: Database, ha: HAClient, clock: Callable[[], float] = time.time) -> None:
        self._db = db
        self._ha = ha
        self._clock = clock
        self._tasks: set[asyncio.Task[None]] = set()

    def sensors(self) -> list[tuple[str, Any, dict[str, Any]]]:
        """``(entity_id, state, attributes)`` of every sensor, from the database."""
        now = self._clock()
        total = _summary(self._db.trip_totals())
        month = _summary(self._db.trip_totals(since_ts=month_start(now)))
        last = self._db.last_trip()
        consumption = distance = ended = None
        if last is not None:
            distance = round(last["distance_m"] / 1000.0, 2)
            ended = last["ended_at"]
            consumption = last["avg_consumption_kwh_per_100km"]
            if consumption is None and last["energy_kwh"] is not None and last["distance_m"] > 0:
                consumption = last["energy_kwh"] / (last["distance_m"] / 1000.0) * 100.0
            if consumption is not None:
                consumption = round(consumption, 2)
        unknown = "unknown"
        return [
            (ENTITY_PREFIX + "last_trip_distance", unknown if distance is None else distance,
             {"friendly_name": "Car Media and Trips last trip distance", "unit_of_measurement": "km",
              "device_class": "distance", "icon": "mdi:map-marker-distance"}),
            (ENTITY_PREFIX + "last_trip_consumption", unknown if consumption is None else consumption,
             {"friendly_name": "Car Media and Trips last trip consumption", "unit_of_measurement": "kWh/100km",
              "icon": "mdi:ev-station"}),
            (ENTITY_PREFIX + "last_trip_end", unknown if ended is None else ended,
             {"friendly_name": "Car Media and Trips last trip end", "device_class": "timestamp",
              "icon": "mdi:flag-checkered"}),
            (ENTITY_PREFIX + "total_distance", total["km"],
             {"friendly_name": "Car Media and Trips total distance", "unit_of_measurement": "km",
              "device_class": "distance", "state_class": "total", "trips": total["trips"],
              "icon": "mdi:road-variant"}),
            (ENTITY_PREFIX + "month_energy", month["kwh"],
             {"friendly_name": "Car Media and Trips month energy", "unit_of_measurement": "kWh",
              "device_class": "energy", "state_class": "total", "trips": month["trips"],
              "icon": "mdi:lightning-bolt"}),
            (ENTITY_PREFIX + "average_consumption",
             unknown if total["avg_kwh_per_100km"] is None else total["avg_kwh_per_100km"],
             {"friendly_name": "Car Media and Trips average consumption", "unit_of_measurement": "kWh/100km",
              "icon": "mdi:gauge"}),
        ]

    async def publish(self) -> None:
        try:
            states = self.sensors()
        except Exception:
            log.exception("Trip sensors: reading the database failed")
            return
        for entity_id, state, attributes in states:
            try:
                await self._ha.set_state(entity_id, state, attributes)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Trip sensors: publishing %s failed", entity_id)

    def publish_soon(self) -> None:
        """Publish in the background (an upload does not wait for Home Assistant)."""
        try:
            task = asyncio.get_running_loop().create_task(self.publish())
        except RuntimeError:
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Wait for the background publications started so far."""
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)


# -- HTTP ----------------------------------------------------------------------------------------

_DB = web.AppKey("trips_db", object)
_PUBLISHER = web.AppKey("trips_publisher", object)
_CLOCK = web.AppKey("trips_clock", object)


def _json(status: int, payload: Any) -> web.Response:
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    resp = web.Response(status=status, body=body, content_type="application/json", charset="utf-8")
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _error(status: int, code: str, detail: str | None = None) -> web.Response:
    payload: dict[str, Any] = {"error": code}
    if detail:
        payload["detail"] = detail
    return _json(status, payload)


async def _body_json(request: web.Request) -> Any:
    """The decoded JSON body, or an error response (413 is raised by aiohttp's ``client_max_size``)."""
    raw = await request.read()
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return _error(400, "invalid_json")


def _db(request: web.Request) -> Database:
    return request.app[_DB]  # type: ignore[no-any-return]


def _changed(request: web.Request) -> None:
    publisher = request.app.get(_PUBLISHER)
    if publisher is not None:
        publisher.publish_soon()


async def _post_trips(request: web.Request) -> web.StreamResponse:
    data = await _body_json(request)
    if isinstance(data, web.Response):
        return data
    if not isinstance(data, list):
        return _error(400, "invalid_body", "the body must be a JSON list of trips")
    if len(data) > MAX_BATCH:
        return _error(413, "too_many_items", f"at most {MAX_BATCH} trips per request")
    db = _db(request)
    inserted = updated = 0
    errors: list[dict[str, Any]] = []
    rejected = 0
    seen: set[str] = set()
    for index, item in enumerate(data):
        try:
            trip, started, ended = validate_trip(item)
            if trip["id"] in seen:
                raise Invalid("id appears twice in this request")
            seen.add(trip["id"])
            is_new = db.upsert_trip(trip, started, ended)
        except Invalid as exc:
            rejected += 1
            if len(errors) < MAX_REASON_ITEMS:
                tid = item.get("id") if isinstance(item, dict) and isinstance(item.get("id"), str) else None
                errors.append({"index": index, "id": tid[:64] if tid else None, "reason": str(exc)})
            continue
        if is_new:
            inserted += 1
        else:
            updated += 1
    if inserted or updated:
        _changed(request)
    return _json(200, {"inserted": inserted, "updated": updated, "rejected": rejected, "errors": errors})


def _int_param(request: web.Request, name: str, default: int, lo: int, hi: int) -> int:
    text = request.query.get(name)
    if text is None or text == "":
        return default
    try:
        value = int(text)
    except ValueError:
        raise Invalid(f"{name} must be a whole number") from None
    if not lo <= value <= hi:
        raise Invalid(f"{name} must be between {lo} and {hi}")
    return value


async def _list_trips(request: web.Request) -> web.StreamResponse:
    try:
        limit = _int_param(request, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
        since = request.query.get("since")
        before = request.query.get("before")
        since_ts = parse_time(since, "since")[1] if since else None
        before_ts = parse_time(before, "before")[1] if before else None
    except Invalid as exc:
        return _error(400, "invalid_parameter", str(exc))
    with_shape = request.query.get("shape", "1") not in ("0", "false")
    trips = _db(request).list_trips(since_ts, before_ts, limit, with_shape)
    payload: dict[str, Any] = {"trips": trips, "count": len(trips)}
    # newest first: pass next_before as ``before`` to get the next older page
    payload["next_before"] = trips[-1]["started_at"] if len(trips) == limit else None
    return _json(200, payload)


def _valid_id(request: web.Request) -> str | None:
    try:
        return str(uuid.UUID(request.match_info["id"]))
    except ValueError:
        return None


async def _get_trip(request: web.Request) -> web.StreamResponse:
    tid = _valid_id(request)
    trip = _db(request).get_trip(tid) if tid else None
    if trip is None:
        return _error(404, "not_found")
    return _json(200, trip)


async def _delete_trip(request: web.Request) -> web.StreamResponse:
    tid = _valid_id(request)
    if tid is None or not _db(request).delete_trip(tid):
        return _error(404, "not_found")
    _changed(request)
    return _json(200, {"deleted": tid})


async def _post_samples(request: web.Request) -> web.StreamResponse:
    tid = _valid_id(request)
    db = _db(request)
    if tid is None or db.get_trip(tid) is None:
        return _error(404, "not_found")
    data = await _body_json(request)
    if isinstance(data, web.Response):
        return data
    if not isinstance(data, list):
        return _error(400, "invalid_body", "the body must be a JSON list of samples")
    if len(data) > MAX_SAMPLES:
        return _error(413, "too_many_items", f"at most {MAX_SAMPLES} samples per request")
    rows: list[tuple[dict[str, Any], float]] = []
    errors: list[dict[str, Any]] = []
    rejected = 0
    for index, item in enumerate(data):
        try:
            if isinstance(item, dict) and item.get("trip_id") not in (None, tid):
                if str(item["trip_id"]).strip().lower() != tid:
                    raise Invalid("trip_id does not match the trip in the URL")
            rows.append(validate_sample(item))
        except Invalid as exc:
            rejected += 1
            if len(errors) < MAX_REASON_ITEMS:
                errors.append({"index": index, "reason": str(exc)})
    stored = db.add_trip_samples(tid, rows) if rows else 0
    return _json(200, {"stored": stored, "rejected": rejected, "errors": errors})


async def _get_samples(request: web.Request) -> web.StreamResponse:
    tid = _valid_id(request)
    db = _db(request)
    if tid is None or db.get_trip(tid) is None:
        return _error(404, "not_found")
    samples = db.list_trip_samples(tid)
    return _json(200, {"trip_id": tid, "samples": samples, "count": len(samples)})


async def _stats(request: web.Request) -> web.StreamResponse:
    clock = request.app[_CLOCK]
    return _json(200, compute_stats(_db(request), clock()))


def is_trips_path(path: str) -> bool:
    """Whether ``path`` belongs to the trip API (it accepts more methods than the file listing)."""
    return path == "/api/stats" or path == "/api/trips" or path.startswith("/api/trips/")


def register_trip_routes(
    app: web.Application, db: Database, publisher: TripPublisher | None = None,
    clock: Callable[[], float] = time.time,
) -> None:
    """Add the trip endpoints to the sync application."""
    app[_DB] = db
    if publisher is not None:
        app[_PUBLISHER] = publisher
    app[_CLOCK] = clock
    r = app.router
    r.add_post("/api/trips", _post_trips)
    r.add_get("/api/trips", _list_trips)
    r.add_get("/api/trips/{id}", _get_trip)
    r.add_delete("/api/trips/{id}", _delete_trip)
    r.add_post("/api/trips/{id}/samples", _post_samples)
    r.add_get("/api/trips/{id}/samples", _get_samples)
    r.add_get("/api/stats", _stats)
