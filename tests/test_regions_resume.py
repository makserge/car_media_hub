"""Resumable region installs on the add-on side: ranged downloads across restarts and rescans, stalled and dropped
connections, the paused / cancelled view of the car app devices, the cancel of an install request, and imports from
a URL that are interrupted (connection, restart) and resumed."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from car_media_hub import regions as R
from car_media_hub import regions_api
from car_media_hub import regions_devices as D
from car_media_hub.regions_import import ImportPolicy
from car_media_hub.regions_web import register_region_pages
from test_regions import AUTH, ADMIN, make_region, old, pmtiles, root, sqlite_bytes, sync_client, wait_hashed  # noqa: F401  (root is a fixture)

URL = "/api/regions/files/big-1/map.pmtiles"
LOCAL = ImportPolicy(require_https=False, check_hosts=False)
DEV = "device-0001"


def big_region(root, size=3_000_000):
    d = make_region(root, "big-1")
    data = b"PMTiles" + (bytes(range(251)) * (size // 251 + 1))[: size - 8]  # not repetitive: a wrong offset shows
    (d / "map.pmtiles").write_bytes(data)
    old(d / "map.pmtiles")
    return data


async def new_mgr(root, **kw):
    m = R.RegionManager(root, 64 * 1024 * 1024, min_free=0, import_policy=LOCAL, import_retry_delays=(), **kw)
    await m.start()
    return m


# ------------------------------------------------------------------ serving a resumed download


async def test_partial_get_then_resumed_range_gives_identical_bytes_and_etag(root, tmp_path):
    data = big_region(root)
    mgr = await new_mgr(root)
    await wait_hashed(mgr)
    client = await sync_client(mgr, tmp_path)
    try:
        first = await client.get(URL, headers=AUTH)
        etag = first.headers["ETag"]
        part = await first.content.readexactly(1_000_000)
        first.close()  # the car went to sleep
        resumed = await client.get(URL, headers={**AUTH, "Range": f"bytes={len(part)}-", "If-Range": etag})
        assert resumed.status == 206 and resumed.headers["ETag"] == etag
        assert resumed.headers["Content-Range"] == f"bytes {len(part)}-{len(data) - 1}/{len(data)}"
        assert part + await resumed.read() == data
        assert etag == f'"{hashlib.sha256(data).hexdigest()}"'
        assert mgr.active_downloads == 0
    finally:
        await client.close()
        await mgr.stop()


async def test_resume_works_right_after_an_add_on_restart_without_hashing_again(root, tmp_path):
    data = big_region(root)
    mgr = await new_mgr(root)
    await wait_hashed(mgr)
    client = await sync_client(mgr, tmp_path)
    etag = (await client.head(URL, headers=AUTH)).headers["ETag"]
    await client.close()
    await mgr.stop()
    cache = (root / "big-1" / "files.json").read_bytes()

    mgr2 = await new_mgr(root)  # a new add-on process on the same share
    mgr2._hasher.submit = lambda *a: pytest.fail("the file must not be hashed again")
    client = await sync_client(mgr2, tmp_path)
    try:
        resp = await client.get(URL, headers={**AUTH, "Range": "bytes=2400000-", "If-Range": etag})  # no waiting for a hash
        assert resp.status == 206 and resp.headers["ETag"] == etag
        assert await resp.read() == data[2400000:]
        assert (root / "big-1" / "files.json").read_bytes() == cache
    finally:
        await client.close()
        await mgr2.stop()


async def test_rescan_and_files_json_rewrite_during_a_download_change_nothing(root, tmp_path):
    data = big_region(root)
    mgr = await new_mgr(root)
    await wait_hashed(mgr)
    client = await sync_client(mgr, tmp_path)
    try:
        first = await client.get(URL, headers=AUTH)
        etag = first.headers["ETag"]
        head = await first.content.readexactly(500_000)
        mgr.invalidate()
        await mgr.rescan()
        doc = json.loads((root / "big-1" / "files.json").read_text())
        await asyncio.to_thread(R.write_json_atomic, root / "big-1" / "files.json", doc)  # rewritten by the hash worker
        await mgr.rescan()
        assert head + await first.content.read() == data  # the running download is not touched
        again = await client.get(URL, headers={**AUTH, "Range": "bytes=10-19", "If-Range": etag})
        assert again.status == 206 and again.headers["ETag"] == etag and await again.read() == data[10:20]
    finally:
        await client.close()
        await mgr.stop()


async def test_range_edge_cases_head_416_and_date_if_range(root, tmp_path):
    data = big_region(root, 100_000)
    mgr = await new_mgr(root)
    await wait_hashed(mgr)
    client = await sync_client(mgr, tmp_path)
    try:
        head = await client.head(URL, headers=AUTH)
        etag, modified = head.headers["ETag"], head.headers["Last-Modified"]
        assert head.status == 200 and head.headers["Content-Length"] == str(len(data)) and head.headers["Accept-Ranges"] == "bytes"
        done = await client.get(URL, headers={**AUTH, "Range": f"bytes={len(data)}-", "If-Range": etag})  # .part is complete already
        assert done.status == 416 and done.headers["Content-Range"] == f"bytes */{len(data)}" and done.headers["ETag"] == etag
        by_date = await client.get(URL, headers={**AUTH, "Range": "bytes=5-9", "If-Range": modified})
        assert by_date.status == 206 and await by_date.read() == data[5:10]
        other = await client.get(URL, headers={**AUTH, "Range": f"bytes={len(data)}-", "If-Range": '"someone-else"'})
        assert other.status == 200 and await other.read() == data  # a different version: everything again, never a 416
        ranged_head = await client.head(URL, headers={**AUTH, "Range": "bytes=10-19"})
        assert ranged_head.status == 206 and ranged_head.headers["Content-Length"] == "10"
    finally:
        await client.close()
        await mgr.stop()


async def test_two_clients_download_different_files_in_parallel(root, tmp_path):
    big = big_region(root, 1_500_000)
    d = make_region(root, "other-1", routing=True)
    (d / "routing.tar").write_bytes((d / "routing.tar").read_bytes() + b"\0" * 1_000_000)
    old(d / "routing.tar")
    mgr = await new_mgr(root)
    await wait_hashed(mgr)
    client = await sync_client(mgr, tmp_path)
    try:
        a = await client.get(URL, headers=AUTH)
        b = await client.get("/api/regions/files/other-1/routing.tar", headers=AUTH)
        got = await asyncio.gather(a.read(), b.read())
        assert got[0] == big and got[1] == (d / "routing.tar").read_bytes()
        assert mgr.active_downloads == 0
    finally:
        await client.close()
        await mgr.stop()


async def raw_get(port, path, read_first=0):
    """A client that sends a request and then reads ``read_first`` bytes of the answer, nothing more."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: x\r\nAuthorization: {AUTH['Authorization']}\r\n\r\n".encode())
    await writer.drain()
    if read_first:
        await reader.readexactly(read_first)
    return reader, writer


