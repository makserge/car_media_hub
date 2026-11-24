"""Small data files the car app refreshes by itself (the charger databases), on the sync API (same bearer token).

``GET /api/resources/manifest`` lists ``<region_storage_dir>/.resources/chargers/*.sqlite`` with size and SHA-256;
``GET /api/resources/files/chargers/<name>`` serves one of them (sendfile, single byte range, strong ``ETag`` = SHA-256; the hash comes from a cache keyed by size, mtime, inode and ctime of the open file, never from a re-read per request). Strict name
whitelist, no links, no sub folders. The add-on builds ``chargers-de.sqlite`` there itself (``charger_refresh.py``); files put there by hand (for example built with ``tools/build_charger_db.py``) are served too;
without the folder the manifest is empty and the app simply keeps what it has.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
import sys
from email.utils import formatdate
from pathlib import Path
from typing import Any

from aiohttp import web

from .fileserve import SendfileResponse, Unsatisfiable, if_range_ok, parse_range
from .streams import MAX_STREAMS_HEADER, STREAMS, StreamBudget, busy_response

PREFIX = "/api/resources"
KINDS = ("chargers",)
RESOURCES_DIR = ".resources"
MAX_FILES = 20
MAX_BYTES = 512 * 1024 * 1024
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\.sqlite")
_ROOT = web.AppKey("resources_root", object)
_CACHE = web.AppKey("resources_hash_cache", dict)


def valid_name(name: str) -> bool:
    return NAME.fullmatch(name) is not None and ".." not in name


def _stamp(st: os.stat_result) -> tuple[int, int, int, int]:
    """What identifies the bytes of a file for the hash cache: size, mtime, inode and ctime.

    ``mtime`` can be set back by ``cp -p``, ``rsync -a`` or ``touch -r`` (and is coarse on some mounts); the inode changes
    when a file is replaced and the ctime cannot be set from user space, so a rewrite with the same size and an unchanged
    mtime is still noticed.
    """
    # on Windows st_ctime is the creation time by path but the change time by handle: not comparable, left out there
    return (st.st_size, st.st_mtime_ns, st.st_ino, 0 if sys.platform == "win32" else st.st_ctime_ns)


def _plain_files(folder: Path) -> list[tuple[str, os.stat_result]]:
    """Regular files (not links) of the folder that pass the name whitelist, sorted by name."""
    out: list[tuple[str, os.stat_result]] = []
    try:
        entries = sorted(os.scandir(folder), key=lambda e: e.name)
    except OSError:
        return out
    for entry in entries:
        if not valid_name(entry.name):
            continue
        try:
            st = os.lstat(entry.path)  # not entry.stat(): on Windows that one has no inode, the cache stamp needs it
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode) and st.st_size <= MAX_BYTES:
            out.append((entry.name, st))
        if len(out) >= MAX_FILES:
            break
    return out


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_manifest(root: Path, cache: dict) -> dict[str, Any]:
    """The manifest document. A file is hashed again only when its size, mtime, inode or ctime changed."""
    resources: list[dict[str, Any]] = []
    for kind in KINDS:
        folder = root / RESOURCES_DIR / kind
        for name, st in _plain_files(folder):
            key = (kind, name)
            stamp = _stamp(st)
            cached = cache.get(key)
            if cached is None or cached[0] != stamp:
                try:
                    cached = (stamp, _sha256(folder / name))
                except OSError:
                    continue
                cache[key] = cached
            resources.append({"kind": kind, "name": name, "size": st.st_size, "sha256": cached[1]})
    return {"version": 1, "resources": resources}


def _json(status: int, payload: Any, headers: dict[str, str] | None = None) -> web.Response:
    resp = web.Response(status=status, body=json.dumps(payload, separators=(",", ":")).encode("ascii"),
                        content_type="application/json", charset="utf-8")
    resp.headers["Cache-Control"] = "no-store"
    for key, value in (headers or {}).items():
        resp.headers[key] = value
    return resp


def register_resource_routes(app: web.Application, root: Path) -> None:
    app[_ROOT] = Path(root)
    app[_CACHE] = {}
    app.router.add_get(PREFIX + "/manifest", _manifest)
    app.router.add_get(PREFIX + "/files/{kind}/{name}", _file)


async def _manifest(request: web.Request) -> web.StreamResponse:
    body = await asyncio.to_thread(build_manifest, request.app[_ROOT], request.app[_CACHE])
    return _json(200, body)


def _prepare_file(root: Path, cache: dict, kind: str, name: str) -> tuple[Any, os.stat_result, str] | None:
    """Open one resource safely and find its SHA-256 without reading it again when it is known (blocking).

    A regular file that is not a link, at most ``MAX_BYTES``, opened without following a link. The hash comes from the
    cache when size, mtime, inode and ctime of the *open* file still match; else it is computed from the open file (and
    cached). None when the file is missing or unusable; a ``ValueError`` when it changed while it was hashed.
    """
    path = root / RESOURCES_DIR / kind / name
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_BYTES:
            os.close(fd)
            return None
        handle = os.fdopen(fd, "rb", buffering=0)
    except BaseException:
        os.close(fd)
        raise
    try:
        stamp = _stamp(st)
        cached = cache.get((kind, name))
        if cached is not None and cached[0] == stamp:
            return handle, st, cached[1]
        digest = hashlib.sha256()
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
        after = os.fstat(handle.fileno())
        if _stamp(after) != stamp:
            raise ValueError("file changed while it was hashed")
        cache[(kind, name)] = (stamp, digest.hexdigest())
        return handle, st, digest.hexdigest()
    except BaseException:
        handle.close()
        raise


async def _file(request: web.Request) -> web.StreamResponse:
    kind, name = request.match_info["kind"], request.match_info["name"]
    if kind not in KINDS or not valid_name(name):
        return _json(404, {"error": "not_found"})
    budget: StreamBudget | None = request.app.get(STREAMS)
    max_streams = {MAX_STREAMS_HEADER: str(budget.per_client)} if budget is not None else {}
    try:
        prepared = await asyncio.to_thread(_prepare_file, request.app[_ROOT], request.app[_CACHE], kind, name)
    except ValueError:  # the file changed between the hash and now: the app would fail its check, so say "try again"
        return _json(503, {"error": "file_changing"})
    except OSError:
        return _json(404, {"error": "not_found"})
    if prepared is None:
        return _json(404, {"error": "not_found"})
    handle, st, sha = prepared
    try:
        size = st.st_size
        etag = '"' + sha + '"'
        last_modified = formatdate(int(st.st_mtime), usegmt=True)
        start, end, status = 0, size - 1, 200
        range_header = request.headers.get("Range")
        if range_header and size > 0 and if_range_ok(request.headers.get("If-Range"), etag, last_modified, st.st_mtime_ns):
            try:
                selected = parse_range(range_header, size)
            except Unsatisfiable:
                return _json(416, {"error": "range_not_satisfiable"},
                             {"Content-Range": f"bytes */{size}", "ETag": etag, "Accept-Ranges": "bytes", **max_streams})
            if selected is not None:
                (start, end), status = selected, 206
        headers = {"Content-Type": "application/octet-stream", "Cache-Control": "no-store", "Accept-Ranges": "bytes",
                   "ETag": etag, "Last-Modified": last_modified, **max_streams}
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        stream = None
        if budget is not None and request.method != "HEAD":
            stream = budget.begin(request.remote or "", ("resource", kind, name), request.transport)
            if stream is None:
                return busy_response(max_streams)
        try:
            resp = SendfileResponse(handle, start, end - start + 1 if size else 0, status=status, headers=headers,
                                    stream=stream, what=f"resources/{kind}/{name}")
            await resp.send(request)
            return resp
        finally:
            if stream is not None and budget is not None:
                budget.end(stream)
    finally:
        handle.close()
