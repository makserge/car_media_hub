"""Estimated sync time: the download rate (reported by the app or derived from consecutive reports), the remaining time
per region and for the whole queue of a device, rounding, and the validation of the optional bytesPerSecond field."""
from __future__ import annotations

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from car_media_hub import regions as R
from car_media_hub import regions_devices as D
from car_media_hub.regions_web import register_region_pages
from test_regions import make_region, root  # noqa: F401  (root is a fixture)
from test_regions_resume import Clock, DEV

MB = 1_000_000
GB = 1_000_000_000


def rows(*items):
    return {"deviceId": DEV, "name": "ID.3", "appVersion": "1.0",
            "regions": [{"id": rid, "state": st, "version": "v1", **extra} for rid, st, extra in items]}


async def make(root, clock, ids=("r-a", "r-b", "r-c")):
    for rid in ids:
        make_region(root, rid)
    mgr = R.RegionManager(root, 1 << 20, min_free=0, clock=clock)
    await mgr.start()
    await mgr.set_wanted_many(list(ids))
    return mgr


async def dev(mgr):
    return (await mgr.devices_public())[0]


def by_id(device):
    return {r["id"]: r for r in device["regions"]}


@pytest.mark.parametrize("seconds,minutes", [
    (10, 1), (60, 1), (95, 2), (29 * 60, 29), (119 * 60 + 29, 119), (2 * 3600, 120), (2 * 3600 + 100, 120),
    (3 * 3600 + 20 * 60, 200), (3 * 3600 + 22 * 60, 200), (3 * 3600 + 23 * 60, 205), (11 * 3600 + 59 * 60, 720),
])
def test_eta_rounding(seconds, minutes):
    assert D.round_eta_minutes(seconds) == minutes


async def test_rate_is_derived_from_consecutive_reports_and_smoothed(root):
    clock = Clock()
    mgr = await make(root, clock, ("r-a",))
    await mgr.device_report(rows(("r-a", "downloading", {"bytesDone": 0, "bytesTotal": GB})))
    assert by_id(await dev(mgr))["r-a"]["bytesPerSecond"] == 0  # one report: unknown, not zero
    clock.now += 30
    await mgr.device_report(rows(("r-a", "downloading", {"bytesDone": 30 * MB, "bytesTotal": GB})))
    r = by_id(await dev(mgr))["r-a"]
    assert r["bytesPerSecond"] == MB and r["rateSource"] == "derived"
    clock.now += 60  # now 3 MB/s instantaneous; the average moves towards it, not all the way (tau 60 s)
    await mgr.device_report(rows(("r-a", "downloading", {"bytesDone": 210 * MB, "bytesTotal": GB})))
    rate = by_id(await dev(mgr))["r-a"]["bytesPerSecond"]
    assert MB < rate < 3 * MB and abs(rate - (MB + (1 - 2.718281828 ** -1) * 2 * MB)) < 5000
    await mgr.stop()


async def test_reset_negative_delta_gap_and_other_states_restart_the_rate(root):
    clock = Clock()
    mgr = await make(root, clock, ("r-a",))
    send = lambda done, state="downloading", **kw: mgr.device_report(rows(("r-a", state, {"bytesDone": done, "bytesTotal": GB, **kw})))
    await send(0)
    clock.now += 10
    await send(10 * MB)
    assert by_id(await dev(mgr))["r-a"]["bytesPerSecond"] == MB
    clock.now += 10
    await send(1 * MB)  # restarted from zero: no negative rate, unknown again
    assert by_id(await dev(mgr))["r-a"]["bytesPerSecond"] == 0
    clock.now += 10
    await send(11 * MB)  # and measured again from the next pair
    assert by_id(await dev(mgr))["r-a"]["bytesPerSecond"] == MB
    clock.now += D.PAUSED_AFTER_SECONDS + 1  # the car slept: a long gap says nothing about the speed
    await send(500 * MB)
    assert by_id(await dev(mgr))["r-a"]["bytesPerSecond"] == 0
    clock.now += 10
    await send(510 * MB)
    clock.now += 10
    await send(0, "queued")  # not downloading: no rate
    clock.now += 10
    await send(520 * MB)
    assert by_id(await dev(mgr))["r-a"]["bytesPerSecond"] == 0
    await mgr.stop()


