"""Slow disk work (charger download, listing rebuild, region rescan) must not stall the event loop or file requests."""
from __future__ import annotations

import asyncio
import os
import time

import aiohttp
import pytest

from car_media_hub import charger_refresh as C
from car_media_hub import regions as R
from car_media_hub import sync as S
from car_media_hub.sources import fetch

from test_charger_refresh import make, src  # noqa: F401  (fixtures)
from test_regions import AUTH, mgr, root, sync_client, make_region, wait_hashed  # noqa: F401
from test_sync_ranges import blob, start

SLOW = 0.5  # seconds a stubbed disk operation takes


class Ticker:
    """Wakes every 10 ms and remembers the longest time the event loop was away."""

    def __init__(self):
        self.max_gap = 0.0
        self._task = None

    async def _run(self):
        last = time.monotonic()
        while True:
            await asyncio.sleep(0.01)
            now = time.monotonic()
            self.max_gap = max(self.max_gap, now - last)
            last = now

    async def __aenter__(self):
        self._task = asyncio.create_task(self._run())
        await asyncio.sleep(0.05)
        return self

    async def __aexit__(self, *exc):
        await asyncio.sleep(0.05)  # lets the ticker record a block that ended right before
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)


async def test_charger_download_writes_and_fsyncs_off_the_loop(tmp_path, src, monkeypatch):
    real_fsync = os.fsync

    def slow_fsync(fd):
        time.sleep(SLOW)
        real_fsync(fd)

    monkeypatch.setattr(C.os, "fsync", slow_fsync)
    ref = make(tmp_path, src)
    dest = tmp_path / "download.csv"
    async with ref._session_factory() as session, Ticker() as ticker:
        t0 = time.monotonic()
        assert await ref._download(session, src.base + "/Ladesaeulenregister_BNetzA_2026-09-01.csv", dest) is True
        assert time.monotonic() - t0 >= SLOW  # the slow fsync really ran
    assert dest.read_bytes() == src.csv
    assert ticker.max_gap < SLOW / 2, f"the event loop was blocked for {ticker.max_gap:.2f} s"


async def test_source_download_hashes_and_writes_off_the_loop(tmp_path, monkeypatch):
    """``download_file``: every digest update and write happens in a worker thread."""
    import hashlib
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    payload = blob(3_000_000)

    async def body(request):
        return web.Response(body=payload)

    app = web.Application()
    app.router.add_get("/x.mp3", body)
    server = TestServer(app)
    await server.start_server()
    threads = set()

    class Digest:
        def __init__(self):
            self._h = hashlib.sha256()

        def update(self, data):
            import threading
            threads.add(threading.current_thread().name)
            self._h.update(data)

        def hexdigest(self):
            return self._h.hexdigest()

    monkeypatch.setattr(fetch.hashlib, "new", lambda algo: Digest())
    try:
        import threading
        folder = tmp_path / "dl"
        folder.mkdir()
        async with aiohttp.ClientSession() as session:
            n = await fetch.download_file(fetch.HttpClient(session), f"http://127.0.0.1:{server.port}/x.mp3", folder,
                                          "x.mp3", 10_000_000, checksum=hashlib.sha256(payload).hexdigest(),
                                          checksum_algo="sha256")
        assert n == len(payload) and (folder / "x.mp3").read_bytes() == payload
        assert threads and threading.current_thread().name not in threads
    finally:
        await server.close()


