"""Regions on the sync API (port 8100, same bearer token): the catalogue and the region files.

``GET /api/regions/catalogue`` is JSON compatible with the car app's ``regions.json`` v2;
``GET|HEAD /api/regions/files/<id>/<name>`` serves one of ``map.pmtiles``, ``routing.tar``, ``places.sqlite`` and the gzip
transfer copies ``routing.tar.gz``, ``places.sqlite.gz`` (only while they are valid for the listed plain file)
(strict whitelist, no links, only a hashed file that is unchanged since (else 503), a single byte range, ``ETag``/``Last-Modified``/``If-Range``, sent with sendfile).
Authentication and the error format come from the middlewares of :mod:`car_media_hub.sync`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from email.utils import formatdate
from typing import Any

from aiohttp import web

from .build_info import build_info
from .fileserve import SendfileResponse, Unsatisfiable, if_range_ok as _if_range_ok, parse_range  # noqa: F401 - re-exported
from .regions import SERVED_FILES, RegionManager, open_region_file, valid_region_id
from .regions_devices import ReportError
from .resources_api import register_resource_routes
from .streams import MAX_STREAMS_HEADER, busy_response

log = logging.getLogger(__name__)

WRITE_TIMEOUT = 120.0
RETRY_AFTER = "30"
PREFIX = "/api/regions"
REPORT_PATH = PREFIX + "/devices/report"
MAX_REPORT_BYTES = 8 * 1024
_MANAGER = web.AppKey("regions_manager", object)


def is_regions_path(path: str) -> bool:
    return path == PREFIX or path.startswith(PREFIX + "/")


def is_report_request(request: web.Request) -> bool:
    """True for the one POST of the regions API (the auth middleware lets it through when regions are served)."""
    return request.method == "POST" and request.rel_url.path == REPORT_PATH and _MANAGER in request.app


def _json(status: int, payload: Any, headers: dict[str, str] | None = None) -> web.Response:
    resp = web.Response(status=status, body=json.dumps(payload, separators=(",", ":")).encode("ascii"),
                        content_type="application/json", charset="utf-8")
    resp.headers["Cache-Control"] = "no-store"
    for key, value in (headers or {}).items():
        resp.headers[key] = value
    return resp


def _target(request: web.Request) -> tuple[str, str] | None:
    """(region id, file name) from the raw request target, or None when it is anything but exactly two plain segments."""
    raw = request.raw_path
    if not raw.startswith("/"):
        raw = request.rel_url.raw_path
    path = raw.split("?", 1)[0]
    prefix = PREFIX + "/files/"
    if not path.startswith(prefix):
        return None
    parts = path[len(prefix):].split("/")
    if len(parts) != 2 or any("%" in p or "\\" in p for p in parts):
        return None
    region_id, name = parts
    if not valid_region_id(region_id) or name not in SERVED_FILES:
        return None
    return region_id, name


def register_region_routes(app: web.Application, manager: RegionManager) -> None:
    app[_MANAGER] = manager
    app.router.add_get(PREFIX + "/catalogue", _catalogue)
    app.router.add_get(PREFIX + "/files/{tail:.*}", _file)
    app.router.add_get(PREFIX + "/devices", _devices)
    app.router.add_post(REPORT_PATH, _report)
    register_resource_routes(app, manager.root)


async def _catalogue(request: web.Request) -> web.StreamResponse:
    manager: RegionManager = request.app[_MANAGER]
    try:
        body = await manager.catalogue_body()
    except OSError:
        log.warning("Regions API: catalogue unavailable", exc_info=True)
        return _json(503, {"error": "regions_unavailable"}, {"Retry-After": RETRY_AFTER})
    if manager.problem:
        return _json(503, {"error": "regions_unavailable"}, {"Retry-After": RETRY_AFTER})
    resp = web.Response(body=body, content_type="application/json", charset="utf-8")
    resp.headers["Cache-Control"] = "no-store"
    return resp


async def _devices(request: web.Request) -> web.StreamResponse:
    manager: RegionManager = request.app[_MANAGER]
    try:
        devices = await manager.devices_public()
    except OSError:
        log.warning("Regions API: device reports unavailable", exc_info=True)
        return _json(503, {"error": "regions_unavailable"}, {"Retry-After": RETRY_AFTER})
    return _json(200, {"devices": devices, "build": build_info()})


async def _report(request: web.Request) -> web.StreamResponse:
    """A car app tells which regions it has (JSON, at most 8 KB). The answer carries nothing but ``ok``."""
    manager: RegionManager = request.app[_MANAGER]
    if request.content_type != "application/json":
        return _json(415, {"error": "unsupported_media_type", "detail": "send application/json"})
    if request.content_length is not None and request.content_length > MAX_REPORT_BYTES:
        return _json(413, {"error": "payload_too_large", "detail": f"at most {MAX_REPORT_BYTES} bytes"})
    raw = bytearray()
    async for chunk in request.content.iter_chunked(4096):
        raw += chunk
        if len(raw) > MAX_REPORT_BYTES:
            return _json(413, {"error": "payload_too_large", "detail": f"at most {MAX_REPORT_BYTES} bytes"})
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        return _json(400, {"error": "bad_request", "detail": "invalid JSON"})
    try:
        await manager.device_report(data)
    except ReportError as exc:
        return _json(exc.status, {"error": "bad_request" if exc.status == 400 else "refused", "detail": str(exc)})
    except OSError:
        log.warning("Regions API: cannot store a device report", exc_info=True)
        return _json(503, {"error": "regions_unavailable"}, {"Retry-After": RETRY_AFTER})
    return _json(200, {"ok": True})


async def _file(request: web.Request) -> web.StreamResponse:
    manager: RegionManager = request.app[_MANAGER]
    target = _target(request)
    if target is None:
        return _json(404, {"error": "not_found"})
    region_id, name = target
    try:
        handle = await asyncio.to_thread(open_region_file, manager.root, region_id, name)
    except OSError:
        return _json(404, {"error": "not_found"})
    max_streams = {MAX_STREAMS_HEADER: str(manager.streams.per_client)}
    try:
        st = os.fstat(handle.fileno())
        # a file that is still being written, or that changed since it was hashed, is not served: the car app would
        # download gigabytes and then fail its SHA-256 check
        info = await manager.servable(region_id, name, st)
        if info is None:
            return _json(503, {"error": "file_changing"}, {"Retry-After": RETRY_AFTER})
        size = st.st_size
        etag = info.etag  # the strong ETag is the SHA-256 of the file (quoted hex)
        last_modified = formatdate(int(st.st_mtime), usegmt=True)
        start, end, status = 0, size - 1, 200
        range_header = request.headers.get("Range")
        if range_header and size > 0 and _if_range_ok(request.headers.get("If-Range"), etag, last_modified, st.st_mtime_ns):
            try:
                selected = parse_range(range_header, size)
            except Unsatisfiable:
                return _json(416, {"error": "range_not_satisfiable"},
                             {"Content-Range": f"bytes */{size}", "ETag": etag, "Accept-Ranges": "bytes", **max_streams})
            if selected is not None:
                (start, end), status = selected, 206
        length = end - start + 1 if size else 0
        headers = {
            "Content-Type": "application/octet-stream", "Cache-Control": "no-store", "Accept-Ranges": "bytes",
            "ETag": etag, "Last-Modified": last_modified, **max_streams,
        }
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        stream = None
        if request.method != "HEAD":
            # a resumed request must not be refused because the old connection of the same client is still draining
            stream = manager.begin_download((request.remote or "", region_id, name), request.transport)
            if stream is None:
                return busy_response(max_streams)
        try:
            resp = SendfileResponse(handle, start, length, status=status, headers=headers, stream=stream,
                                    timeout=WRITE_TIMEOUT, what=f"{region_id}/{name}")
            await resp.send(request)
            return resp
        finally:
            if stream is not None:
                manager.end_download(stream)
    finally:
        handle.close()