async def wait_until(check, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return True
        await asyncio.sleep(0.02)
    return False


async def test_a_client_that_stops_reading_is_dropped_quietly(root, tmp_path, monkeypatch, caplog):
    big_region(root, 40_000_000)
    monkeypatch.setattr(regions_api, "WRITE_TIMEOUT", 0.4)
    mgr = await new_mgr(root)
    await wait_hashed(mgr)
    client = await sync_client(mgr, tmp_path)
    caplog.set_level(logging.DEBUG)
    try:
        reader, writer = await raw_get(client.server.port, URL, read_first=200)  # then the car sleeps: no more reads
        assert await wait_until(lambda: mgr.active_downloads == 0, 8)
        writer.close()
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
    finally:
        await client.close()
        await mgr.stop()


async def test_a_client_that_disconnects_mid_body_leaks_nothing(root, tmp_path, caplog):
    big_region(root, 40_000_000)
    mgr = await new_mgr(root)
    await wait_hashed(mgr)
    client = await sync_client(mgr, tmp_path)
    caplog.set_level(logging.DEBUG)
    try:
        reader, writer = await raw_get(client.server.port, URL, read_first=300)
        writer.transport.abort()
        assert await wait_until(lambda: mgr.active_downloads == 0, 8)
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
        # nothing keeps the file open: it can be replaced and deleted at once (a Windows handle would refuse)
        os.replace(root / "big-1" / "map.pmtiles", root / "big-1" / "moved.tmp")
    finally:
        await client.close()
        await mgr.stop()


async def test_a_reconnecting_client_gets_the_slot_of_its_stalled_connection(root, tmp_path):
    big_region(root, 1_000_000)
    mgr = await new_mgr(root)
    await wait_hashed(mgr)
    client = await sync_client(mgr, tmp_path)
    try:
        etag = (await client.head(URL, headers=AUTH)).headers["ETag"]
        for i in range(mgr.streams.per_client):  # eight connections of a car that fell asleep, one of them for this file
            stream = mgr.begin_download(("127.0.0.1", "big-1" if i == 0 else "other", "map.pmtiles" if i == 0 else f"f{i}"), None)
            stream.progress_at -= R.STALL_SECONDS + 1
        resumed = await client.get(URL, headers={**AUTH, "Range": "bytes=999000-", "If-Range": etag})
        assert resumed.status == 206 and len(await resumed.read()) == 1000
        assert mgr.active_downloads == mgr.streams.per_client - 1  # exactly one stalled slot was taken over, and given back
    finally:
        await client.close()
        await mgr.stop()


async def test_downloads_that_move_are_never_dropped(root, tmp_path):
    big_region(root, 100_000)
    mgr = await new_mgr(root)
    await wait_hashed(mgr)
    client = await sync_client(mgr, tmp_path)
    try:
        for i in range(mgr.streams.per_client):
            mgr.begin_download(("127.0.0.1", "big-1", f"f{i}"), None)
        busy = await client.get(URL, headers=AUTH)
        assert busy.status == 503 and busy.headers["Retry-After"]
        assert mgr.active_downloads == mgr.streams.per_client
    finally:
        await client.close()
        await mgr.stop()


async def test_hash_finished_during_a_scan_is_not_forgotten(root):
    """A rescan that started before the hash was stored must not leave the snapshot clean (stale "hashing")."""
    make_region(root)
    mgr = await new_mgr(root)
    await wait_hashed(mgr)
    mgr._hash_done()  # a hash finishes while the folder is being read
    assert mgr._dirty is True
    await mgr.rescan()
    mgr._hash_done()
    assert mgr._dirty is True
    await mgr.stop()


# ------------------------------------------------------------------ devices: paused, progress kept, never expiring requests


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def rep(state, **extra):
    return {"deviceId": DEV, "name": "ID.3", "appVersion": "1.0",
            "regions": [{"id": "big-1", "state": state, "version": "v1", **extra}]}


async def devices(mgr):
    return (await mgr.devices_public())[0]["regions"][0]


async def test_quiet_downloading_device_shows_as_paused_with_progress(root):
    clock = Clock()
    make_region(root, "big-1")
    mgr = R.RegionManager(root, 1 << 20, min_free=0, clock=clock)
    await mgr.start()
    await mgr.device_report(rep("downloading", progress=40, bytesDone=4_000_000, bytesTotal=10_000_000))
    e = await devices(mgr)
    assert e["phase"] == "downloading" and e["state"] == "downloading"
    clock.now += 12 * 60  # the car sleeps
    e = await devices(mgr)
    assert e["phase"] == "paused" and e["state"] == "downloading"
    assert (e["doneBytes"], e["totalBytes"], e["ageSeconds"]) == (4_000_000, 10_000_000, 720)
    # it wakes up and says "queued" without byte counts: the progress is still shown, nothing looks failed
    await mgr.device_report(rep("queued", message="Queued until a network is available"))
    e = await devices(mgr)
    assert e["state"] == "queued" and e["phase"] == "queued" and e["doneBytes"] == 4_000_000 and e["totalBytes"] == 10_000_000
    clock.now += 600
    assert (await devices(mgr))["phase"] == "paused"
    await mgr.device_report(rep("installed", progress=100, bytesDone=10_000_000, bytesTotal=10_000_000))
    e = await devices(mgr)
    assert e["phase"] == "installed" and e["doneBytes"] == 10_000_000
    await mgr.stop()


@pytest.mark.parametrize("message,paused", [
    ("Connection lost", True), ("Server answered HTTP 503", True), ("Queued until Wi-Fi", True),
    ("Downloaded file failed the SHA-256 check", False), ("Not enough free space (900 MB needed)", False),
    ("Server answered HTTP 404", False), ("File could not be written", False),
])
async def test_network_failure_after_progress_is_not_a_failure(root, message, paused):
    make_region(root, "big-1")
    mgr = R.RegionManager(root, 1 << 20, min_free=0, clock=Clock())
    await mgr.start()
    await mgr.device_report(rep("downloading", progress=10, bytesDone=1_000_000, bytesTotal=10_000_000))
    await mgr.device_report(rep("failed", message=message))
    e = await devices(mgr)
    assert e["state"] == "failed" and (e["phase"] == "paused") is paused
    await mgr.stop()


async def test_network_failure_without_progress_stays_failed(root):
    make_region(root, "big-1")
    mgr = R.RegionManager(root, 1 << 20, min_free=0, clock=Clock())
    await mgr.start()
    await mgr.device_report(rep("failed", message="Connection lost"))
    assert (await devices(mgr))["phase"] == "failed"
    await mgr.stop()


async def test_report_accepts_paused_and_interrupted_and_still_old_apps(root):
    make_region(root, "big-1")
    mgr = R.RegionManager(root, 1 << 20, min_free=0, clock=Clock())
    await mgr.start()
    await mgr.device_report(rep("interrupted", bytesDone=5, bytesTotal=10))
    e = await devices(mgr)
    assert e["state"] == "paused" and e["phase"] == "paused" and e["doneBytes"] == 5
    await mgr.device_report(rep("paused"))
    await mgr.device_report(rep("downloading"))  # an old app that never sends the new states
    with pytest.raises(D.ReportError):
        await mgr.device_report(rep("exploding"))
    await mgr.stop()


async def test_install_request_never_expires_because_of_silence(root):
    clock = Clock()
    make_region(root, "big-1")
    mgr = R.RegionManager(root, 1 << 20, min_free=0, clock=clock)
    await mgr.start()
    await mgr.set_wanted("big-1", True)
    await mgr.device_report(rep("downloading", progress=1, bytesDone=1, bytesTotal=100))
    clock.now += D.STALE_SECONDS + 86400  # the device is forgotten after 90 days without a report ...
    assert await mgr.devices.load() == []
    await wait_hashed(mgr)
    item = (await mgr.overview())["regions"][0]
    assert item["wanted"] is True  # ... the request stays pending
    cat = json.loads(await mgr.catalogue_body())["regions"][0]
    assert cat["install"] is True and cat["installRevision"]
    await mgr.stop()


async def test_devices_endpoint_and_page_show_paused_and_per_region_bytes(root, tmp_path):
    clock = Clock()
    make_region(root, "big-1")
    mgr = R.RegionManager(root, 1 << 20, min_free=0, clock=clock)
    await mgr.start()
    await mgr.device_report(rep("downloading", progress=40, bytesDone=4_000_000, bytesTotal=10_000_000))
    clock.now += 1200
    api = await sync_client(mgr, tmp_path)
    try:
        j = await (await api.get("/api/regions/devices", headers=AUTH)).json()
        e = j["devices"][0]["regions"][0]
        assert e["phase"] == "paused" and e["doneBytes"] == 4_000_000 and e["totalBytes"] == 10_000_000 and e["ageSeconds"] == 1200
    finally:
        await api.close()
    app = web.Application()
    register_region_pages(app, mgr)
    admin = TestClient(TestServer(app))
    await admin.start_server()
    try:
        page = await (await admin.get("/regions")).text()
        assert "paused — last seen" in page and "pausiert" in page  # en and de wording, no ETA
        assert "ETA" not in page
        ov = await (await admin.get("/regions/api/list", headers={"X-Requested-With": "car-media-hub"})).json()
        assert ov["devices"][0]["regions"][0]["phase"] == "paused"
    finally:
        await admin.close()
        await mgr.stop()


# ------------------------------------------------------------------ cancel of an install request


async def cat(mgr):
    return json.loads(await mgr.catalogue_body())["regions"][0]


async def test_cancel_is_a_state_of_its_own_and_survives_a_restart(root):
    clock = Clock()
    make_region(root, "big-1")
    mgr = R.RegionManager(root, 1 << 20, min_free=0, clock=clock)
    await mgr.start()
    await wait_hashed(mgr)
    assert (await cat(mgr))["cancel"] is False
    await mgr.set_wanted("big-1", True)
    c = await cat(mgr)
    assert c["install"] is True and c["cancel"] is False and c["cancelRevision"] == ""
    first = c["installRevision"]
    clock.now += 5
    await mgr.set_wanted("big-1", False)
    c = await cat(mgr)  # immediately, no rescan wait
    assert (c["install"], c["installRevision"], c["cancel"], c["remove"]) == (False, "", True, False)
    assert c["cancelRevision"] > first and c["files"]  # files stay offered, nothing is deleted
    assert (root / "big-1" / "map.pmtiles").exists()
    await mgr.stop()

    mgr2 = R.RegionManager(root, 1 << 20, min_free=0, clock=clock)
    await mgr2.start()
    c2 = await cat(mgr2)
    assert c2["cancel"] is True and c2["cancelRevision"] == c["cancelRevision"] and c2["install"] is False
    # a new request works again and has a revision of its own, even within the same second as the cancel
    await mgr2.set_wanted("big-1", True)
    c3 = await cat(mgr2)
    assert c3["install"] is True and c3["cancel"] is False and c3["cancelRevision"] == ""
    assert c3["installRevision"] not in ("", first, c["cancelRevision"]) and c3["installRevision"] > c["cancelRevision"]
    await mgr2.stop()


async def test_cancel_without_a_request_changes_nothing_and_remove_replaces_a_cancel(root):
    clock = Clock()
    make_region(root, "big-1")
    mgr = R.RegionManager(root, 1 << 20, min_free=0, clock=clock)
    await mgr.start()
    await wait_hashed(mgr)
    await mgr.set_wanted("big-1", False)
    assert (await cat(mgr))["cancel"] is False
    await mgr.set_wanted("big-1", True)
    await mgr.set_wanted("big-1", False)
    assert (await cat(mgr))["cancel"] is True
    await mgr.request_remove("big-1")
    c = await cat(mgr)
    assert c["remove"] is True and c["cancel"] is False
    await mgr.stop()


async def test_cancelled_region_shows_as_cancelled_on_the_device(root):
    clock = Clock()
    make_region(root, "big-1")
    mgr = R.RegionManager(root, 1 << 20, min_free=0, clock=clock)
    await mgr.start()
    await mgr.set_wanted("big-1", True)
    await mgr.device_report(rep("downloading", progress=40, bytesDone=4_000_000, bytesTotal=10_000_000))
    clock.now += 30
    await mgr.set_wanted("big-1", False)
    e = await devices(mgr)
    assert e["phase"] == "cancelled" and e["cancelAcknowledged"] is False  # waiting for the next report
    clock.now += 600  # still no report: cancelled, not "paused"
    assert (await devices(mgr))["phase"] == "cancelled"
    await mgr.device_report(rep("downloading", progress=41, bytesDone=4_100_000, bytesTotal=10_000_000))  # not yet stopped
    e = await devices(mgr)
    assert e["phase"] == "cancelled" and e["cancelAcknowledged"] is True
    await mgr.device_report(rep("installed", progress=100, bytesDone=10_000_000, bytesTotal=10_000_000))
    assert (await devices(mgr))["phase"] == "installed"  # finished before the cancel arrived: it is installed
    await mgr.set_wanted("big-1", True)
    await mgr.device_report(rep("downloading", progress=1, bytesDone=1, bytesTotal=2))
    assert (await devices(mgr))["phase"] == "downloading"  # a new request: no longer cancelled
    await mgr.stop()


async def test_page_endpoint_cancel_shows_in_the_list(root):
    make_region(root, "big-1")
    mgr = R.RegionManager(root, 1 << 20, min_free=0, clock=Clock())
    await mgr.start()
    app = web.Application()
    register_region_pages(app, mgr)
    admin = TestClient(TestServer(app))
    await admin.start_server()
    try:
        for wanted in (True, False):
            r = await admin.post("/regions/api/big-1/wanted", data=json.dumps({"wanted": wanted}), headers=ADMIN)
            assert r.status == 200
        j = await (await admin.get("/regions/api/list", headers={"X-Requested-With": "car-media-hub"})).json()
        item = j["regions"][0]
        assert item["wanted"] is False and item["cancelled"] is True and item["cancelAt"].endswith("Z")
    finally:
        await admin.close()
        await mgr.stop()


# ------------------------------------------------------------------ imports: interrupted and resumed


def url_of(server, path):
    return f"http://127.0.0.1:{server.port}{path}"


@pytest.fixture
async def origin():
    data = pmtiles(400_000, b"z")
    state = {"break": True, "calls": []}

    async def serve(request):
        state["calls"].append(dict(request.headers))
        rng = request.headers.get("Range")
        start = int(rng.split("=")[1].split("-")[0]) if rng and request.headers.get("If-Range") == '"v1"' else 0
        head = {"ETag": '"v1"'}
        if start:
            head["Content-Range"] = f"bytes {start}-{len(data) - 1}/{len(data)}"
        resp = web.StreamResponse(status=206 if start else 200, headers={**head, "Content-Length": str(len(data) - start)})
        await resp.prepare(request)
        if state["break"] and not start:
            await resp.write(data[:150_000])
            await asyncio.sleep(1.5)
            request.transport.abort()
            return resp
        await resp.write(data[start:])
        return resp

    app = web.Application()
    app.router.add_get("/map", serve)
    server = TestServer(app)
    await server.start_server()
    yield server, data, state
    await server.close()


async def finished(task, timeout=15):
    await asyncio.wait_for(asyncio.gather(task.job, return_exceptions=True), timeout)
    return task


async def test_import_resumes_by_itself_after_a_lost_connection(root, origin):
    server, data, state = origin
    m = R.RegionManager(root, 1 << 24, min_free=0, import_policy=LOCAL, import_retry_delays=(0.0, 0.0))
    await m.start()
    await m.create_region("imp-1", {"name": "x"})
    task = await finished(m.start_import("imp-1", "map", url_of(server, "/map")))
    assert task.state == "done", task.error
    assert (root / "imp-1" / "map.pmtiles").read_bytes() == data and task.resumed_from > 0
    assert state["calls"][-1]["Range"].startswith("bytes=") and state["calls"][-1]["If-Range"] == '"v1"'
    assert sorted(p.name for p in (root / "imp-1").iterdir() if p.name.startswith(".import")) == []
    await m.stop()


async def test_import_interrupted_by_a_restart_is_picked_up_at_start(root, origin):
    server, data, state = origin
    m = R.RegionManager(root, 1 << 24, min_free=0, import_policy=LOCAL, import_retry_delays=())
    await m.start()
    await m.create_region("imp-1", {"name": "x"})
    task = await finished(m.start_import("imp-1", "map", url_of(server, "/map")))
    assert task.state == "failed" and (root / "imp-1" / ".import-map.part").stat().st_size >= 100_000
    meta = json.loads((root / "imp-1" / ".import-map.json").read_text())
    assert meta["auto"] is True and meta["address"] == url_of(server, "/map")
    await m.stop()
    state["break"] = False

    m2 = R.RegionManager(root, 1 << 24, min_free=0, import_policy=LOCAL, import_retry_delays=())
    await m2.start()  # the add-on starts again: the import goes on without a click
    job = next(iter(m2.tasks()))
    await finished(job)
    assert job.state == "done" and job.resumed_from >= 100_000, job.error
    assert (root / "imp-1" / "map.pmtiles").read_bytes() == data
    # the SHA-256 streamed in is stored: served at once, no second read of the file
    cache = json.loads((root / "imp-1" / "files.json").read_text())["files"]["map.pmtiles"]
    assert cache["sha256"] == hashlib.sha256(data).hexdigest()
    region = (await m2.rescan())[0]
    assert region.files["map"].sha256 == cache["sha256"]
    await m2.stop()


async def test_a_cancelled_import_is_not_restarted_by_the_next_start(root, origin):
    server, data, state = origin
    gate = asyncio.Event()

    async def slow(request):
        resp = web.StreamResponse(headers={"Content-Length": str(len(data)), "ETag": '"v1"'})
        await resp.prepare(request)
        await resp.write(data[:100_000])
        await gate.wait()
        return resp

    app = web.Application()
    app.router.add_get("/slow", slow)
    srv = TestServer(app)
    await srv.start_server()
    try:
        m = R.RegionManager(root, 1 << 24, min_free=0, import_policy=LOCAL)
        await m.start()
        await m.create_region("imp-1", {"name": "x"})
        task = m.start_import("imp-1", "map", url_of(srv, "/slow"))
        for _ in range(100):
            if task.downloaded >= 100_000:
                break
            await asyncio.sleep(0.05)
        m.cancel_task(task.id)
        await finished(task)
        assert task.state == "cancelled"
        assert json.loads((root / "imp-1" / ".import-map.json").read_text())["auto"] is False
        await m.stop()
        m2 = R.RegionManager(root, 1 << 24, min_free=0, import_policy=LOCAL)
        await m2.start()
        assert m2.tasks() == []
        await m2.stop()
    finally:
        gate.set()
        await srv.close()


async def test_add_on_stop_in_the_middle_leaves_the_import_for_the_next_start(root, origin):
    server, data, state = origin
    gate = asyncio.Event()

    async def slow(request):
        resp = web.StreamResponse(headers={"Content-Length": str(len(data)), "ETag": '"v1"'})
        await resp.prepare(request)
        await resp.write(data[:100_000])
        await gate.wait()
        return resp

    app = web.Application()
    app.router.add_get("/slow", slow)
    srv = TestServer(app)
    await srv.start_server()
    try:
        m = R.RegionManager(root, 1 << 24, min_free=0, import_policy=LOCAL)
        await m.start()
        await m.create_region("imp-1", {"name": "x"})
        task = m.start_import("imp-1", "map", url_of(srv, "/slow"))
        for _ in range(100):
            if task.downloaded >= 100_000:
                break
            await asyncio.sleep(0.05)
        await m.stop()  # the add-on is stopped (update, host reboot)
        meta = json.loads((root / "imp-1" / ".import-map.json").read_text())
        assert meta["auto"] is True and meta["synced"] == (root / "imp-1" / ".import-map.part").stat().st_size >= 100_000
        assert not (root / "imp-1" / "map.pmtiles").exists()
    finally:
        gate.set()
        await srv.close()


async def test_part_behind_the_fsynced_length_is_cut_back_before_resuming(root, origin):
    server, data, state = origin
    state["break"] = False
    m = R.RegionManager(root, 1 << 24, min_free=0, import_policy=LOCAL, import_retry_delays=())
    await m.start()
    await m.create_region("imp-1", {"name": "x"})
    d = root / "imp-1"
    (d / ".import-map.part").write_bytes(data[:90_000] + b"\0" * 30_000)  # a power cut left zeros behind what was synced
    (d / ".import-map.json").write_text(json.dumps({
        "url": hashlib.sha256(url_of(server, "/map").encode()).hexdigest(), "etag": '"v1"', "synced": 90_000}))
    task = await finished(m.start_import("imp-1", "map", url_of(server, "/map")))
    assert task.state == "done" and task.resumed_from == 90_000
    assert (d / "map.pmtiles").read_bytes() == data
    await m.stop()


async def test_two_imports_of_the_same_file_at_once_second_is_refused(root, origin):
    server, data, state = origin
    state["break"] = False
    m = R.RegionManager(root, 1 << 24, min_free=0, import_policy=LOCAL, import_retry_delays=())
    await m.start()
    await m.create_region("imp-1", {"name": "x"})
    first = m.start_import("imp-1", "map", url_of(server, "/map"))
    with pytest.raises(R.RegionError) as err:
        m.start_import("imp-1", "map", url_of(server, "/map"))
    assert err.value.status == 409
    await finished(first)
    assert first.state == "done"
    await m.stop()


async def test_half_built_region_is_never_advertised_and_old_files_stay_served(root, origin, tmp_path):
    server, data, state = origin
    gate = asyncio.Event()

    db = sqlite_bytes(300_000)

    def slow_of(blob):
        async def slow(request):
            resp = web.StreamResponse(headers={"Content-Length": str(len(blob)), "ETag": '"v1"'})
            await resp.prepare(request)
            await resp.write(blob[:100_000])
            await gate.wait()
            await resp.write(blob[100_000:])
            return resp
        return slow

    app = web.Application()
    app.router.add_get("/slow", slow_of(data))
    app.router.add_get("/slowdb", slow_of(db))
    srv = TestServer(app)
    await srv.start_server()
    d = make_region(root, "big-1")
    m = R.RegionManager(root, 1 << 24, min_free=0, import_policy=LOCAL, import_retry_delays=())
    await m.start()
    await wait_hashed(m)
    client = await sync_client(m, tmp_path)
    try:
        before = await cat(m)
        assert before["state"] == "ready" and [f["kind"] for f in before["files"]] == ["map"]
        # a new file kind is being imported: the region is "updating" with no files, not offered half built
        task = m.start_import("big-1", "places", url_of(srv, "/slowdb"))
        assert await wait_until(lambda: ("big-1", "places") in m._active)
        c = await cat(m)
        assert c["state"] == "updating" and c["files"] == [] and c["install"] is False and c["updating"] == [{"kind": "places"}]
        # the same for a wanted region: install is not offered while it is being built
        await m.set_wanted("big-1", True)
        assert (await cat(m))["install"] is False
        # an existing file that is being replaced keeps its old complete file listed and served
        replace = m.start_import("big-1", "map", url_of(srv, "/slow"))
        assert replace.state == "queued"  # imports run one at a time: it waits for the first one
        got = await client.get("/api/regions/files/big-1/map.pmtiles", headers=AUTH)
        assert got.status == 200 and await got.read() == pmtiles()
        gate.set()
        await finished(task)
        await finished(replace)
        assert task.state == "done" and replace.state == "done", (task.error, replace.error)
        c = await cat(m)
        assert c["state"] == "ready" and sorted(f["kind"] for f in c["files"]) == ["map", "places"] and c["install"] is True
    finally:
        gate.set()
        await client.close()
        await m.stop()
        await srv.close()