async def test_rate_is_persisted_and_old_rates_are_dropped(root):
    clock = Clock()
    mgr = await make(root, clock, ("r-a",))
    await mgr.device_report(rows(("r-a", "downloading", {"bytesDone": 0, "bytesTotal": GB})))
    clock.now += 10
    await mgr.device_report(rows(("r-a", "downloading", {"bytesDone": 20 * MB, "bytesTotal": GB})))
    await mgr.stop()
    mgr2 = R.RegionManager(root, 1 << 20, min_free=0, clock=clock)  # add-on restart
    await mgr2.start()
    assert by_id(await dev(mgr2))["r-a"]["bytesPerSecond"] == 2 * MB
    clock.now += 100  # the next report continues the average of the stored rate
    await mgr2.device_report(rows(("r-a", "downloading", {"bytesDone": 20 * MB + 400 * MB, "bytesTotal": GB})))
    assert by_id(await dev(mgr2))["r-a"]["bytesPerSecond"] > 2 * MB
    await mgr2.stop()
    doc = json.loads((root / ".devices" / f"{DEV}.json").read_text())
    assert doc["regions"]["r-a"]["rate"] > 0
    # a rate older than 10 minutes is not shown (a stored one, or the report was long ago)
    mgr3 = R.RegionManager(root, 1 << 20, min_free=0, clock=clock)
    await mgr3.start()
    clock.now += D.RATE_KEEP_SECONDS + 1
    r = by_id(await dev(mgr3))["r-a"]
    assert r["bytesPerSecond"] == 0 and r["etaSeconds"] is None and r["phase"] == "paused"
    await mgr3.stop()


async def test_reported_rate_wins_and_is_validated(root):
    clock = Clock()
    mgr = await make(root, clock, ("r-a",))
    await mgr.device_report(rows(("r-a", "downloading", {"bytesDone": 0, "bytesTotal": GB})))
    clock.now += 10
    await mgr.device_report(rows(("r-a", "downloading", {"bytesDone": 10 * MB, "bytesTotal": GB, "bytesPerSecond": 5 * MB})))
    r = by_id(await dev(mgr))["r-a"]
    assert r["bytesPerSecond"] == 5 * MB and r["rateSource"] == "app"
    assert r["etaSeconds"] == int((GB - 10 * MB) / (5 * MB))
    for bad in (-1, 1e9 + 1, "fast", True, float("nan"), [1]):
        with pytest.raises(D.ReportError):
            await mgr.device_report(rows(("r-a", "downloading", {"bytesPerSecond": bad})))
    await mgr.device_report(rows(("r-a", "downloading", {"bytesPerSecond": None})))  # missing/null: unknown
    await mgr.device_report(rows(("r-a", "downloading", {"bytesPerSecond": 0})))  # zero is allowed, means nothing known
    await mgr.device_report(rows(("r-a", "downloading", {"bytesPerSecond": 1e9})))
    await mgr.stop()


