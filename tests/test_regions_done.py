"""An install request that every device has fulfilled is "done" on the page (no queue position); the positions count only
the pending regions; an installed region in an older version than the catalogue is an update that is pending."""
from __future__ import annotations

import json

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from car_media_hub import regions as R
from car_media_hub.regions_web import register_region_pages
from test_regions import make_region, root, wait_hashed  # noqa: F401  (root is a fixture)
from test_regions_resume import Clock

CURRENT = "2026-11"  # the version make_region writes into the manifest


def report(dev, *items):
    return {"deviceId": dev, "name": dev, "appVersion": "1", "regions": [
        {"id": rid, "state": st, "version": ver, "progress": 0, **extra} for rid, st, ver, extra in items]}


async def setup(root, ids=("r-a", "r-b", "r-c", "r-d")):
    for rid in ids:
        make_region(root, rid)
    mgr = R.RegionManager(root, 1 << 20, min_free=0, clock=Clock())
    await mgr.start()
    await wait_hashed(mgr)
    await mgr.set_wanted_many(list(ids))
    return mgr


async def regions_of(mgr):
    return {r["id"]: r for r in (await mgr.overview())["regions"]}


async def test_installed_region_is_done_and_positions_count_only_pending(root):
    mgr = await setup(root)
    await mgr.device_report(report("device-0001", ("r-a", "installed", CURRENT, {}), ("r-b", "installed", "2026-10", {}),
                                   ("r-c", "queued", "", {"bytesTotal": 10}), ("r-d", "downloading", "", {"bytesDone": 5, "bytesTotal": 10})))
    r = await regions_of(mgr)
    assert (r["r-a"]["done"], r["r-a"]["updatePending"]) == (True, False)
    assert (r["r-b"]["done"], r["r-b"]["updatePending"]) == (False, True)
    assert r["r-c"]["done"] is False and r["r-d"]["done"] is False
    # pending: b (update), c, d in request order, dense; the done region has no position
    assert {k: v["installOrder"] for k, v in r.items()} == {"r-a": 0, "r-b": 1, "r-c": 2, "r-d": 3}
    # the catalogue keeps the order of all wanted regions for the app
    cat = {x["id"]: x["installOrder"] for x in json.loads(await mgr.catalogue_body())["regions"]}
    assert cat == {"r-a": 1, "r-b": 2, "r-c": 3, "r-d": 4} and r["r-c"]["requestOrder"] == 3
    assert all(x["wanted"] for x in r.values())  # the request itself stays
    dev = (await mgr.devices_public())[0]
    assert dev["running"] == "r-d" and dev["queue"] == [{"id": "r-b", "position": 1}, {"id": "r-c", "position": 2}]
    by = {x["id"]: x for x in dev["regions"]}
    assert by["r-a"]["queuePosition"] == 0 and by["r-a"]["current"] is True and by["r-b"]["updatePending"] is True
    await mgr.stop()


async def test_done_needs_every_known_device_and_unknown_version_counts_as_current(root):
    mgr = await setup(root, ("r-a", "r-b"))
    await mgr.device_report(report("device-0001", ("r-a", "installed", "", {}), ("r-b", "installed", CURRENT, {})))
    r = await regions_of(mgr)
    assert r["r-a"]["done"] is True and r["r-a"]["devicesInstalled"] == 1 and r["r-a"]["devicesTotal"] == 1
    await mgr.device_report(report("device-0002", ("r-a", "absent", "", {}), ("r-b", "installed", CURRENT, {})))  # a second car
    r = await regions_of(mgr)
    assert r["r-a"]["done"] is False and (r["r-a"]["devicesInstalled"], r["r-a"]["devicesTotal"]) == (1, 2)
    assert r["r-b"]["done"] is True
    assert (r["r-a"]["installOrder"], r["r-b"]["installOrder"]) == (1, 0)
    await mgr.stop()


async def test_no_device_yet_means_pending_and_unwanted_is_never_done(root):
    mgr = await setup(root, ("r-a", "r-b"))
    await mgr.set_wanted("r-b", False)
    r = await regions_of(mgr)
    assert (r["r-a"]["done"], r["r-a"]["installOrder"]) == (False, 1)
    assert (r["r-b"]["done"], r["r-b"]["installOrder"]) == (False, 0)
    await mgr.device_report(report("device-0001", ("r-a", "installed", CURRENT, {}), ("r-b", "installed", CURRENT, {})))
    r = await regions_of(mgr)
    assert r["r-a"]["done"] is True and r["r-b"]["done"] is False and r["r-b"]["updatePending"] is False
    await mgr.stop()


async def test_new_catalogue_version_turns_a_done_region_into_an_update(root):
    mgr = await setup(root, ("r-a",))
    await mgr.device_report(report("device-0001", ("r-a", "installed", CURRENT, {})))
    assert (await regions_of(mgr))["r-a"]["done"] is True
    await mgr.update_manifest("r-a", {"name": "x", "version": "2026-12"})
    r = (await regions_of(mgr))["r-a"]
    assert (r["done"], r["updatePending"], r["installOrder"]) == (False, True, 1)
    await mgr.stop()


async def test_page_wording_in_both_languages(root):
    mgr = await setup(root, ("r-a",))
    app = web.Application()
    register_region_pages(app, mgr)
    admin = TestClient(TestServer(app))
    await admin.start_server()
    try:
        page = await (await admin.get("/regions")).text()
        for text in ("Installed on the car app (", "Auf der Auto-App installiert (", "no installs pending", "keine Installationen offen",
                     "Update pending", "Update ausstehend", "Cancel request", "Anfrage zurücknehmen"):
            assert text in page
        j = await (await admin.get("/regions/api/list", headers={"X-Requested-With": "car-media-hub"})).json()
        assert j["regions"][0]["done"] is False and j["regions"][0]["installOrder"] == 1
    finally:
        await admin.close()
        await mgr.stop()
