"""Regions review fixes (2026-11-16): free space while streaming, routing-group names, files that are still being
written, partial import files, forgetting devices, If-Range on resume, import time limit, install/remove revisions,
SHA-256 ETag and stricter header checks."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from car_media_hub import diskspace
from car_media_hub import regions as R
from car_media_hub.config import load_config
from car_media_hub.main import make_region_manager
from car_media_hub.regions_import import ImportPolicy
from car_media_hub.regions_web import register_region_pages

from test_regions import (  # noqa: F401  (root is a fixture)
    ADMIN, AUTH, app_parse, gen, make_region, mgr, old, options, pmtiles, root, sqlite_bytes, sync_client, tar_bytes,
    wait_hashed,
)

LOCAL = ImportPolicy(require_https=False, check_hosts=False)
HDR = {"X-Requested-With": "car-media-hub"}
DEV1, DEV2 = "abcdef0123456789", "fedcba9876543210"


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def parts_of(root, region):
    return [p.name for p in (root / region).iterdir() if p.name.startswith((".upload", ".import"))]


# ------------------------------------------------------------------ H1 / M1: free space


async def test_upload_without_size_checks_the_reserve(root):
    m = R.RegionManager(root, 1 << 40, min_free=1 << 60)
    await m.start()
    await m.create_region("up-1", {"name": "x"})
    with pytest.raises(R.RegionError) as exc:  # no Content-Length (declared None)
        await m.save_upload("up-1", "map", gen(pmtiles()), None)
    assert exc.value.status == 507
    assert parts_of(root, "up-1") == [] and not m._busy
    await m.stop()


async def test_http_chunked_upload_is_refused_when_the_reserve_is_short(root):
    m = R.RegionManager(root, 1 << 40, min_free=1 << 60)
    await m.start()
    await m.create_region("up-2", {"name": "x"})
    app = web.Application()
    register_region_pages(app, m)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.put("/regions/api/up-2/upload/map", data=gen(pmtiles()), headers=HDR)  # chunked: no length
        assert resp.status == 507 and not (await resp.json())["ok"]
    finally:
        await client.close()
        await m.stop()
    assert parts_of(root, "up-2") == []


async def test_free_space_is_checked_while_streaming(root, monkeypatch):
    m = R.RegionManager(root, 1 << 40, min_free=1000)
    await m.start()
    await m.create_region("up-3", {"name": "x"})
    monkeypatch.setattr(R, "FREE_CHECK_BYTES", 5000)
    calls = []

    def fake_free(path):
        calls.append(1)
        return 1_000_000 if len(calls) <= 2 else 500  # the disk fills up while the upload runs

    monkeypatch.setattr(diskspace, "free_bytes", fake_free)
    with pytest.raises(R.RegionError) as exc:
        await m.save_upload("up-3", "map", gen(pmtiles(4096), b"x" * 4096, b"x" * 4096, b"x" * 4096), None)
    assert exc.value.status == 507 and len(calls) == 3
    assert parts_of(root, "up-3") == [] and not (root / "up-3" / "map.pmtiles").exists() and not m._busy
    await m.stop()


def test_main_passes_the_free_space_reserve_and_import_limit(tmp_path):
    cfg = load_config(options(tmp_path, min_free_space_gb=2, region_import_max_hours=1.5))
    m = make_region_manager(cfg)
    assert m.min_free == 2 * 2**30 and m.import_timeout == 5400
    default = make_region_manager(load_config(options(tmp_path)))
    assert default.min_free == 50 * 2**30 and default.import_timeout == 6 * 3600


# ------------------------------------------------------------------ M3: routing group names


async def test_routing_group_name_never_collides_with_a_regions_own_routing_file(root, mgr):
    make_region(root, "bayern", routing=True)  # no group: its own file is bayern-tiles.tar
    make_region(root, "oberpfalz", routing=True, group="bayern")
    other = root / "oberpfalz" / "routing.tar"
    other.write_bytes(tar_bytes(3000, b"other"))  # different content than bayern's file
    old(other)
    make_region(root, "regensburg", routing=False, group="bayern")
    await wait_hashed(mgr)
    doc = json.loads(await mgr.catalogue_body())
    by = {r["id"]: {f["kind"]: f for f in r["files"]} for r in doc["regions"]}
    own, grouped, member = by["bayern"]["routing"], by["oberpfalz"]["routing"], by["regensburg"]["routing"]
    assert own["name"] == "bayern-tiles.tar"
    assert grouped["name"] == f"bayern-{grouped['sha256'][:8]}-tiles.tar" and grouped["name"] != own["name"]
    assert member["name"] == grouped["name"] and member["sha256"] == grouped["sha256"] and member["url"] == grouped["url"]
    assert own["sha256"] != grouped["sha256"]
    assert len(app_parse(doc)) == 3  # the app accepts every entry (extension of its kind, plain name)
    # the same group name with other content gets another name
    prov = root / "oberpfalz" / "routing.tar"
    prov.write_bytes(tar_bytes(4000, b"third"))
    old(prov)
    await wait_hashed(mgr)
    again = {r["id"]: {f["kind"]: f for f in r["files"]} for r in json.loads(await mgr.catalogue_body())["regions"]}
    assert again["oberpfalz"]["routing"]["name"] != grouped["name"]
    assert re.fullmatch(r"bayern-[0-9a-f]{8}-tiles\.tar", again["oberpfalz"]["routing"]["name"])


def test_grouped_name_gets_a_longer_hash_on_a_clash():
    sha = "ab" * 32
    plain = R.grouped_routing_name("g", sha)
    assert plain == "g-abababab-tiles.tar"
    clash = R.grouped_routing_name("g", sha, {plain: "cd" * 32})  # a region with another file uses exactly that name
    assert clash != plain and clash.startswith("g-" + sha[:16]) and clash.endswith("-tiles.tar")
    assert R.grouped_routing_name("g", sha, {plain: sha}) == plain  # same content: the same name is fine


# ------------------------------------------------------------------ M4: files that are still written / changed


async def test_files_younger_than_30_seconds_are_not_offered(root, mgr):
    make_region(root, "young", age=20)
    make_region(root, "settled", age=40)
    mgr.invalidate()
    regions = {r.id: r for r in await mgr.rescan()}
    assert regions["young"].files["map"].settling and not regions["settled"].files["map"].settling
    entries = {r["id"]: r for r in json.loads(await mgr.catalogue_body())["regions"]}
    assert entries["young"]["state"] == "hashing" and entries["young"]["files"] == []


async def test_size_change_between_two_scans_means_settling(root, mgr):
    make_region(root, "grow")
    await wait_hashed(mgr)
    path = root / "grow" / "map.pmtiles"
    path.write_bytes(pmtiles(9000))  # a copy that is still in progress (old mtime: only the change shows it)
    old(path, 7200)
    first = {r.id: r for r in await mgr.rescan()}["grow"].files["map"]
    assert first.sha256 is None and first.settling
    second = {r.id: r for r in await mgr.rescan()}["grow"].files["map"]
    assert not second.settling  # unchanged since the previous scan: hashed now
    regions = await wait_hashed(mgr)
    assert regions[0].files["map"].sha256 == hashlib.sha256(pmtiles(9000)).hexdigest()


async def test_changed_file_is_not_served_and_etag_is_the_sha256(root, mgr, tmp_path):
    make_region(root)
    await wait_hashed(mgr)
    client = await sync_client(mgr, tmp_path)
    url = "/api/regions/files/oberpfalz/map.pmtiles"
    try:
        first = await client.get(url, headers=AUTH)
        data = pmtiles()
        assert first.status == 200 and first.headers["ETag"] == f'"{hashlib.sha256(data).hexdigest()}"'
        assert "GMT" in first.headers["Last-Modified"]
        ranged = await client.get(url, headers={**AUTH, "Range": "bytes=0-9", "If-Range": first.headers["ETag"]})
        assert ranged.status == 206
        stale_style = await client.get(url, headers={**AUTH, "Range": "bytes=0-9", "If-Range": f'"{len(data):x}-1"'})
        assert stale_style.status == 200  # the old size-mtime validator no longer matches
        path = root / "oberpfalz" / "map.pmtiles"
        path.write_bytes(pmtiles(5000, b"y"))  # replaced (a Samba copy just finished or is still running)
        blocked = await client.get(url, headers=AUTH)
        assert blocked.status == 503 and "Retry-After" in blocked.headers
        assert (await client.head(url, headers=AUTH)).status == 503
        old(path)
        await wait_hashed(mgr)
        fresh = await client.get(url, headers=AUTH)
        new = pmtiles(5000, b"y")
        assert fresh.status == 200 and await fresh.read() == new
        assert fresh.headers["ETag"] == f'"{hashlib.sha256(new).hexdigest()}"' != first.headers["ETag"]
        # same size and mtime but other content in the cache: never served (hash does not describe the file)
        cache = json.loads((root / "oberpfalz" / "files.json").read_text())
        assert cache["files"]["map.pmtiles"]["size"] == len(new)
    finally:
        await client.close()


async def test_a_file_that_is_still_hashing_is_not_served(root, tmp_path):
    m = R.RegionManager(root, 1 << 20, min_free=0)
    await m.start()
    m._hasher.stop()  # no worker: the hash stays unknown
    make_region(root)
    m.invalidate()
    client = await sync_client(m, tmp_path)
    try:
        assert (await client.get("/api/regions/files/oberpfalz/map.pmtiles", headers=AUTH)).status == 503
    finally:
        await client.close()
        await m.stop()


# ------------------------------------------------------------------ M2: partial files


async def test_stale_partial_files_expire_at_start_and_on_rescan(root):
    region = root / "p-1"
    region.mkdir(parents=True)
    long_ago = time.time() - 25 * 3600

    def put(name, age=None):
        path = region / name
        path.write_bytes(b"x" * 100)
        if age:
            os.utime(path, (age, age))

    put(".import-map.part", long_ago)
    put(".import-map.json", long_ago)
    put(".upload-places-abcd1234.part", long_ago)
    put(".import-routing.part")  # fresh: stays
    put(".import-routing.json")
    m = R.RegionManager(root, 1 << 20, min_free=0)
    await m.start()  # cleanup at start
    assert sorted(parts_of(root, "p-1")) == [".import-routing.json", ".import-routing.part"]
    put(".import-places.part", long_ago)  # appears later; a running transfer is never touched
    m._busy.add(("p-1", "places"))
    await m.rescan()
    assert (region / ".import-places.part").exists()
    m._busy.discard(("p-1", "places"))
    await m.rescan()  # cleanup on rescan
    assert not (region / ".import-places.part").exists()
    await m.stop()


async def test_partial_imports_are_listed_and_can_be_discarded(root, mgr):
    await mgr.create_region("p-2", {"name": "x"})
    (root / "p-2" / ".import-map.part").write_bytes(b"x" * 1234)
    (root / "p-2" / ".import-map.json").write_text("{}")
    mgr.invalidate()
    item = (await mgr.overview())["regions"][0]
    assert item["parts"] == [{"kind": "map", "origin": "import", "size": 1234, "ageSeconds": item["parts"][0]["ageSeconds"], "running": False}]
    mgr._busy.add(("p-2", "map"))
    with pytest.raises(R.RegionError) as busy:
        await mgr.discard_part("p-2", "map")
    assert busy.value.status == 409
    mgr._busy.clear()
    admin_app = web.Application()
    register_region_pages(admin_app, mgr)
    client = TestClient(TestServer(admin_app))
    await client.start_server()
    try:
        assert (await client.post("/regions/api/p-2/parts/map/discard", data="{}")).status == 400  # needs the page header
        ok = await client.post("/regions/api/p-2/parts/map/discard", data="{}", headers=ADMIN)
        assert ok.status == 200 and parts_of(root, "p-2") == []
        assert (await client.post("/regions/api/p-2/parts/map/discard", data="{}", headers=ADMIN)).status == 404
    finally:
        await client.close()


# ------------------------------------------------------------------ imports: free space, validators, time limit


@pytest.fixture
async def origin():
    data = pmtiles(300_000)
    calls = []

    async def chunked(request):  # no Content-Length
        resp = web.StreamResponse()
        resp.enable_chunked_encoding()
        await resp.prepare(request)
        for i in range(0, len(data), 50_000):
            await resp.write(data[i:i + 50_000])
        await resp.write_eof()
        return resp

    async def noetag(request):
        calls.append(dict(request.headers))
        return web.Response(body=data)

    async def changed(request):  # a 206 whose ETag differs from the first try
        calls.append(dict(request.headers))
        rng = request.headers.get("Range")
        if rng:
            start = int(rng.split("=")[1].split("-")[0])
            return web.Response(status=206, body=data[start:], headers={
                "ETag": '"v2"', "Content-Range": f"bytes {start}-{len(data) - 1}/{len(data)}"})
        return web.Response(body=data, headers={"ETag": '"v2"'})

    async def slow(request):
        resp = web.StreamResponse(headers={"Content-Length": str(len(data))})
        await resp.prepare(request)
        await resp.write(data[:100_000])
        await asyncio.sleep(10)
        return resp

    app = web.Application()
    app.router.add_get("/chunked", chunked)
    app.router.add_get("/noetag", noetag)
    app.router.add_get("/changed", changed)
    app.router.add_get("/slow", slow)
    server = TestServer(app)
    await server.start_server()
    yield server, data, calls
    await server.close()


def url_of(server, path):
    return f"http://127.0.0.1:{server.port}{path}"


async def run_task(task, timeout=15):
    await asyncio.wait_for(asyncio.gather(task.job, return_exceptions=True), timeout)
    return task


def seed_part(root, server, path, meta_extra, size=1000):
    d = root / "imp-1"
    (d / ".import-map.part").write_bytes(pmtiles(size))
    meta = {"url": hashlib.sha256(url_of(server, path).encode()).hexdigest(), **meta_extra}
    (d / ".import-map.json").write_text(json.dumps(meta))


async def test_import_without_length_keeps_the_reserve(root, origin):
    server, data, _ = origin
    m = R.RegionManager(root, 1 << 30, min_free=1 << 60, import_policy=LOCAL)
    await m.start()
    await m.create_region("imp-1", {"name": "x"})
    task = await run_task(m.start_import("imp-1", "map", url_of(server, "/chunked")))
    assert task.state == "failed" and "free space" in task.error
    assert parts_of(root, "imp-1") == [] and not (root / "imp-1" / "map.pmtiles").exists()
    await m.stop()


async def test_import_streaming_free_space_check_discards_the_part(root, origin, monkeypatch):
    server, data, _ = origin
    m = R.RegionManager(root, 1 << 30, min_free=1000, import_policy=LOCAL)
    await m.start()
    await m.create_region("imp-1", {"name": "x"})
    monkeypatch.setattr(R, "FREE_CHECK_BYTES", 20_000)
    calls = []

    def fake_free(path):
        calls.append(1)
        return 10_000_000 if len(calls) <= 1 else 0  # full after the start

    monkeypatch.setattr(diskspace, "free_bytes", fake_free)
    task = await run_task(m.start_import("imp-1", "map", url_of(server, "/chunked")))
    assert task.state == "failed" and "deleted" in task.error and len(calls) >= 2
    assert parts_of(root, "imp-1") == [] and not m._busy  # nothing kept: the disk would stay full
    await m.stop()


async def test_resume_without_a_validator_restarts_at_zero(root, origin):
    server, data, calls = origin
    m = R.RegionManager(root, 1 << 30, min_free=0, import_policy=LOCAL)
    await m.start()
    await m.create_region("imp-1", {"name": "x"})
    seed_part(root, server, "/noetag", {})  # no ETag / Last-Modified known
    task = await run_task(m.start_import("imp-1", "map", url_of(server, "/noetag")))
    assert task.state == "done", task.error
    assert "Range" not in calls[0] and task.resumed_from == 0
    assert (root / "imp-1" / "map.pmtiles").read_bytes() == data
    await m.stop()


async def test_resume_restarts_when_the_server_cannot_confirm_the_validator(root, origin):
    server, data, calls = origin
    m = R.RegionManager(root, 1 << 30, min_free=0, import_policy=LOCAL)
    await m.start()
    await m.create_region("imp-1", {"name": "x"})
    seed_part(root, server, "/changed", {"etag": '"v1"'})
    task = await run_task(m.start_import("imp-1", "map", url_of(server, "/changed")))
    assert task.state == "done", task.error
    assert calls[0]["If-Range"] == '"v1"' and "Range" not in calls[1]  # answered 206 with another ETag: start again
    assert (root / "imp-1" / "map.pmtiles").read_bytes() == data  # not spliced
    await m.stop()


async def test_import_time_limit(root, origin):
    server, data, _ = origin
    m = R.RegionManager(root, 1 << 30, min_free=0, import_policy=LOCAL, import_timeout=0.7)
    await m.start()
    await m.create_region("imp-1", {"name": "x"})
    task = await run_task(m.start_import("imp-1", "map", url_of(server, "/slow")))
    assert task.state == "failed" and "time limit" in task.error
    assert (root / "imp-1" / ".import-map.part").exists()  # kept: the import resumes
    assert not m._busy
    await m.stop()


# ------------------------------------------------------------------ L1: forget devices


def report(dev, name="Car"):
    return {"deviceId": dev, "name": name, "appVersion": "1", "regions": [{"id": "oberpfalz", "state": "installed", "version": "1", "progress": 100}]}


async def test_forget_device_and_forget_all(root, mgr):
    await mgr.device_report(report(DEV1))
    await mgr.device_report(report(DEV2, "Phone"))
    app = web.Application()
    register_region_pages(app, mgr)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        url = f"/regions/api/devices/{DEV1}/forget"
        assert (await client.post(url, data="{}")).status == 400  # no page header
        assert (await client.post(url, data="{}", headers={**ADMIN, "Sec-Fetch-Site": "cross-site"})).status == 403
        assert (await client.post(url, data="{}", headers={"X-Requested-With": "car-media-hub"})).status == 400  # JSON needed
        assert (await client.post(url, data="{}", headers=ADMIN)).status == 200
        assert [d["deviceId"] for d in await mgr.devices.public_list()] == [DEV2]
        assert (await client.post(url, data="{}", headers=ADMIN)).status == 404
        assert (await client.post("/regions/api/devices/bad/forget", data="{}", headers=ADMIN)).status == 400
        assert (await client.post("/regions/api/devices/forget-all", data="{}")).status == 400
        done = await client.post("/regions/api/devices/forget-all", data="{}", headers=ADMIN)
        assert done.status == 200 and (await done.json())["forgotten"] == 1
        assert await mgr.devices.public_list() == []
    finally:
        await client.close()


async def test_a_forgotten_device_no_longer_blocks_a_removal(root):
    clock = Clock()
    m = R.RegionManager(root, 1 << 20, min_free=0, clock=clock)
    await m.start()
    make_region(root, "oberpfalz")
    await wait_hashed(m)
    await m.device_report({**report(DEV1), "regions": [{"id": "oberpfalz", "state": "installed", "version": "1", "progress": 100}]})
    await m.request_remove("oberpfalz")
    clock.t += 5
    await m.device_report({**report(DEV2), "regions": [{"id": "oberpfalz", "state": "absent"}]})
    assert (await m.overview())["regions"][0]["removeRequested"]  # DEV1 (a reinstalled app) never confirmed
    await m.forget_device(DEV1)
    assert not (await m.overview())["regions"][0]["removeRequested"]
    await m.stop()


async def test_devices_unseen_for_90_days_are_dropped(root):
    clock = Clock()
    m = R.RegionManager(root, 1 << 20, min_free=0, clock=clock)
    await m.start()
    await m.device_report(report(DEV1))
    clock.t += 91 * 86400
    assert await m.devices.public_list() == []
    await m.stop()


# ------------------------------------------------------------------ catalogue revisions


async def test_install_and_remove_revisions(root):
    clock = Clock()
    m = R.RegionManager(root, 1 << 20, min_free=0, clock=clock)
    await m.start()
    make_region(root, "oberpfalz")
    await wait_hashed(m)

    async def entry():
        return {r["id"]: r for r in json.loads(await m.catalogue_body())["regions"]}["oberpfalz"]

    reg = await entry()
    assert reg["installRevision"] == "" and reg["removeRevision"] == "" and reg["install"] is False
    await m.set_wanted("oberpfalz", True)
    first = (await entry())["installRevision"]
    assert first and first == (await entry())["installRevision"] and (await entry())["install"] is True
    clock.t += 30
    await m.set_wanted("oberpfalz", False)  # the owner cancels
    assert (await entry())["installRevision"] == "" and (await entry())["install"] is False
    clock.t += 30
    await m.set_wanted("oberpfalz", True)  # and clicks Install again: a NEW request
    second = (await entry())["installRevision"]
    assert second and second != first
    clock.t += 30
    await m.request_remove("oberpfalz")
    reg = await entry()
    assert reg["removeRevision"] and reg["remove"] is True and reg["installRevision"] == ""
    removal = reg["removeRevision"]
    clock.t += 30
    await m.request_remove("oberpfalz")
    assert (await entry())["removeRevision"] != removal
    await m.stop()


# ------------------------------------------------------------------ header checks, config


@pytest.mark.parametrize("kind,blob,ok", [
    ("map", pmtiles(126), False), ("map", pmtiles(127), True),
    ("places", b"SQLite format 3\x00" + (1000).to_bytes(2, "big") + b"\x00" * 200, False),
    ("places", b"SQLite format 3\x00" + (512).to_bytes(2, "big") + b"\x00" * 200, True),
    ("places", b"SQLite format 3\x00" + (1).to_bytes(2, "big") + b"\x00" * 200, True),
    ("places", b"SQLite format 3\x00" + (4096).to_bytes(2, "big") + b"\x00" * 10, False),
    ("routing", tar_bytes(), True),
    ("routing", tar_bytes()[:148] + b"0000001\x00" + tar_bytes()[156:], False),
])
def test_head_checks_beyond_the_magic_bytes(kind, blob, ok):
    head = blob[: R.head_bytes_needed(kind)]
    if ok:
        R.validate_head(kind, head, len(blob))
    else:
        with pytest.raises(R.RegionError):
            R.validate_head(kind, head, len(blob))


@pytest.mark.parametrize("value,ok", [
    ("/share/regions", True), ("/media/maps/regions", True), ("/data/regions", True), ("/data/regions/maps", True),
    ("/data/maps", False), ("/data", False), ("/addon_config/regions", False), ("/data/regionsx", False),
])
def test_region_parents(tmp_path, value, ok):
    if ok:
        assert load_config(options(tmp_path, region_storage_dir=value)).region_storage_dir
    else:
        with pytest.raises(ValueError):
            load_config(options(tmp_path, region_storage_dir=value))


@pytest.mark.parametrize("value", [-1, 721, "x", True])
def test_import_limit_option_is_validated(tmp_path, value):
    with pytest.raises(ValueError):
        load_config(options(tmp_path, region_import_max_hours=value))
