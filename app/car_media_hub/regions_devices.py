"""Car app devices that report their region state back to the add-on (pull model: the add-on never contacts the app).

``POST /api/regions/devices/report`` stores one JSON file per device below ``<region_storage_dir>/.devices/``
(``<deviceId>.json``, atomic writes, at most :data:`MAX_DEVICES` devices, entries not seen for 90 days are dropped).
Everything in a report is validated and cleaned; sizes and counts are never trusted.

A car that sleeps in the middle of an install is not a failed install: the store keeps the largest byte count a
device reported for a region (so a later ``queued`` or network ``failed`` report still shows how far it got), and the
public view turns a ``downloading`` report that went quiet, or a ``failed`` report with a network-type message from an
app that made progress, into the phase ``paused`` ("last seen N min ago, X of Y"). Silence never removes a request:
:data:`STALE_SECONDS` only makes the add-on forget a *device*.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

DEVICE_ID_RE = re.compile(r"[A-Za-z0-9_-]{8,64}")
DEVICE_FILE_RE = re.compile(r"([A-Za-z0-9_-]{8,64})\.json")
# "paused" and "cancelled" are accepted from newer apps ("interrupted" is read as "paused"); the current app never sends them
STATES = ("installed", "downloading", "queued", "failed", "removed", "absent", "paused", "cancelled")
STATE_ALIASES = {"interrupted": "paused"}
PAUSED_AFTER_SECONDS = 180  # a downloading report older than this means the app is not running (car asleep)
NETWORK_WORDS = ("connection", "network", "timeout", "timed out", "wi-fi", "wifi", "offline", "unreachable", "interrupted",
                 "http 5", "http 429", "reset", "broken pipe", "no route")
MAX_DEVICES = 20
MAX_REGIONS_PER_DEVICE = 100
STALE_SECONDS = 90 * 24 * 3600
MAX_NAME = 60
MAX_VERSION = 40
MAX_MESSAGE = 200
MAX_BYTES_VALUE = 1 << 50
MAX_RATE = 1e9  # bytes per second an app may report
RATE_TAU_SECONDS = 60.0  # time constant of the exponential average of the rate derived from consecutive reports
RATE_KEEP_SECONDS = 600  # a rate older than this is forgotten
MIN_ETA_RATE = 50 * 1024  # below this the estimate is meaningless ("measuring...")


class ReportError(ValueError):
    """A report the add-on refuses; ``status`` is the HTTP status of the answer."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean_text(value: Any, name: str, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ReportError(f"{name} must be text")
    return "".join(ch for ch in value if ch >= " " and ch != "\x7f").strip()[:limit]


def _clean_int(value: Any, name: str, maximum: int) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReportError(f"{name} must be a whole number")
    return max(0, min(value, maximum))


