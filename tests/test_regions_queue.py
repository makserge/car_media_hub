"""One region at a time: the install queue (order of the requests in the catalogue, per-device running/queued view)
and add-on imports that run one after the other from a queue file that survives a restart."""
from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from car_media_hub import regions as R
from car_media_hub.regions_web import register_region_pages
from test_regions import ADMIN, make_region, pmtiles, root, sqlite_bytes, wait_hashed  # noqa: F401  (root is a fixture)
from test_regions_resume import Clock, DEV, finished, new_mgr, url_of


async def orders(mgr):
    return {r["id"]: r["installOrder"] for r in json.loads(await mgr.catalogue_body())["regions"]}


def four_regions(root):
    for rid in ("r-a", "r-b", "r-c", "r-d"):
        make_region(root, rid)


async def test_install_requests_are_kept_in_the_order_they_were_made(root):
    clock = Clock()
    four_regions(root)
    mgr = R.RegionManager(root, 1 << 20, min_free=0, clock=clock)
    await mgr.start()
    await wait_hashed(mgr)
    assert await orders(mgr) == {"r-a": 0, "r-b": 0, "r-c": 0, "r-d": 0}
    assert await mgr.set_wanted_many(["r-c", "r-a", "r-b"]) == ["r-c", "r-a", "r-b"]  # all accepted at once, this order
    await mgr.set_wanted("r-d", True)
    assert await orders(mgr) == {"r-c": 1, "r-a": 2, "r-b": 3, "r-d": 4}
    await mgr.set_wanted("r-a", True)  # asking again keeps the place (and gives a fresh revision)
    assert (await orders(mgr))["r-a"] == 2
    await mgr.set_wanted("r-a", False)  # cancelled: the others move up
    assert await orders(mgr) == {"r-c": 1, "r-a": 0, "r-b": 2, "r-d": 3}
    clock.now += 1
    await mgr.set_wanted("r-a", True)  # a new request goes to the end of the queue
    assert await orders(mgr) == {"r-c": 1, "r-b": 2, "r-d": 3, "r-a": 4}
    await mgr.stop()

    mgr2 = R.RegionManager(root, 1 << 20, min_free=0, clock=clock)  # the queue survives a restart
    await mgr2.start()
    assert await orders(mgr2) == {"r-c": 1, "r-b": 2, "r-d": 3, "r-a": 4}
    await mgr2.stop()


async def test_install_all_endpoint_accepts_everything_at_once(root):
    four_regions(root)
    make_region(root, "r-e", map_=False)  # no map: not installable
    mgr = R.RegionManager(root, 1 << 20, min_free=0, clock=Clock())
    await mgr.start()
    await wait_hashed(mgr)
    await mgr.request_remove("r-b")  # an open removal request is not overridden by "install all"
    await mgr.set_wanted("r-d", True)
    app = web.Application()
    register_region_pages(app, mgr)
    admin = TestClient(TestServer(app))
    await admin.start_server()
    try:
        r = await admin.post("/regions/api/install", data=json.dumps({"all": True}), headers=ADMIN)
        assert r.status == 200 and (await r.json())["requested"] == ["r-a", "r-c"]
        assert await orders(mgr) == {"r-a": 2, "r-b": 0, "r-c": 3, "r-d": 1}  # r-d asked first and keeps its place
        r = await admin.post("/regions/api/install", data=json.dumps({"ids": ["nope-1"]}), headers=ADMIN)
        assert r.status == 404
        assert (await admin.post("/regions/api/install", data=json.dumps({"ids": "x"}), headers=ADMIN)).status == 400
        assert (await admin.post("/regions/api/install", data="{}", headers={"X-Requested-With": "x"})).status == 400
        j = await (await admin.get("/regions/api/list", headers={"X-Requested-With": "car-media-hub"})).json()
        assert {x["id"]: x["installOrder"] for x in j["regions"]}["r-c"] == 3
    finally:
        await admin.close()
        await mgr.stop()


def multi(rid_states):
    return {"deviceId": DEV, "name": "ID.3", "appVersion": "1.0",
            "regions": [{"id": rid, "state": st, "version": "v1", **extra} for rid, st, extra in rid_states]}


async def test_device_view_shows_running_and_queued_with_positions(root):
    clock = Clock()
    four_regions(root)
    mgr = R.RegionManager(root, 1 << 20, min_free=0, clock=clock)
    await mgr.start()
    await mgr.set_wanted_many(["r-c", "r-a", "r-b", "r-d"])
    await mgr.device_report(multi([("r-a", "queued", {}), ("r-b", "queued", {}), ("r-c", "downloading", {"progress": 10, "bytesDone": 1, "bytesTotal": 10}),
                                   ("r-d", "queued", {})]))
    dev = (await mgr.devices_public())[0]
    assert dev["running"] == "r-c" and dev["queue"] == [{"id": "r-a", "position": 1}, {"id": "r-b", "position": 2}, {"id": "r-d", "position": 3}]
    by = {r["id"]: r for r in dev["regions"]}
    assert by["r-b"]["queuePosition"] == 2 and by["r-c"]["queuePosition"] == 0
    clock.now += 900  # the car sleeps: the running region is paused, the others still wait in their order
    dev = (await mgr.devices_public())[0]
    assert dev["running"] == "r-c" and {r["id"]: r["phase"] for r in dev["regions"]}["r-c"] == "paused"
    # cancel a queued and the running request
    await mgr.set_wanted("r-b", False)
    await mgr.set_wanted("r-c", False)
    dev = (await mgr.devices_public())[0]
    assert {r["id"]: r["phase"] for r in dev["regions"]} == {"r-a": "queued", "r-b": "cancelled", "r-c": "cancelled", "r-d": "queued"}
    assert dev["running"] == "" and dev["queue"] == [{"id": "r-a", "position": 1}, {"id": "r-d", "position": 2}]
    await mgr.stop()