async def test_queue_estimate_sums_the_regions_ahead(root):
    clock = Clock()
    mgr = await make(root, clock)
    # r-a is running at 2 MB/s with 600 MB to go; r-b and r-c wait (the app reports their total, no progress)
    items = lambda done: (("r-a", "downloading", {"bytesDone": done, "bytesTotal": GB, "bytesPerSecond": 2 * MB}),
                          ("r-b", "queued", {"bytesTotal": 500 * MB}), ("r-c", "queued", {"bytesTotal": 300 * MB}))
    await mgr.device_report(rows(*items(400 * MB)))
    d = await dev(mgr)
    r = by_id(d)
    assert r["r-a"]["etaSeconds"] == 300 and r["r-a"]["etaMinutes"] == 5
    assert r["r-b"]["startsInSeconds"] == 300 and r["r-b"]["etaSeconds"] is None
    assert r["r-c"]["startsInSeconds"] == 300 + 250  # 600 MB + 500 MB ahead, at 2 MB/s
    s = d["estimate"]
    assert s["state"] == "eta" and s["count"] == 3 and s["bytesLeft"] == 600 * MB + 500 * MB + 300 * MB
    assert s["bytesPerSecond"] == 2 * MB and s["etaSeconds"] == 700 and s["etaMinutes"] == 12
    # a long queue: hours and minutes, rounded to 5 minutes
    await mgr.device_report(rows(("r-a", "downloading", {"bytesDone": 400 * MB, "bytesTotal": 10 * GB, "bytesPerSecond": 1 * MB}),
                                 ("r-b", "queued", {"bytesTotal": GB}), ("r-c", "queued", {"bytesTotal": GB + 200 * MB})))
    s = (await dev(mgr))["estimate"]
    assert s["state"] == "eta" and s["etaSeconds"] == 11_800 and s["etaMinutes"] == 195  # 3 h 16 min 40 s -> 3 h 15 min
    await mgr.stop()


async def test_unknown_or_too_low_rate_means_measuring_and_paused_has_no_number(root):
    clock = Clock()
    mgr = await make(root, clock, ("r-a", "r-b"))
    await mgr.device_report(rows(("r-a", "downloading", {"bytesDone": 5 * MB, "bytesTotal": GB}), ("r-b", "queued", {"bytesTotal": GB})))
    d = await dev(mgr)
    assert d["estimate"]["state"] == "measuring" and by_id(d)["r-a"]["etaSeconds"] is None and by_id(d)["r-b"]["startsInSeconds"] is None
    await mgr.device_report(rows(("r-a", "downloading", {"bytesDone": 5 * MB, "bytesTotal": GB, "bytesPerSecond": 40_000}),
                                 ("r-b", "queued", {"bytesTotal": GB})))
    assert (await dev(mgr))["estimate"]["state"] == "measuring"  # below 50 KB/s: no number
    await mgr.device_report(rows(("r-a", "downloading", {"bytesDone": 5 * MB, "bytesTotal": GB, "bytesPerSecond": MB}),
                                 ("r-b", "queued", {"bytesTotal": GB})))
    assert (await dev(mgr))["estimate"]["state"] == "eta"
    clock.now += 600  # the car sleeps: paused, queued - no ETA at all
    d = await dev(mgr)
    assert d["estimate"]["state"] == "paused" and d["estimate"]["etaSeconds"] is None
    assert by_id(d)["r-a"]["phase"] == "paused" and by_id(d)["r-a"]["etaSeconds"] is None
    await mgr.device_report(rows(("r-a", "installed", {"bytesDone": GB, "bytesTotal": GB}), ("r-b", "installed", {})))
    assert (await dev(mgr))["estimate"]["state"] == "idle"
    await mgr.stop()


async def test_page_has_the_estimate_wording_in_both_languages_and_the_json_the_page_reads(root):
    clock = Clock()
    mgr = await make(root, clock, ("r-a", "r-b"))
    await mgr.device_report(rows(("r-a", "downloading", {"bytesDone": GB, "bytesTotal": 4 * GB, "bytesPerSecond": 2 * MB}),
                                 ("r-b", "queued", {"bytesTotal": GB})))
    app = web.Application()
    register_region_pages(app, mgr)
    admin = TestClient(TestServer(app))
    await admin.start_server()
    try:
        page = await (await admin.get("/regions")).text()
        for text in ("queued regions: about", "measuring", "Estimate, depends on Wi-Fi and the car being awake", "Schätzung", "startet in ca."):
            assert text in page
        j = await (await admin.get("/regions/api/list", headers={"X-Requested-With": "car-media-hub"})).json()
        est = j["devices"][0]["estimate"]
        assert est["state"] == "eta" and est["count"] == 2 and est["etaMinutes"] == 33  # 4 GB left at 2 MB/s = 2000 s
    finally:
        await admin.close()
        await mgr.stop()