def _clean_rate(value: Any) -> float | None:
    """The optional ``bytesPerSecond`` of a report region: None when missing, else a number from 0 to 1e9."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= MAX_RATE:
        raise ReportError("bytesPerSecond must be a number from 0 to 1e9")
    return float(value)


def round_eta_minutes(seconds: float) -> int:
    """A remaining time rounded for people: whole minutes (at least 1) under 2 hours, else the nearest 5 minutes."""
    minutes = max(1, int(round(seconds / 60.0)))
    return minutes if seconds < 2 * 3600 else int(round(seconds / 300.0)) * 5


def _clean_progress(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ReportError("progress must be a number from 0 to 100")
    return float(max(0.0, min(100.0, value)))


def _state(value: Any) -> str | None:
    """The stored state for a reported one (aliases folded), None when unknown."""
    if isinstance(value, str):
        value = STATE_ALIASES.get(value, value)
        if value in STATES:
            return value
    return None


def is_network_message(message: str) -> bool:
    """True for the failure texts of a lost or unavailable connection ("Connection lost", "Server answered HTTP 503"), not
    for verdicts about the file or the device (hash, size, space, storage, HTTP 4xx)."""
    text = message.lower()
    return any(word in text for word in NETWORK_WORDS)


def clean_report(data: Any, now: float, valid_region_id: Callable[[object], bool]) -> dict[str, Any]:
    """The stored form of a device report or raises :class:`ReportError`."""
    if not isinstance(data, dict):
        raise ReportError("the body must be a JSON object")
    device_id = data.get("deviceId")
    if not isinstance(device_id, str) or DEVICE_ID_RE.fullmatch(device_id) is None:
        raise ReportError("deviceId must match [A-Za-z0-9_-]{8,64}")
    raw_regions = data.get("regions", [])
    if not isinstance(raw_regions, list):
        raise ReportError("regions must be a list")
    if len(raw_regions) > MAX_REGIONS_PER_DEVICE:
        raise ReportError(f"at most {MAX_REGIONS_PER_DEVICE} regions per report")
    regions: dict[str, dict[str, Any]] = {}
    for item in raw_regions:
        if not isinstance(item, dict):
            raise ReportError("every region entry must be an object")
        rid = item.get("id")
        if not valid_region_id(rid):
            raise ReportError("region id is not valid")
        state = _state(item.get("state"))
        if state is None:
            raise ReportError("state must be one of " + ", ".join(STATES))
        regions[rid] = {
            "state": state,
            "version": _clean_text(item.get("version"), "version", MAX_VERSION),
            "progress": _clean_progress(item.get("progress")),
            "message": _clean_text(item.get("message"), "message", MAX_MESSAGE),
            "bytesDone": _clean_int(item.get("bytesDone"), "bytesDone", MAX_BYTES_VALUE),
            "bytesTotal": _clean_int(item.get("bytesTotal"), "bytesTotal", MAX_BYTES_VALUE),
            "updatedAt": _iso(now), "updatedTs": now,
        }
        regions[rid]["bestBytes"], regions[rid]["bestTotal"] = regions[rid]["bytesDone"], regions[rid]["bytesTotal"]
        regions[rid]["reportedRate"] = _clean_rate(item.get("bytesPerSecond"))
        regions[rid]["rate"], regions[rid]["rateTs"] = None, 0.0  # derived by the store from the previous report
    return {
        "version": 1, "deviceId": device_id,
        "name": _clean_text(data.get("name"), "name", MAX_NAME),
        "appVersion": _clean_text(data.get("appVersion"), "appVersion", MAX_VERSION),
        "lastSeen": _iso(now), "lastSeenTs": now, "regions": regions,
    }


def _num(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return 0.0
    return float(value)


def _stored_rate(value: Any) -> float | None:
    number = _num(value) if value is not None else None
    return number if number is not None and 0 <= number <= MAX_RATE else None


def _reload(data: Any, valid_region_id: Callable[[object], bool]) -> dict[str, Any] | None:
    """A stored device document checked again (the file may have been edited or damaged); None when unusable."""
    if not isinstance(data, dict):
        return None
    device_id = data.get("deviceId")
    if not isinstance(device_id, str) or DEVICE_ID_RE.fullmatch(device_id) is None:
        return None
    ts = _num(data.get("lastSeenTs"))
    if ts <= 0:
        return None
    regions: dict[str, dict[str, Any]] = {}
    raw = data.get("regions")
    for rid, item in (raw.items() if isinstance(raw, dict) else ()):
        state = _state(item.get("state")) if isinstance(item, dict) else None
        if not valid_region_id(rid) or state is None or len(regions) >= MAX_REGIONS_PER_DEVICE:
            continue
        try:
            regions[rid] = {
                "state": state, "version": _clean_text(item.get("version"), "v", MAX_VERSION),
                "progress": _clean_progress(item.get("progress")), "message": _clean_text(item.get("message"), "m", MAX_MESSAGE),
                "bytesDone": _clean_int(item.get("bytesDone"), "b", MAX_BYTES_VALUE),
                "bytesTotal": _clean_int(item.get("bytesTotal"), "b", MAX_BYTES_VALUE),
                "updatedAt": _clean_text(item.get("updatedAt"), "u", 40), "updatedTs": _num(item.get("updatedTs")),
                "bestBytes": _clean_int(item.get("bestBytes", item.get("bytesDone")), "b", MAX_BYTES_VALUE),
                "bestTotal": _clean_int(item.get("bestTotal", item.get("bytesTotal")), "b", MAX_BYTES_VALUE),
                "reportedRate": _stored_rate(item.get("reportedRate")), "rate": _stored_rate(item.get("rate")),
                "rateTs": _num(item.get("rateTs")),
            }
        except ReportError:
            continue
    try:
        return {
            "version": 1, "deviceId": device_id, "name": _clean_text(data.get("name"), "name", MAX_NAME),
            "appVersion": _clean_text(data.get("appVersion"), "appVersion", MAX_VERSION),
            "lastSeen": _clean_text(data.get("lastSeen"), "lastSeen", 40), "lastSeenTs": ts, "regions": regions,
        }
    except ReportError:
        return None


def derive_rate(old: dict[str, Any] | None, entry: dict[str, Any]) -> None:
    """The download rate of a region from two consecutive reports of the same device (sets ``rate`` / ``rateTs``).

    The bytesDone delta over the report times is smoothed with an exponential average (:data:`RATE_TAU_SECONDS`). A reset
    (bytesDone went down: the download restarted from zero), a gap longer than :data:`PAUSED_AFTER_SECONDS`, a report that is not
    ``downloading`` or a different version leave the rate unknown until the next pair of reports.
    """
    entry["rate"], entry["rateTs"] = None, 0.0
    if old is None or entry["state"] != "downloading" or old["state"] != "downloading":
        return
    if old["version"] and entry["version"] and old["version"] != entry["version"]:
        return
    dt = entry["updatedTs"] - old["updatedTs"]
    delta = entry["bytesDone"] - old["bytesDone"]
    if not 0 < dt <= PAUSED_AFTER_SECONDS or delta < 0:
        return
    instant = delta / dt
    previous = old.get("rate")
    if previous is not None and entry["updatedTs"] - old.get("rateTs", 0.0) <= RATE_KEEP_SECONDS:
        alpha = 1.0 - math.exp(-dt / RATE_TAU_SECONDS)
        instant = previous + alpha * (instant - previous)
    entry["rate"], entry["rateTs"] = instant, entry["updatedTs"]


def carry_progress(previous: dict[str, Any] | None, doc: dict[str, Any]) -> None:
    """Keep how far a device got: a ``queued`` / ``failed`` report after a wake-up carries no byte counts, so the largest
    earlier count of the same version stays as ``bestBytes`` / ``bestTotal`` until the region is installed or gone."""
    if previous is None:
        return
    for rid, entry in doc["regions"].items():
        old = previous["regions"].get(rid)
        derive_rate(old, entry)
        if old is None or entry["state"] in ("installed", "removed", "absent"):
            continue
        if old["state"] in ("installed", "removed", "absent") or (old["version"] and entry["version"] and old["version"] != entry["version"]):
            continue
        if old["bestBytes"] > entry["bestBytes"]:
            entry["bestBytes"] = old["bestBytes"]
        if entry["bestTotal"] == 0:
            entry["bestTotal"] = old["bestTotal"]


def _left(region: dict[str, Any]) -> int | None:
    """Bytes still to download of a region (None when its size is unknown); a shared file is counted under one region only."""
    return region["leftBytes"]


def running_region(by_order: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The region that is really downloading: of the ``downloading`` ones not waiting for a shared file, the one that moves
    (highest rate, then most bytes)."""
    live = [r for r in by_order if r["phase"] == "downloading" and not r["waitingForShared"]]
    return max(live, key=lambda r: (r["bytesPerSecond"], r["doneBytes"])) if live else None


