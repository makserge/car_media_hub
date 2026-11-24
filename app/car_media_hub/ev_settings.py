"""EV settings store for the car app (docs/TRIPS_API.md, section "EV settings").

One JSON document (operator preferences, tariffs, owned cards, default tariff, battery, vehicle) that the app
keeps in sync so the settings survive a phone or box change. Optimistic concurrency: a PUT carries the revision
it was based on and is refused with 409 (and the current document) when somebody else wrote in between.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any

from aiohttp import web

from .trips import _db, _error, _json

SETTINGS_KEY = "ev"
MAX_SETTINGS_BYTES = 1024 * 1024
KNOWN_KEYS = ("operators", "cards", "default_tariff", "battery", "vehicle", "restrict", "updated")
MAX_OPERATORS = 5000
MAX_CARDS = 500
MAX_NAME = 120
MAX_NOTE = 1000
MAX_TS_MS = 4_102_444_800_000  # year 2100
VEHICLES = ("id3_58", "id3_77")

_PRICE = (0.0, 1000.0)


class InvalidSettings(ValueError):
    """The document does not fit the contract; the message names the field."""


def is_ev_settings_path(path: str) -> bool:
    return path == "/api/settings/ev"


def _num(value: Any, name: str, lo: float, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidSettings(f"{name} must be a number")
    if not math.isfinite(value) or not lo <= value <= hi:
        raise InvalidSettings(f"{name} out of range ({lo:g}..{hi:g})")
    return value


def _str(value: Any, name: str, limit: int) -> str:
    if not isinstance(value, str) or len(value) > limit or any(c < " " and c not in "\n\t" for c in value):
        raise InvalidSettings(f"{name} must be text of at most {limit} characters")
    return value


def _obj(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InvalidSettings(f"{name} must be an object")
    return value


def _strings(value: Any, name: str) -> None:
    if not isinstance(value, list) or len(value) > MAX_CARDS:
        raise InvalidSettings(f"{name} must be a list of at most {MAX_CARDS} texts")
    for item in value:
        _str(item, name, MAX_NAME)


def _tariff(value: Any, name: str, fields: tuple[str, ...]) -> None:
    for key, v in _obj(value, name).items():
        if key not in fields:
            raise InvalidSettings(f"{name}.{key} is not a known field")
        if v is not None:
            _num(v, f"{name}.{key}", *_PRICE)


def validate_settings(doc: Any) -> dict[str, Any]:
    """The document when it is valid, else raises :class:`InvalidSettings`."""
    doc = _obj(doc, "settings")
    for key in doc:
        if key not in KNOWN_KEYS:
            raise InvalidSettings(f"unknown key {key!r}")
    ops = doc.get("operators")
    if ops is not None:
        ops = _obj(ops, "operators")
        if len(ops) > MAX_OPERATORS:
            raise InvalidSettings(f"at most {MAX_OPERATORS} operators")
        for name, entry in ops.items():
            _str(name, "operator name", MAX_NAME)
            for key, v in _obj(entry, f"operators.{name}").items():
                if key == "mode":
                    _str(v, "mode", 32)
                elif key == "tariff":
                    _tariff(v, "tariff", ("ac", "dc", "min", "fee"))
                elif key == "cards":
                    _strings(v, "operator cards")
                elif key == "note":
                    if v is not None:
                        _str(v, "note", MAX_NOTE)
                elif key == "updatedAt":
                    _num(v, "updatedAt", 0, MAX_TS_MS)
                else:
                    raise InvalidSettings(f"operators.{name}.{key} is not a known field")
    if "cards" in doc:
        _strings(doc["cards"], "cards")
    if doc.get("default_tariff") is not None:
        _tariff(doc["default_tariff"], "default_tariff", ("ac", "dc", "fee"))
    if doc.get("battery") is not None:
        for key, v in _obj(doc["battery"], "battery").items():
            if key == "choice":
                if v not in VEHICLES:
                    raise InvalidSettings("battery.choice is not a known vehicle")
            elif key == "custom_kwh":
                if v is not None:
                    _num(v, "battery.custom_kwh", 20.0, 120.0)
            else:
                raise InvalidSettings(f"battery.{key} is not a known field")
    if doc.get("vehicle") is not None and doc["vehicle"] not in VEHICLES:
        raise InvalidSettings("vehicle is not a known vehicle")
    if doc.get("restrict") is not None and not isinstance(doc["restrict"], bool):
        raise InvalidSettings("restrict must be true or false")
    if doc.get("updated") is not None:
        for key, v in _obj(doc["updated"], "updated").items():
            _str(key, "updated key", 32)
            _num(v, f"updated.{key}", 0, MAX_TS_MS)
    return doc


def _document(row: tuple[str, str, int] | None) -> dict[str, Any]:
    if row is None:
        return {"revision": 0, "updated_at": None, "settings": {}}
    return {"revision": row[2], "updated_at": row[1], "settings": json.loads(row[0])}


async def _get(request: web.Request) -> web.StreamResponse:
    return _json(200, _document(_db(request).get_ev_settings(SETTINGS_KEY)))


async def _put(request: web.Request) -> web.StreamResponse:
    length = request.content_length
    if length is not None and length > MAX_SETTINGS_BYTES:
        return _error(413, "too_large", f"at most {MAX_SETTINGS_BYTES} bytes")
    raw = await request.read()
    if len(raw) > MAX_SETTINGS_BYTES:
        return _error(413, "too_large", f"at most {MAX_SETTINGS_BYTES} bytes")
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return _error(400, "invalid_json")
    if not isinstance(body, dict):
        return _error(400, "invalid_body", "the body must be {base_revision, settings}")
    base = body.get("base_revision")
    if isinstance(base, bool) or not isinstance(base, int) or base < 0:
        return _error(400, "invalid_body", "base_revision must be a whole number >= 0")
    try:
        settings = validate_settings(body.get("settings"))
    except InvalidSettings as exc:
        return _error(400, "invalid_settings", str(exc))
    text = json.dumps(settings, separators=(",", ":"), ensure_ascii=True)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    ok, current = _db(request).put_ev_settings(SETTINGS_KEY, text, base, now)
    if not ok:
        doc = _document(current)
        doc["error"] = "revision_conflict"
        return _json(409, doc)
    return _json(200, _document(current))


def register_ev_settings_routes(app: web.Application) -> None:
    app.router.add_get("/api/settings/ev", _get)
    app.router.add_put("/api/settings/ev", _put)