# ------------------------------------------------------------------ imports: one at a time, queue on disk


@pytest.fixture
async def serial_origin():
    data = pmtiles(200_000, b"q")
    db = sqlite_bytes(200_000)
    state = {"running": 0, "peak": 0, "order": [], "gate": asyncio.Event()}

    def handler(blob, name):
        async def serve(request):
            state["order"].append(name)
            state["running"] += 1
            state["peak"] = max(state["peak"], state["running"])
            try:
                resp = web.StreamResponse(headers={"Content-Length": str(len(blob)), "ETag": '"v1"'})
                await resp.prepare(request)
                await resp.write(blob[:50_000])
                await state["gate"].wait()
                await resp.write(blob[50_000:])
                return resp
            finally:
                state["running"] -= 1
        return serve

    app = web.Application()
    app.router.add_get("/one", handler(data, "one"))
    app.router.add_get("/two", handler(db, "two"))
    app.router.add_get("/three", handler(data, "three"))
    server = TestServer(app)
    await server.start_server()
    yield server, data, db, state
    state["gate"].set()
    await server.close()


async def test_imports_run_one_at_a_time_in_the_order_requested(root, serial_origin):
    server, data, db, state = serial_origin
    m = await new_mgr(root)
    await m.create_region("imp-1", {"name": "x"})
    await m.create_region("imp-2", {"name": "y"})
    t1 = m.start_import("imp-1", "map", url_of(server, "/one"))
    t2 = m.start_import("imp-1", "places", url_of(server, "/two"))
    t3 = m.start_import("imp-2", "map", url_of(server, "/three"))
    for _ in range(200):  # a loaded machine may need a moment to start the first one
        if t1.state == "running" and t1.downloaded >= 50_000:
            break
        await asyncio.sleep(0.05)
    assert [t.state for t in (t1, t2, t3)] == ["running", "queued", "queued"]
    pos = {t["id"]: t["queuePosition"] for t in (await m.overview())["tasks"]}
    assert pos[t2.id] == 1 and pos[t3.id] == 2 and pos[t1.id] == 0
    state["gate"].set()
    for t in (t1, t2, t3):
        await finished(t)
    assert [t.state for t in (t1, t2, t3)] == ["done"] * 3, [t.error for t in (t1, t2, t3)]
    assert state["peak"] == 1 and state["order"] == ["one", "two", "three"]
    assert not (root / R.IMPORT_QUEUE).exists()  # nothing left to resume
    await m.stop()


async def test_import_queue_survives_a_restart_in_order(root, serial_origin):
    server, data, db, state = serial_origin
    m = await new_mgr(root)
    await m.create_region("imp-1", {"name": "x"})
    t1 = m.start_import("imp-1", "map", url_of(server, "/one"))
    m.start_import("imp-1", "places", url_of(server, "/two"))
    m.start_import("imp-1", "routing", url_of(server, "/three"))
    for _ in range(100):
        if t1.downloaded >= 50_000:
            break
        await asyncio.sleep(0.05)
    await m.stop()  # the add-on is stopped with one import running and two waiting
    queue = json.loads((root / R.IMPORT_QUEUE).read_text())["imports"]
    assert [(q["kind"], q["address"].rsplit("/", 1)[1]) for q in queue] == [("map", "one"), ("places", "two"), ("routing", "three")]
    state.update(order=[], peak=0)
    state["gate"].set()

    m2 = await new_mgr(root)
    jobs = m2.tasks()
    assert [t.kind for t in jobs] == ["map", "places", "routing"]
    for t in jobs[:2]:
        await finished(t)
    assert [t.state for t in jobs[:2]] == ["done", "done"], [t.error for t in jobs]
    assert state["order"][:2] == ["one", "two"] and state["peak"] == 1
    assert (root / "imp-1" / "map.pmtiles").read_bytes() == data
    await finished(jobs[2])  # the third is a map file's bytes under the routing name: refused by the header check, not queued again
    assert jobs[2].state == "failed"
    await m2.stop()


async def test_cancel_of_a_queued_and_of_the_running_import(root, serial_origin):
    server, data, db, state = serial_origin
    m = await new_mgr(root)
    await m.create_region("imp-1", {"name": "x"})
    t1 = m.start_import("imp-1", "map", url_of(server, "/one"))
    t2 = m.start_import("imp-1", "places", url_of(server, "/two"))
    await m.create_region("imp-2", {"name": "y"})
    t3 = m.start_import("imp-2", "map", url_of(server, "/three"))
    for _ in range(200):
        if t1.state == "running" and t1.downloaded >= 50_000:
            break
        await asyncio.sleep(0.05)
    assert m.cancel_task(t2.id) is True  # a queued one: never starts, leaves the queue file
    await finished(t2)
    assert t2.state == "cancelled" and "two" not in state["order"]
    queue = json.loads((root / R.IMPORT_QUEUE).read_text())["imports"]
    assert [q["kind"] for q in queue] == ["map", "map"]
    assert m.cancel_task(t1.id) is True  # the running one: the next in line takes over
    await finished(t1)
    for _ in range(100):
        if t3.state == "running":
            break
        await asyncio.sleep(0.05)
    assert t3.state == "running" and state["order"] == ["one", "three"]
    state["gate"].set()
    await finished(t3)
    assert t3.state == "done" and not (root / R.IMPORT_QUEUE).exists()
    assert not m._busy
    await m.stop()
    m2 = await new_mgr(root)  # neither cancelled import comes back after a restart
    assert m2.tasks() == []
    await m2.stop()