def apply_shared(by_order: list[dict[str, Any]], shared: dict[str, tuple[str, int]]) -> None:
    """Count the routing file of a routing group once per device (see :meth:`DeviceStore.public_list`).

    Within a group the *carrier* downloads the file: the region that is moving (most bytes done), else the first one in install
    order. The others get ``leftBytes`` without the routing size (their total includes it). When a region of the group is
    installed already, the file is on the device and nobody carries it. A region that has 0 bytes while the carrier is
    downloading is ``waitingForShared``.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in by_order:
        info = shared.get(r["id"])
        if info is not None and info[1] > 0:
            groups.setdefault(info[0], []).append(r)
    for group, members in groups.items():
        size = shared[members[0]["id"]][1]
        active = [r for r in members if r["phase"] in ("downloading", "queued", "paused")]
        if not active:
            continue
        installed = any(r["state"] == "installed" for r in members)
        moving = [r for r in active if r["phase"] in ("downloading", "paused") and r["doneBytes"] > 0]
        carrier = None if installed else (max(moving, key=lambda r: (r["bytesPerSecond"], r["doneBytes"])) if moving else active[0])
        done = size if installed else min(size, carrier["doneBytes"])
        for r in active:
            r["sharedRouting"] = {"group": group, "bytes": size, "doneBytes": done, "carrier": r is carrier}
            if r is carrier:
                continue
            if r["totalBytes"] >= size:
                r["leftBytes"] = max(0, r["totalBytes"] - max(r["doneBytes"], size))
            r["waitingForShared"] = (
                not installed and r["doneBytes"] == 0 and done < size and bool(moving) and r["phase"] == "downloading")


def _estimate(by_order: list[dict[str, Any]], queue: list[dict[str, Any]]) -> dict[str, Any]:
    """Remaining time per region and for the whole queue of one device (fills ``etaSeconds`` / ``etaMinutes`` /
    ``startsInSeconds`` in the region dicts). An estimate is given only while the device is actively downloading (fresh
    report) and the rate is known and at least :data:`MIN_ETA_RATE`; the queued regions use the rate of the running one.

    ``state``: ``eta`` (numbers given), ``measuring`` (downloading, rate unknown or too low), ``paused`` (nothing is moving),
    ``idle`` (nothing left to install).
    """
    active = [r for r in by_order if r["phase"] in ("downloading", "queued", "paused")]
    count = len(active)
    lefts = [_left(r) for r in active]
    known = all(x is not None for x in lefts)
    bytes_left = sum(x for x in lefts if x is not None)
    running = running_region(by_order)
    summary: dict[str, Any] = {"count": count, "bytesLeft": bytes_left, "bytesPerSecond": 0, "etaSeconds": None, "etaMinutes": None,
                               "state": "idle" if not count else "paused"}
    if running is None:
        return summary
    rate = running["bytesPerSecond"]
    summary["state"] = "measuring"
    if rate < MIN_ETA_RATE:
        return summary
    summary["bytesPerSecond"] = rate
    ahead = _left(running)
    if ahead is not None:
        running["etaSeconds"] = int(ahead / rate)
        running["etaMinutes"] = round_eta_minutes(running["etaSeconds"])
    ahead_known = ahead is not None
    ahead = ahead or 0
    for r in by_order:  # a region that waits for the shared file runs right after the one that carries it
        if r["phase"] == "downloading" and r["waitingForShared"]:
            left = _left(r)
            ahead_known = ahead_known and left is not None
            ahead += left or 0
    for r in queue:
        left = _left(r)
        if ahead_known:
            r["startsInSeconds"] = int(ahead / rate)
        if left is None:
            ahead_known = False
        else:
            ahead += left
    if known:
        summary.update(state="eta", etaSeconds=int(bytes_left / rate), etaMinutes=round_eta_minutes(bytes_left / rate))
    return summary


class DeviceStore:
    """The ``.devices`` folder. Writes are serialised by a lock and go through a temporary file + rename."""

    def __init__(self, directory: Path, clock: Callable[[], float]) -> None:
        self._dir = Path(directory)
        self._clock = clock
        self._lock = asyncio.Lock()

    # -- blocking helpers (run in a thread)
    def _entries(self) -> list[tuple[str, Path]]:
        try:
            scan = list(os.scandir(self._dir))
        except FileNotFoundError:
            return []
        out = []
        for entry in scan:
            match = DEVICE_FILE_RE.fullmatch(entry.name)
            if match is None:
                continue
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            out.append((match.group(1), Path(entry.path)))
        return sorted(out)

    def _load_blocking(self, prune: bool) -> list[dict[str, Any]]:
        from .regions import read_small_json, valid_region_id

        now = self._clock()
        good: list[dict[str, Any]] = []
        for device_id, path in self._entries():
            try:
                doc = _reload(read_small_json(path), valid_region_id)
            except (OSError, ValueError):
                doc = None
            if doc is None or doc["deviceId"] != device_id:
                continue
            if now - doc["lastSeenTs"] > STALE_SECONDS:
                if prune:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
                continue
            good.append(doc)
        good.sort(key=lambda d: (-d["lastSeenTs"], d["deviceId"]))
        return good

    def _store_blocking(self, doc: dict[str, Any]) -> None:
        from .regions import write_json_atomic

        self._dir.mkdir(parents=True, exist_ok=True)
        existing = self._load_blocking(prune=True)
        known = {d["deviceId"] for d in existing}
        if doc["deviceId"] not in known and len(known) >= MAX_DEVICES:
            raise ReportError(f"too many devices (at most {MAX_DEVICES}); a device is dropped after 90 days without a report", 429)
        carry_progress(next((d for d in existing if d["deviceId"] == doc["deviceId"]), None), doc)
        target = self._dir / f"{doc['deviceId']}.json"
        try:
            if target.exists() and not stat.S_ISREG(os.lstat(target).st_mode):
                raise ReportError("device slot is not a regular file", 409)
        except OSError:
            pass
        write_json_atomic(target, doc)

    def _forget_blocking(self, device_id: str | None) -> int:
        removed = 0
        for found_id, path in self._entries():
            if device_id is not None and found_id != device_id:
                continue
            try:
                os.unlink(path)
                removed += 1
            except OSError:
                pass
        return removed

    # -- public coroutines
    async def forget(self, device_id: str) -> bool:
        """Delete the stored report of one device; False when it is not known."""
        if not isinstance(device_id, str) or DEVICE_ID_RE.fullmatch(device_id) is None:
            raise ReportError("invalid device id", 400)
        async with self._lock:
            return await asyncio.to_thread(self._forget_blocking, device_id) > 0

    async def forget_all(self) -> int:
        """Delete every stored device report; returns how many."""
        async with self._lock:
            return await asyncio.to_thread(self._forget_blocking, None)

    async def report(self, data: Any) -> dict[str, Any]:
        from .regions import valid_region_id

        doc = clean_report(data, self._clock(), valid_region_id)
        async with self._lock:
            await asyncio.to_thread(self._store_blocking, doc)
        return doc

    async def load(self) -> list[dict[str, Any]]:
        """The stored devices (newest first, stale ones left out)."""
        async with self._lock:
            return await asyncio.to_thread(self._load_blocking, False)

    async def public_list(
        self, cancelled: dict[str, float] | None = None, order: dict[str, int] | None = None,
        shared: dict[str, tuple[str, int]] | None = None, versions: dict[str, str] | None = None,
        wanted: frozenset[str] | set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """The devices as JSON for the page and for tooling: regions as a list, ``ageSeconds`` since the last report.

        Per region ``state`` is what the app reported and ``phase`` what to show: ``paused`` for a download that went quiet
        (or a network failure after progress), ``cancelled`` for a region whose install the owner cancelled (``cancelled``
        maps region id -> time of the cancel; ``cancelAcknowledged`` is true once the device reported after it).
        ``doneBytes`` / ``totalBytes`` are the best known progress (also carried over a report without byte counts).
        The app installs one region at a time: per device ``running`` is the region that is downloading (or paused in the
        middle of it) and ``queue`` the queued ones as ``{id, position}`` in the order of the install requests (``order``:
        region id -> position in the add-on's install queue); a queued region also has ``queuePosition``.
        ``shared`` maps a region id to ``(routing group, size of the shared routing file)``: regions of one group use one
        routing archive, which the app downloads once under the region that runs first (the others report 0 bytes with the
        full routing size in their total). Such a file is counted once in the remaining bytes and the estimate, the regions
        that wait for it get ``waitingForShared`` and ``sharedRouting`` (``bytes``, ``doneBytes``) instead of looking stuck at 0 %.
        ``versions`` (region id -> catalogue version) and ``wanted`` (ids with an install request) tell what is done: ``current``
        is true for an installed region whose reported version is the catalogue's (or unknown), ``updatePending`` for a wanted
        one installed in an older version. ``queue`` / ``queuePosition`` count only the pending regions of the device (queued
        ones and updates), dense, in request order; an installed current region has no position.
        """
        now = self._clock()
        cancelled = cancelled or {}
        order = order or {}
        shared = shared or {}
        versions = versions or {}
        wanted = wanted or frozenset()
        out = []
        for d in await self.load():
            regions = []
            for rid, e in sorted(d["regions"].items()):
                done, total = max(e["bytesDone"], e["bestBytes"]), e["bytesTotal"] or e["bestTotal"]
                entry_age = max(0, int(now - (e["updatedTs"] or d["lastSeenTs"])))
                phase = e["state"]
                if rid in cancelled and e["state"] not in ("installed", "removed", "absent"):
                    phase = "cancelled"
                elif e["state"] == "paused":
                    phase = "paused"
                elif e["state"] == "downloading" and entry_age >= PAUSED_AFTER_SECONDS:
                    phase = "paused"
                elif e["state"] == "queued" and entry_age >= PAUSED_AFTER_SECONDS and done > 0:
                    phase = "paused"
                elif e["state"] == "failed" and done > 0 and is_network_message(e["message"]):
                    phase = "paused"  # the connection broke after progress: the install goes on, it did not fail
                rate, source = None, ""
                if phase == "downloading":
                    if e["reportedRate"]:
                        rate, source = e["reportedRate"], "app"
                    elif e["rate"] is not None and now - e["rateTs"] <= RATE_KEEP_SECONDS:
                        rate, source = e["rate"], "derived"
                current = e["state"] == "installed" and (not e["version"] or not versions.get(rid) or e["version"] == versions[rid])
                regions.append({
                    "current": current, "updatePending": e["state"] == "installed" and not current and rid in wanted,
                    "bytesPerSecond": round(rate) if rate else 0, "rateSource": source, "etaSeconds": None, "etaMinutes": None,
                    "startsInSeconds": None, "waitingForShared": False, "sharedRouting": None,
                    "leftBytes": max(0, total - done) if total > 0 else None,
                    "id": rid, "state": e["state"], "phase": phase, "queuePosition": 0, "version": e["version"], "progress": e["progress"],
                    "message": e["message"], "bytesDone": e["bytesDone"], "bytesTotal": e["bytesTotal"],
                    "doneBytes": done, "totalBytes": total, "ageSeconds": entry_age, "updatedAt": e["updatedAt"],
                    "cancelAcknowledged": rid in cancelled and (e["updatedTs"] or 0) >= cancelled[rid],
                })
            by_order = sorted(regions, key=lambda r: (order.get(r["id"], 1 << 30), r["id"]))
            apply_shared(by_order, shared)
            queue = [r for r in by_order if r["phase"] == "queued" or r["updatePending"]]
            for position, r in enumerate(queue, 1):
                r["queuePosition"] = position
            moving = running_region(by_order)
            running = moving["id"] if moving else next(
                (r["id"] for r in by_order if r["phase"] == "paused" and r["state"] in ("downloading", "paused")), "")
            summary = _estimate(by_order, queue)
            out.append({
                "deviceId": d["deviceId"], "name": d["name"], "appVersion": d["appVersion"], "lastSeen": d["lastSeen"],
                "ageSeconds": max(0, int(now - d["lastSeenTs"])), "regions": regions,
                "running": running, "queue": [{"id": r["id"], "position": r["queuePosition"]} for r in queue],
                "estimate": summary,
            })
        return out
