"""Regions of one routing group share one routing archive: counted once in the estimate, and a region that waits for it
does not look stuck at 0 % (device with 4 regions of the group "germany", as seen on the real car)."""
from __future__ import annotations

import json

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from car_media_hub import regions as R
from car_media_hub.regions_web import register_region_pages
from test_regions import make_region, root  # noqa: F401  (root is a fixture)
from test_regions_eta import MB, by_id, rows
from test_regions_resume import Clock

ROUTING = 2400 * MB
ORDER = {"bran": 1, "ham": 2, "sax": 3, "thu": 4}
SHARED = {rid: ("germany", ROUTING) for rid in ORDER}


async def mgr_with(root, clock, ids=tuple(ORDER)):
    for rid in ids:
        make_region(root, rid, group="germany")
    mgr = R.RegionManager(root, 1 << 20, min_free=0, clock=clock)
    await mgr.start()
    return mgr


def car(**changes):
    items = {
        "bran": ("downloading", {"bytesDone": 1590 * MB, "bytesTotal": 2920 * MB, "bytesPerSecond": 2 * MB}),
        "ham": ("downloading", {"bytesDone": 0, "bytesTotal": 2530 * MB}),
        "sax": ("queued", {"bytesTotal": 2600 * MB}),
        "thu": ("queued", {"bytesTotal": 2700 * MB}),
    }
    items.update(changes)
    return rows(*((rid, st, extra) for rid, (st, extra) in items.items()))


async def view(mgr, shared=SHARED):
    return (await mgr.devices.public_list(order=ORDER, shared=shared))[0]


async def test_shared_routing_file_is_counted_once_in_the_total(root):
    mgr = await mgr_with(root, Clock())
    await mgr.device_report(car())
    d = await view(mgr)
    r = by_id(d)
    # bran carries the file: 2920 - 1590 left; the others only their own part (total minus the 2400 MB routing archive)
    assert [r[i]["leftBytes"] for i in ORDER] == [1330 * MB, 130 * MB, 200 * MB, 300 * MB]
    s = d["estimate"]
    assert s["state"] == "eta" and s["count"] == 4 and s["bytesLeft"] == 1960 * MB  # not 9160 MB
    assert s["etaSeconds"] == 980 and s["etaMinutes"] == 16
    assert d["running"] == "bran"  # the region that moves, not the one that merely says "downloading"
    assert r["bran"]["etaSeconds"] == 665
    # the queued ones start after bran and after ham (which runs next, it only needs its own 130 MB)
    assert r["sax"]["startsInSeconds"] == 730 and r["thu"]["startsInSeconds"] == 830
    assert [q["id"] for q in d["queue"]] == ["sax", "thu"]
    await mgr.stop()


async def test_a_region_waiting_for_the_shared_file_does_not_look_stuck_at_zero(root):
    mgr = await mgr_with(root, Clock())
    await mgr.device_report(car())
    r = by_id(await view(mgr))
    assert r["ham"]["waitingForShared"] is True and r["ham"]["state"] == "downloading"
    assert r["ham"]["sharedRouting"] == {"group": "germany", "bytes": ROUTING, "doneBytes": 1590 * MB, "carrier": False}
    assert r["bran"]["waitingForShared"] is False and r["bran"]["sharedRouting"]["carrier"] is True
    assert r["sax"]["waitingForShared"] is False  # queued: it waits for its turn, not "for the file"
    # the file is complete (the carrier is past it): the 0 byte region is simply next in line, no longer "waiting for the file"
    await mgr.device_report(car(bran=("downloading", {"bytesDone": 2700 * MB, "bytesTotal": 2920 * MB, "bytesPerSecond": 2 * MB})))
    r = by_id(await view(mgr))
    assert r["ham"]["waitingForShared"] is False and r["ham"]["sharedRouting"]["doneBytes"] == ROUTING
    await mgr.stop()


async def test_an_installed_region_of_the_group_means_the_file_is_on_the_device(root):
    mgr = await mgr_with(root, Clock())
    await mgr.device_report(car(bran=("installed", {"bytesDone": 2920 * MB, "bytesTotal": 2920 * MB}),
                                ham=("downloading", {"bytesDone": 50 * MB, "bytesTotal": 2530 * MB, "bytesPerSecond": 2 * MB})))
    d = await view(mgr)
    r = by_id(d)
    assert r["bran"]["phase"] == "installed" and r["ham"]["waitingForShared"] is False
    # nobody carries the routing file any more: every region only has its own part left (ham's own 130 MB, bytesDone counts too)
    assert r["ham"]["sharedRouting"]["doneBytes"] == ROUTING
    assert d["estimate"]["bytesLeft"] == (130 + 200 + 300) * MB
    assert r["sax"]["leftBytes"] == 200 * MB and r["thu"]["leftBytes"] == 300 * MB
    await mgr.stop()


async def test_all_queued_the_first_in_order_carries_the_file(root):
    mgr = await mgr_with(root, Clock())
    await mgr.device_report(car(bran=("queued", {"bytesTotal": 2920 * MB}), ham=("queued", {"bytesTotal": 2530 * MB})))
    d = await view(mgr)
    r = by_id(d)
    assert r["bran"]["leftBytes"] == 2920 * MB and r["bran"]["sharedRouting"]["carrier"] is True
    assert r["ham"]["leftBytes"] == 130 * MB  # its own part only: the archive comes with bran
    assert d["estimate"]["state"] == "paused"  # nothing downloads: no number, but also no double counting
    assert d["estimate"]["bytesLeft"] == (2920 + 130 + 200 + 300) * MB
    await mgr.stop()


async def test_regions_without_a_group_are_untouched(root):
    mgr = await mgr_with(root, Clock())
    await mgr.device_report(car())
    d = await view(mgr, shared={})
    assert d["estimate"]["bytesLeft"] == (1330 + 2530 + 2600 + 2700) * MB
    assert not any(x["waitingForShared"] for x in d["regions"])
    await mgr.stop()


async def test_manager_derives_the_shared_file_from_the_routing_group(root):
    clock = Clock()
    make_region(root, "bran", routing=True, group="germany")  # holds routing.tar (the first by id with one provides it)
    make_region(root, "ham", group="germany")
    make_region(root, "solo")
    mgr = R.RegionManager(root, 1 << 20, min_free=0, clock=clock)
    await mgr.start()
    size = (root / "bran" / "routing.tar").stat().st_size
    await mgr.device_report(rows(("bran", "downloading", {"bytesDone": size // 2, "bytesTotal": 10 * size, "bytesPerSecond": 1000 * size}),
                                 ("ham", "downloading", {"bytesDone": 0, "bytesTotal": 10 * size}),
                                 ("solo", "queued", {"bytesTotal": 10 * size})))
    r = by_id((await mgr.devices_public())[0])
    assert r["ham"]["sharedRouting"]["bytes"] == size and r["ham"]["waitingForShared"] is True
    assert r["solo"]["sharedRouting"] is None
    await mgr.stop()


async def test_page_has_the_waiting_wording_in_both_languages(root):
    mgr = await mgr_with(root, Clock(), ("bran",))
    app = web.Application()
    register_region_pages(app, mgr)
    admin = TestClient(TestServer(app))
    await admin.start_server()
    try:
        page = await (await admin.get("/regions")).text()
        assert "waiting for the shared routing file" in page and "gemeinsame Routing-Datei" in page
    finally:
        await admin.close()
        await mgr.stop()
