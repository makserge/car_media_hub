"""Regions: install/remove wishes for the car app (catalogue fields), the device report endpoint and the page API."""
from __future__ import annotations

import asyncio
import json
import os

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from car_media_hub import regions as R
from car_media_hub import regions_devices as D
from car_media_hub.regions_web import register_region_pages
from car_media_hub.sync import LibraryIndex, create_sync_app

from test_regions import ADMIN, AUTH, TOKEN, State, make_region, wait_hashed  # noqa: F401

DEV = "abcdef0123456789"
JSON_H = {"Content-Type": "application/json"}


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def root(tmp_path):
    return tmp_path / "regions"


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
async def mgr(root, clock):
    m = R.RegionManager(root, 1024 * 1024, min_free=0, clock=clock)
    await m.start()
    yield m
    await m.stop()


@pytest.fixture
async def api(mgr, tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir(exist_ok=True)
    app = create_sync_app(LibraryIndex(lib, State()), TOKEN, regions=mgr)
    client = TestClient(TestServer(app))
    await client.start_server()
    yield client
    await client.close()


@pytest.fixture
async def admin(mgr):
    app = web.Application()
    register_region_pages(app, mgr)
    client = TestClient(TestServer(app))
    await client.start_server()
    yield client
    await client.close()


def body(deviceId=DEV, regions=None, **extra):
    doc = {"deviceId": deviceId, "name": "Car box", "appVersion": "1.2.3",
           "regions": regions if regions is not None else [{"id": "oberpfalz", "state": "installed", "version": "2026-11", "progress": 100}]}
    doc.update(extra)
    return doc


async def post_report(api, doc, headers=None, raw=None):
    h = {**AUTH, **JSON_H, **(headers or {})}
    return await api.post("/api/regions/devices/report", data=raw if raw is not None else json.dumps(doc), headers=h)


async def catalogue(api):
    return {r["id"]: r for r in (await (await api.get("/api/regions/catalogue", headers=AUTH)).json())["regions"]}


async def cat(mgr):
    return {r["id"]: r for r in json.loads(await mgr.catalogue_body())["regions"]}


def no_devices(root):
    return not (root / ".devices").exists() or not os.listdir(root / ".devices")


# ------------------------------------------------------------------ wanted / remove state and the catalogue


async def test_catalogue_has_install_and_remove_booleans(root, mgr, api):
    make_region(root, "oberpfalz")
    await wait_hashed(mgr)
    reg = (await catalogue(api))["oberpfalz"]
    assert reg["install"] is False and reg["remove"] is False
    await mgr.set_wanted("oberpfalz", True, "tester")
    reg = (await catalogue(api))["oberpfalz"]
    assert reg["install"] is True and reg["remove"] is False
    await mgr.request_remove("oberpfalz")
    reg = (await catalogue(api))["oberpfalz"]
    assert reg["install"] is False and reg["remove"] is True
    await mgr.set_wanted("oberpfalz", True)  # wanting it again cancels the removal
    reg = (await catalogue(api))["oberpfalz"]
    assert reg["install"] is True and reg["remove"] is False


async def test_state_file_and_overview(root, mgr):
    make_region(root, "oberpfalz")
    await wait_hashed(mgr)
    await mgr.set_wanted("oberpfalz", True, "ctrl\x01 note")
    doc = json.loads((root / "oberpfalz" / "want.json").read_text())
    assert doc["wanted"] is True and doc["wantedBy"] == "ctrl note" and doc["wantedAt"].endswith("Z")
    item = (await mgr.overview())["regions"][0]
    assert item["wanted"] is True and item["ready"] is True and item["removeRequested"] is False
    await mgr.set_wanted("oberpfalz", False)
    assert not (await mgr.overview())["regions"][0]["wanted"]


async def test_want_does_not_change_the_catalogue_version_or_survive_manifest_edit(root, mgr):
    make_region(root, "oberpfalz", manifest=False)
    await wait_hashed(mgr)
    before = (await cat(mgr))["oberpfalz"]["updatedAt"]
    await mgr.set_wanted("oberpfalz", True)
    after = (await cat(mgr))["oberpfalz"]
    assert after["updatedAt"] == before and after["install"] is True
    await mgr.update_manifest("oberpfalz", {"name": "Neu"})
    assert (await cat(mgr))["oberpfalz"]["install"] is True


async def test_hashing_region_is_wanted_but_not_offered(root, mgr):
    make_region(root, "oberpfalz", age=0)  # fresh file: still settling
    await mgr.set_wanted("oberpfalz", True)
    reg = (await cat(mgr))["oberpfalz"]
    assert reg["state"] == "hashing" and reg["install"] is False
    assert (await mgr.overview())["regions"][0]["wanted"] is True
    os.utime(root / "oberpfalz" / "map.pmtiles", (1, 1))
    await wait_hashed(mgr)
    assert (await cat(mgr))["oberpfalz"]["install"] is True


async def test_wanted_validation_and_unknown_region(root, mgr):
    make_region(root, "oberpfalz")
    for bad in ("yes", 1, None, [], {}):
        with pytest.raises(R.RegionError):
            await mgr.set_wanted("oberpfalz", bad)
    with pytest.raises(R.RegionError) as e:
        await mgr.set_wanted("nope-1", True)
    assert e.value.status == 404
    with pytest.raises(R.RegionError):
        await mgr.set_wanted("../etc", True)
    with pytest.raises(R.RegionError) as e:
        await mgr.request_remove("nope-1")
    assert e.value.status == 404


async def test_corrupt_want_file_means_not_wanted(root, mgr):
    d = make_region(root, "oberpfalz")
    (d / "want.json").write_text('{"wanted": "true", "removeRequested": 1}')
    await wait_hashed(mgr)
    reg = (await cat(mgr))["oberpfalz"]
    assert reg["install"] is False and reg["remove"] is False
    (d / "want.json").write_bytes(b"\xff\x00not json")
    mgr.invalidate()
    assert (await cat(mgr))["oberpfalz"]["install"] is False


async def test_remove_request_expires_after_30_days(root, mgr, clock):
    make_region(root, "oberpfalz")
    await wait_hashed(mgr)
    await mgr.request_remove("oberpfalz")
    assert (await cat(mgr))["oberpfalz"]["remove"] is True
    clock.t += R.REMOVE_KEEP_SECONDS + 10
    mgr.invalidate()
    assert (await cat(mgr))["oberpfalz"]["remove"] is False


async def test_remove_is_cleared_when_every_device_reports_absent(root, mgr, clock):
    make_region(root, "oberpfalz")
    await wait_hashed(mgr)
    await mgr.device_report(body(DEV, [{"id": "oberpfalz", "state": "installed"}]))
    await mgr.device_report(body("second-device-1", [{"id": "oberpfalz", "state": "installed"}]))
    clock.t += 5
    await mgr.request_remove("oberpfalz")
    clock.t += 5
    await mgr.device_report(body(DEV, [{"id": "oberpfalz", "state": "absent"}]))
    assert (await cat(mgr))["oberpfalz"]["remove"] is True  # the second device still has it
    clock.t += 5
    await mgr.device_report(body("second-device-1", [{"id": "oberpfalz", "state": "removed"}]))
    assert (await cat(mgr))["oberpfalz"]["remove"] is False
    assert (await mgr.overview())["regions"][0]["removeRequested"] is False


async def test_old_absent_report_does_not_confirm_a_new_removal(root, mgr, clock):
    make_region(root, "oberpfalz")
    await wait_hashed(mgr)
    await mgr.device_report(body(DEV, [{"id": "oberpfalz", "state": "absent"}]))
    clock.t += 5
    await mgr.request_remove("oberpfalz")
    await mgr.device_report(body("other-device-1", [{"id": "oberpfalz", "state": "absent"}]))
    assert (await cat(mgr))["oberpfalz"]["remove"] is True  # DEV has not reported since the request


async def test_concurrent_wishes_leave_valid_json(root, mgr):
    make_region(root, "oberpfalz")
    await wait_hashed(mgr)
    await asyncio.gather(*[mgr.set_wanted("oberpfalz", i % 2 == 0, f"n{i}") for i in range(40)],
                         *[mgr.request_remove("oberpfalz") for _ in range(5)])
    doc = json.loads((root / "oberpfalz" / "want.json").read_text())
    assert isinstance(doc["wanted"], bool)
    assert not [n for n in os.listdir(root / "oberpfalz") if n.endswith(".tmp")]


async def test_delete_region_removes_want(root, mgr):
    make_region(root, "oberpfalz")
    await mgr.set_wanted("oberpfalz", True)
    await mgr.delete_region("oberpfalz")
    assert not (root / "oberpfalz").exists()


# ------------------------------------------------------------------ report endpoint


async def test_report_needs_token_and_json(api):
    assert (await api.post("/api/regions/devices/report", data=json.dumps(body()), headers=JSON_H)).status == 401
    assert (await post_report(api, None, headers={"Content-Type": "text/plain"}, raw="x")).status == 415
    assert (await api.get("/api/regions/devices")).status == 401
    assert (await api.put("/api/regions/devices/report", data="{}", headers=AUTH)).status == 405
    assert (await api.post("/api/regions/other", data="{}", headers=AUTH)).status == 405


async def test_report_roundtrip_and_devices_list(root, mgr, api, clock):
    resp = await post_report(api, body(regions=[
        {"id": "oberpfalz", "state": "downloading", "version": "2026-11", "progress": 43.5, "message": "x" * 500,
         "bytesDone": 10, "bytesTotal": 100},
        {"id": "franken", "state": "absent"}], name="  Car\x00 box  " + "n" * 100))
    assert resp.status == 200 and await resp.json() == {"ok": True}
    stored = json.loads((root / ".devices" / f"{DEV}.json").read_text())
    assert stored["deviceId"] == DEV and len(stored["name"]) == 60 and "\x00" not in stored["name"]
    assert stored["regions"]["oberpfalz"]["message"] == "x" * 200
    clock.t += 600
    listing = await (await api.get("/api/regions/devices", headers=AUTH)).json()
    (dev,) = listing["devices"]
    assert dev["deviceId"] == DEV and dev["ageSeconds"] == 600 and dev["appVersion"] == "1.2.3"
    assert [r["id"] for r in dev["regions"]] == ["franken", "oberpfalz"]
    ober = dev["regions"][1]
    assert ober["state"] == "downloading" and ober["progress"] == 43.5 and ober["bytesDone"] == 10 and ober["bytesTotal"] == 100
    # a second report replaces the first one
    assert (await post_report(api, body(regions=[]))).status == 200
    (dev,) = (await (await api.get("/api/regions/devices", headers=AUTH)).json())["devices"]
    assert dev["regions"] == []
    assert not [n for n in os.listdir(root / ".devices") if n.endswith(".tmp")]


async def test_devices_folder_is_not_a_region(root, mgr, api):
    make_region(root, "oberpfalz")
    await post_report(api, body())
    await wait_hashed(mgr)
    assert [r.id for r in (await mgr.rescan())] == ["oberpfalz"]


BAD_REPORTS = [
    [], "x", 5, None,
    {"name": "x"},
    {"deviceId": "short"},
    {"deviceId": "a" * 65},
    {"deviceId": "bad/../../id01"},
    {"deviceId": "has space id01"},
    {"deviceId": 12345678},
    {"deviceId": DEV, "regions": "oberpfalz"},
    {"deviceId": DEV, "regions": ["oberpfalz"]},
    {"deviceId": DEV, "name": 5},
    {"deviceId": DEV, "name": ["a"]},
    {"deviceId": DEV, "appVersion": {"a": 1}},
    {"deviceId": DEV, "regions": [{"id": "../x", "state": "installed"}]},
    {"deviceId": DEV, "regions": [{"id": "Ober", "state": "installed"}]},
    {"deviceId": DEV, "regions": [{"id": "oberpfalz", "state": "weird"}]},
    {"deviceId": DEV, "regions": [{"id": "oberpfalz"}]},
    {"deviceId": DEV, "regions": [{"id": "oberpfalz", "state": "installed", "progress": "50"}]},
    {"deviceId": DEV, "regions": [{"id": "oberpfalz", "state": "installed", "progress": True}]},
    {"deviceId": DEV, "regions": [{"id": "oberpfalz", "state": "installed", "bytesDone": "5"}]},
    {"deviceId": DEV, "regions": [{"id": "oberpfalz", "state": "installed", "bytesTotal": 1.5}]},
    {"deviceId": DEV, "regions": [{"id": "oberpfalz", "state": "installed", "message": 7}]},
    {"deviceId": DEV, "regions": [{"id": "oberpfalz", "state": "installed", "version": []}]},
]


@pytest.mark.parametrize("doc", BAD_REPORTS)
async def test_report_validation_rejects(root, api, doc):
    resp = await post_report(api, doc)
    assert resp.status == 400, await resp.text()
    assert no_devices(root)


async def test_report_sanitises_numbers(root, api):
    resp = await post_report(api, body(regions=[
        {"id": "oberpfalz", "state": "downloading", "progress": 1e9, "bytesDone": -5, "bytesTotal": 10 ** 30}]))
    assert resp.status == 200
    entry = json.loads((root / ".devices" / f"{DEV}.json").read_text())["regions"]["oberpfalz"]
    assert entry["progress"] == 100.0 and entry["bytesDone"] == 0 and entry["bytesTotal"] == 1 << 50
    nan = '{"deviceId":"%s","regions":[{"id":"oberpfalz","state":"queued","progress":NaN}]}' % DEV
    assert (await post_report(api, None, raw=nan)).status == 400


async def test_report_oversize_and_garbage(root, api):
    big = body(regions=[{"id": "oberpfalz", "state": "failed", "message": "m" * 190} for _ in range(60)])
    assert (await post_report(api, big)).status == 413
    assert (await post_report(api, None, raw=b"x" * 9000)).status == 413
    assert (await post_report(api, None, raw="{not json")).status == 400
    assert (await post_report(api, None, raw=b"\xff\xfe")).status == 400
    assert (await post_report(api, None, raw="[" * 5000)).status == 400
    assert no_devices(root)


async def test_chunked_oversize_body_is_refused(api):
    async def gen():
        for _ in range(10):
            yield b" " * 1024

    resp = await api.post("/api/regions/devices/report", data=gen(), headers={**AUTH, **JSON_H})
    assert resp.status == 413


async def test_too_many_regions_in_one_report(api):
    regions = [{"id": f"r-{i:03d}", "state": "absent"} for i in range(101)]
    resp = await post_report(api, None, raw=json.dumps({"deviceId": DEV, "regions": regions}, separators=(",", ":")))
    assert resp.status in (400, 413)


async def test_device_cap_and_stale_cleanup(root, mgr, api, clock):
    for i in range(D.MAX_DEVICES):
        assert (await post_report(api, body(f"device-{i:04d}", []))).status == 200
    assert (await post_report(api, body("device-new-01", []))).status == 429
    assert (await post_report(api, body("device-0003", [{"id": "oberpfalz", "state": "queued"}]))).status == 200  # a known one may report
    clock.t += D.STALE_SECONDS - 10
    assert (await post_report(api, body("device-0003", []))).status == 200  # keeps one alive
    clock.t += 20
    assert (await post_report(api, body("device-new-01", []))).status == 200  # the others are older than 90 days: dropped
    assert sorted(os.listdir(root / ".devices")) == ["device-0003.json", "device-new-01.json"]
    listing = (await (await api.get("/api/regions/devices", headers=AUTH)).json())["devices"]
    assert {d["deviceId"] for d in listing} == {"device-0003", "device-new-01"}


async def test_stale_devices_are_hidden_from_the_list(mgr, clock):
    await mgr.device_report(body(DEV, []))
    clock.t += D.STALE_SECONDS + 1
    assert await mgr.devices.public_list() == []


async def test_damaged_device_files_are_ignored(root, mgr):
    await mgr.device_report(body(DEV, []))
    (root / ".devices" / "broken-device-1.json").write_text("{nope")
    (root / ".devices" / "wrongname-id-01.json").write_text(json.dumps({"deviceId": "other-device-1", "lastSeenTs": 1.8e9}))
    (root / ".devices" / "note.txt").write_text("x")
    assert [d["deviceId"] for d in await mgr.devices.public_list()] == [DEV]


async def test_concurrent_reports_are_atomic(root, mgr, api):
    docs = [body(f"device-{i % 5:04d}", [{"id": "oberpfalz", "state": "downloading", "progress": i % 100}]) for i in range(60)]
    results = await asyncio.gather(*[post_report(api, d) for d in docs])
    assert {r.status for r in results} == {200}
    files = os.listdir(root / ".devices")
    assert sorted(files) == [f"device-{i:04d}.json" for i in range(5)]
    for name in files:
        json.loads((root / ".devices" / name).read_text())


# ------------------------------------------------------------------ the page API


async def test_page_wanted_and_remove_endpoints(root, mgr, admin):
    make_region(root, "oberpfalz")
    await wait_hashed(mgr)
    ok = await admin.post("/regions/api/oberpfalz/wanted", data=json.dumps({"wanted": True}), headers=ADMIN)
    assert ok.status == 200 and (await ok.json())["wanted"] is True
    j = await (await admin.get("/regions/api/list")).json()
    assert j["regions"][0]["wanted"] is True and j["devices"] == []
    assert (await admin.post("/regions/api/oberpfalz/wanted", data=json.dumps({"wanted": "yes"}), headers=ADMIN)).status == 400
    assert (await admin.post("/regions/api/oberpfalz/wanted", data="{}", headers=ADMIN)).status == 400
    assert (await admin.post("/regions/api/nope-1/wanted", data=json.dumps({"wanted": True}), headers=ADMIN)).status == 404
    rm = await admin.post("/regions/api/oberpfalz/remove", data="{}", headers=ADMIN)
    assert rm.status == 200 and (await rm.json())["removeRequested"] is True
    j = await (await admin.get("/regions/api/list")).json()
    assert j["regions"][0]["removeRequested"] is True and j["regions"][0]["wanted"] is False
    assert (await admin.post("/regions/api/nope-1/remove", data="{}", headers=ADMIN)).status == 404


@pytest.mark.parametrize("path,payload", [("/regions/api/oberpfalz/wanted", {"wanted": True}), ("/regions/api/oberpfalz/remove", {})])
async def test_page_wanted_and_remove_are_csrf_protected(root, mgr, admin, path, payload):
    make_region(root, "oberpfalz")
    data = json.dumps(payload)
    assert (await admin.post(path, data=data, headers=JSON_H)).status == 400
    assert (await admin.post(path, data=data, headers={"X-Requested-With": "car-media-hub", "Content-Type": "text/plain"})).status == 400
    assert (await admin.post(path, data=data, headers={**ADMIN, "Sec-Fetch-Site": "cross-site"})).status == 403
    assert (await admin.post(path, data=data, headers={**ADMIN, "Origin": "https://evil.example"})).status == 403
    assert not (root / "oberpfalz" / "want.json").exists()


async def test_page_shows_devices_and_has_the_buttons(root, mgr, admin):
    make_region(root, "oberpfalz")
    await mgr.device_report(body(DEV, [{"id": "oberpfalz", "state": "installed", "version": "2026-11"}]))
    j = await (await admin.get("/regions/api/list")).json()
    assert j["devices"][0]["name"] == "Car box" and j["devices"][0]["regions"][0]["state"] == "installed"
    text = await (await admin.get("/regions")).text()
    for needle in ("Install on car app", "Cancel install", "Remove from car app", "Car app devices", "5000", "30000"):
        assert needle in text
    assert TOKEN not in text and "Authorization" not in text