async def test_file_requests_are_served_from_the_old_listing_while_it_is_rebuilt(tmp_path, monkeypatch):
    now = [1000.0]
    client, app, lib = await start(tmp_path, {"a.mp3": blob(200_000)}, clock=lambda: now[0], ttl=30.0)
    try:
        url = "/api/v1/files/a.mp3"
        assert (await client.get(url, headers=AUTH)).status == 200  # first (blocking) build
        real_scan = S._scan_library
        started = []

        def slow_scan(*args, **kw):
            started.append(time.monotonic())
            time.sleep(SLOW)
            return real_scan(*args, **kw)

        monkeypatch.setattr(S, "_scan_library", slow_scan)
        now[0] += 31  # the TTL ran out
        t0 = time.monotonic()
        listing = await client.get("/api/v1/files", headers=AUTH)
        resp = await client.get(url, headers=AUTH)
        body = await resp.read()
        took = time.monotonic() - t0
        assert listing.status == 200 and resp.status == 200 and len(body) == 200_000
        assert took < SLOW / 2, f"requests waited {took:.2f} s for the rebuild"
        for _ in range(100):  # the rebuild ran once, in the background
            if app[S._INDEX]._built_at == now[0]:
                break
            await asyncio.sleep(0.05)
        assert len(started) == 1 and app[S._INDEX]._built_at == now[0]
        # after it a new file shows up
        (lib / "b.mp3").write_bytes(b"x" * 10)
        old = time.time() - 3600
        os.utime(lib / "b.mp3", (old, old))
        monkeypatch.setattr(S, "_scan_library", real_scan)
        now[0] += 31
        stale = await (await client.get("/api/v1/files", headers=AUTH)).json()
        assert [e["name"] for e in stale] == ["a.mp3"]  # still the cached one on the very request that starts the rebuild
        for _ in range(100):
            fresh = await (await client.get("/api/v1/files", headers=AUTH)).json()
            if len(fresh) == 2:
                break
            await asyncio.sleep(0.05)
        assert sorted(e["name"] for e in fresh) == ["a.mp3", "b.mp3"]
    finally:
        await client.close()


async def test_an_old_listing_is_not_served_for_ever(tmp_path):
    now = [1000.0]
    client, app, lib = await start(tmp_path, {"a.mp3": blob(1000)}, clock=lambda: now[0], ttl=30.0)
    try:
        assert (await client.get("/api/v1/files", headers=AUTH)).status == 200
        (lib / "a.mp3").unlink()
        now[0] += S.MAX_STALE + 1  # too old: the next request waits for a real rebuild
        resp = await client.get("/api/v1/files", headers=AUTH)
        assert resp.status in (200, 503)  # an emptied library is refused by the empty-library guard
        assert resp.status == 503 or await resp.json() == []
    finally:
        await client.close()


async def test_a_failed_background_rebuild_is_answered_on_the_next_request(tmp_path, monkeypatch):
    now = [1000.0]
    client, app, lib = await start(tmp_path, {"a.mp3": blob(1000)}, clock=lambda: now[0], ttl=30.0)
    try:
        assert (await client.get("/api/v1/files", headers=AUTH)).status == 200

        def failing(*args, **kw):
            raise S.LibraryUnavailable("disk went away")

        monkeypatch.setattr(S, "_scan_library", failing)
        now[0] += 31
        assert (await client.get("/api/v1/files", headers=AUTH)).status == 200  # stale answer, rebuild fails behind it
        await asyncio.sleep(0.2)
        assert (await client.get("/api/v1/files", headers=AUTH)).status == 503
    finally:
        await client.close()


async def test_region_files_do_not_wait_for_a_rescan(root, mgr, tmp_path, monkeypatch):
    make_region(root, "slow-1")
    await wait_hashed(mgr)
    client = await sync_client(mgr, tmp_path)
    try:
        url = "/api/regions/files/slow-1/map.pmtiles"
        assert (await client.get(url, headers=AUTH)).status == 200
        real_scan = R.scan_region_dir
        calls = []

        def slow_scan(*args, **kw):
            calls.append(1)
            time.sleep(SLOW)
            return real_scan(*args, **kw)

        monkeypatch.setattr(R, "scan_region_dir", slow_scan)
        mgr.invalidate()  # a rescan is due (an upload elsewhere, a finished hash)
        t0 = time.monotonic()
        resp = await client.get(url, headers=AUTH)
        await resp.read()
        took = time.monotonic() - t0
        assert resp.status == 200 and took < SLOW / 2, f"the download waited {took:.2f} s for the rescan"
        for _ in range(100):  # the due rescan ran in the background
            if calls:
                break
            await asyncio.sleep(0.05)
        assert calls
        await asyncio.sleep(SLOW + 0.3)
    finally:
        await client.close()


async def test_a_changed_region_file_still_waits_for_the_rescan_and_is_refused(root, mgr, tmp_path):
    make_region(root, "chg-1")
    await wait_hashed(mgr)
    client = await sync_client(mgr, tmp_path)
    try:
        url = "/api/regions/files/chg-1/map.pmtiles"
        assert (await client.get(url, headers=AUTH)).status == 200
        path = root / "chg-1" / "map.pmtiles"
        path.write_bytes(path.read_bytes() + b"more")  # no longer the hashed file
        resp = await client.get(url, headers=AUTH)
        assert resp.status == 503 and (await resp.json()) == {"error": "file_changing"}
    finally:
        await client.close()
