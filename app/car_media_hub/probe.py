"""Bandwidth probe of the sync API (same bearer token as every other route).

``GET /api/probe?bytes=N`` streams ``N`` bytes (default 8 MiB, at most 64 MiB, larger values are capped) of incompressible
pseudo-random data from memory: no disk access, so a client measures the network path to this add-on and nothing else.
``mb=N`` is accepted as a shorthand for ``bytes=N*1048576`` (``bytes`` wins when both are given). The answer carries an
exact ``Content-Length``, ``Cache-Control: no-store``, ``X-Max-Streams`` and ``X-Probe-Started`` (server monotonic clock in
nanoseconds). A probe takes a slot of the shared stream budget like a download (503 + ``Retry-After`` above the limit).

``GET /api/probe/disk?bytes=N`` reads up to ``N`` bytes (default and maximum 64 MiB) of the largest library file with the
same kind of reads as a download and answers JSON ``{"bytes", "seconds", "cached"}``: the speed of the add-on's disk.
``cached`` is a guess (a rate no disk can reach means the page cache answered). It is empty (0 bytes) for an empty library.
Only one disk probe runs at a time (another one gets ``503`` busy), it reads in its own single worker thread (so it never
occupies the shared executor that serves downloads and listings), it opens the file without following a link, and ``HEAD``
answers the headers only (no read, no slot).
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import stat
import time
from typing import Any

from aiohttp import web

from .fileserve import WRITE_TIMEOUT, mark_background
from .streams import MAX_STREAMS_HEADER, STREAMS, StreamBudget, busy_response, dropped_by_budget

log = logging.getLogger(__name__)

PROBE_PATH = "/api/probe"
DISK_PROBE_PATH = "/api/probe/disk"
DEFAULT_BYTES = 8 << 20
MAX_BYTES = 64 << 20
BLOCK_BYTES = 4 << 20  # the random block that is sent over and over (generated once)
SLICE_BYTES = 256 << 10  # bytes per write (progress is marked per write)
DISK_READ_BYTES = 1 << 20  # bytes per read of the disk probe
CACHED_RATE = 3e9  # bytes per second above which a "disk" read must have come from the page cache

_block: bytes | None = None
_DISK = web.AppKey("disk_probe", object)


class _DiskProbe:
    """State of the disk probe of one app: one probe at a time, in a worker thread of its own."""

    def __init__(self) -> None:
        self.running = False
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="disk-probe")


def random_block() -> bytes:
    """The shared 4 MiB block of ``os.urandom`` (created on first use, so a start without probes costs nothing)."""
    global _block
    if _block is None:
        _block = os.urandom(BLOCK_BYTES)
    return _block


def requested_bytes(request: web.Request) -> int | None:
    """The probe size asked for, capped to ``MAX_BYTES``; the default when absent; None when it is not a positive integer."""
    query = request.query
    raw = query.get("bytes")
    factor = 1
    if raw is None and query.get("mb") is not None:
        raw, factor = query["mb"], 1 << 20
    if raw is None:
        return DEFAULT_BYTES
    raw = raw.strip()
    if not (raw.isascii() and raw.isdigit()) or len(raw) > 18:
        return None
    value = int(raw) * factor
    return None if value <= 0 else min(value, MAX_BYTES)


def _bad_size() -> web.Response:
    return web.Response(status=400, body=b'{"error":"bad_request"}', content_type="application/json", charset="utf-8",
                        headers={"Cache-Control": "no-store"})


async def _probe(request: web.Request) -> web.StreamResponse:
    budget: StreamBudget = request.app[STREAMS]
    size = requested_bytes(request)
    if size is None:
        return _bad_size()
    max_streams = {MAX_STREAMS_HEADER: str(budget.per_client)}
    stream = None
    if request.method != "HEAD":
        stream = budget.begin(request.remote or "", ("probe", PROBE_PATH), request.transport)
        if stream is None:
            return busy_response(max_streams)
    try:
        resp = web.StreamResponse(status=200, headers={
            "Content-Type": "application/octet-stream", "Cache-Control": "no-store",
            "X-Probe-Started": str(time.monotonic_ns()), **max_streams,
        })
        resp.content_length = size
        mark_background(request)
        await resp.prepare(request)
        if request.method == "HEAD":
            await resp.write_eof()
            return resp
        block = memoryview(random_block())
        left, pos = size, 0
        try:
            while left:
                n = min(SLICE_BYTES, left, BLOCK_BYTES - pos)
                async with asyncio.timeout(WRITE_TIMEOUT):
                    await resp.write(block[pos:pos + n])
                pos = (pos + n) % BLOCK_BYTES
                left -= n
                if stream is not None:
                    stream.moved()
            if stream is not None:
                stream.finish()
            await resp.write_eof()
        except ConnectionError:
            log.debug("Sync API: client went away during a probe")
            if request.transport is not None:
                request.transport.abort()
        except TimeoutError:
            if request.transport is not None:
                request.transport.abort()
        except asyncio.CancelledError:
            if not dropped_by_budget(stream):
                raise
            if request.transport is not None:  # the slot went to a newer request; the write has unwound, close the socket
                request.transport.abort()
        return resp
    finally:
        if stream is not None:
            budget.end(stream)


def _read_disk(path: Any, limit: int) -> tuple[int, float]:
    """Read up to ``limit`` bytes of ``path`` in 1 MiB reads (blocking); (bytes read, seconds).

    The file is opened without following a link and must be a regular file (a library file replaced by a link reads 0).
    """
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    done = 0
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb", buffering=0) as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            return 0, 0.0
        t0 = time.perf_counter()
        while done < limit:
            chunk = f.read(min(DISK_READ_BYTES, limit - done))
            if not chunk:
                break
            done += len(chunk)
    return done, time.perf_counter() - t0


async def _disk_probe(request: web.Request) -> web.StreamResponse:
    from .sync import _INDEX, LibraryUnavailable, _json  # late: sync imports this module

    budget: StreamBudget = request.app[STREAMS]
    limit = requested_bytes(request)
    if limit is None:
        return _bad_size()
    max_streams = {MAX_STREAMS_HEADER: str(budget.per_client)}
    if request.method == "HEAD":  # headers only: no read, no slot
        return web.Response(status=200, headers={"Content-Type": "application/json", "Cache-Control": "no-store", **max_streams})
    disk: _DiskProbe = request.app[_DISK]
    if disk.running:  # one probe at a time: a second one would only measure the first
        return busy_response(max_streams)
    stream = budget.begin(request.remote or "", ("probe", DISK_PROBE_PATH), request.transport)
    if stream is None:
        return busy_response(max_streams)
    disk.running = True
    try:
        try:
            entries = await request.app[_INDEX].listing()
        except LibraryUnavailable:
            entries = []
        done, seconds = 0, 0.0
        loop = asyncio.get_running_loop()
        for entry in sorted(entries, key=lambda e: -e.size)[:1]:
            try:
                done, seconds = await loop.run_in_executor(disk.executor, _read_disk, entry.path, min(limit, entry.size))
            except OSError:
                done, seconds = 0, 0.0
        cached = done > 0 and seconds > 0 and done / seconds > CACHED_RATE
        return _json(200, {"bytes": done, "seconds": round(seconds, 6), "cached": cached}, max_streams)
    finally:
        disk.running = False
        budget.end(stream)


async def _close_disk_probe(app: web.Application) -> None:
    app[_DISK].executor.shutdown(wait=False)


def register_probe_routes(app: web.Application) -> None:
    app[_DISK] = _DiskProbe()
    app.on_cleanup.append(_close_disk_probe)
    app.router.add_get(PROBE_PATH, _probe)
    app.router.add_get(DISK_PROBE_PATH, _disk_probe)
