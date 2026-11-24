"""Build stamp (which code runs) and the "done" display for the exact data of the real car."""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from car_media_hub import build_info as B
from car_media_hub import regions as R
from car_media_hub.regions_web import register_region_pages
from test_regions import ADMIN, AUTH, make_region, root, sync_client, wait_hashed  # noqa: F401  (root is a fixture)

TOOL = Path(__file__).resolve().parents[2] / "tools" / "addon_build_id.py"


def load_tool():
    spec = importlib.util.spec_from_file_location("addon_build_id", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_id_depends_on_names_and_bytes_and_ignores_pycache(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.py").write_text("y = 2\n")
    first = B.compute_build_id(tmp_path)
    assert re.fullmatch(r"[0-9a-f]{10}", first) and B.compute_build_id(tmp_path) == first
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "a.cpython-312.py").write_text("junk")
    (tmp_path / "notes.txt").write_text("not code")
    assert B.compute_build_id(tmp_path) == first
    (tmp_path / "a.py").write_text("x = 2\n")
    changed = B.compute_build_id(tmp_path)
    assert changed != first
    (tmp_path / "a.py").rename(tmp_path / "c.py")  # same bytes, other name
    assert B.compute_build_id(tmp_path) != changed


@pytest.mark.skipif(not TOOL.exists(), reason="tools/addon_build_id.py is not next to the add-on copy")
def test_tool_script_gives_the_same_id_as_the_add_on(tmp_path, capsys, monkeypatch):
    tool = load_tool()
    assert tool.compute_build_id(B.PACKAGE) == B.compute_build_id(B.PACKAGE)
    (tmp_path / "m.py").write_text("print('hi')\n")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "n.py").write_bytes(b"\xc3\xa4 = 1\n")
    assert tool.compute_build_id(tmp_path) == B.compute_build_id(tmp_path)
    monkeypatch.setattr("sys.argv", ["addon_build_id.py", str(B.PACKAGE)])
    runpy_main = compile(TOOL.read_text(), str(TOOL), "exec")
    exec(runpy_main, {"__name__": "__main__"})
    assert capsys.readouterr().out.strip() == B.build_info()["id"]


def test_build_info_shape_and_version_from_config(tmp_path):
    info = B.build_info()
    assert set(info) == {"id", "started", "version"} and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", info["started"])
    assert info == B.build_info() and info["id"] == B.compute_build_id(B.PACKAGE)
    pkg = tmp_path / "app" / "car_media_hub"
    pkg.mkdir(parents=True)
    assert B.read_version(pkg) == ""
    (tmp_path / "config.yaml").write_text('name: x\nversion: "1.2.3"\n')
    assert B.read_version(pkg) == "1.2.3"
    assert B.footer_text({"id": "abc", "started": "2026-11-18T10:00:00Z", "version": "1.2.3"}) == "build abc \u00b7 started 2026-11-18T10:00:00Z \u00b7 v1.2.3"


async def test_build_is_in_both_json_answers_and_in_the_page_footer(root, tmp_path):
    make_region(root, "r-a")
    mgr = R.RegionManager(root, 1 << 20, min_free=0)
    await mgr.start()
    api = await sync_client(mgr, tmp_path)
    app = web.Application()
    register_region_pages(app, mgr)
    admin = TestClient(TestServer(app))
    await admin.start_server()
    try:
        info = B.build_info()
        devices = await (await api.get("/api/regions/devices", headers=AUTH)).json()
        assert devices["devices"] == [] and devices["build"] == info
        listing = await (await admin.get("/regions/api/list", headers={"X-Requested-With": "car-media-hub"})).json()
        assert listing["ok"] is True and listing["regions"] and listing["build"] == info
        page = await (await admin.get("/regions")).text()
        assert f"build {info['id']} \u00b7 started {info['started']} \u00b7 v{info['version']}" in page
    finally:
        await api.close()
        await admin.close()
        await mgr.stop()


# ------------------------------------------------------------------ the real data of the car: done, installOrder 0


async def test_real_car_data_is_done_with_order_zero_through_the_http_paths(root, tmp_path):
    d = make_region(root, "oberpfalz")  # manifest version 2026-11
    make_region(root, "baden-wuerttemberg")
    for rid in ("oberpfalz", "baden-wuerttemberg"):
        (root / rid / "want.json").write_text(json.dumps(
            {"wanted": True, "wantedAt": "2026-11-16T14:43:27Z", "wantedBy": "", "removeRequested": False}))  # no wantedSeq: older file
    mgr = R.RegionManager(root, 1 << 20, min_free=0)  # the real clock: the report is "now", the want is from 2 days ago
    await mgr.start()
    await wait_hashed(mgr)
    api = await sync_client(mgr, tmp_path)
    app = web.Application()
    register_region_pages(app, mgr)
    admin = TestClient(TestServer(app))
    await admin.start_server()
    try:
        cat_before = json.loads(await mgr.catalogue_body())
        body = {"deviceId": "car-device-0001", "name": "Car", "appVersion": "1", "regions": [
            {"id": "oberpfalz", "state": "installed", "version": "2026-11", "progress": 100, "message": "",
             "bytesDone": 123456789, "bytesTotal": 123456789, "rate": 1.5, "rateTs": 1.0, "reportedRate": None},
            {"id": "baden-wuerttemberg", "state": "installed", "version": "2026-11", "progress": 100,
             "bytesDone": 5, "bytesTotal": 5}]}
        r = await api.post("/api/regions/devices/report", data=json.dumps(body), headers={**AUTH, "Content-Type": "application/json"})
        assert r.status == 200
        j = await (await admin.get("/regions/api/list", headers={"X-Requested-With": "car-media-hub"})).json()
        by = {x["id"]: x for x in j["regions"]}
        for rid in ("oberpfalz", "baden-wuerttemberg"):
            x = by[rid]
            assert x["done"] is True and x["installOrder"] == 0 and x["wanted"] is True and x["updatePending"] is False, x
            assert (x["devicesInstalled"], x["devicesTotal"]) == (1, 1)
        # no catalogue cache involved: the page API reads the devices at every call; the catalogue still wants both
        cat = {c["id"]: c for c in json.loads(await mgr.catalogue_body())["regions"]}
        assert cat["oberpfalz"]["install"] is True and cat["oberpfalz"]["installOrder"] in (1, 2)
        assert cat_before["regions"]
        # a device that also lists other installed regions changes nothing
        body["regions"].append({"id": "sachsen", "state": "installed", "version": "2026-11", "progress": 100})
        await api.post("/api/regions/devices/report", data=json.dumps(body), headers={**AUTH, "Content-Type": "application/json"})
        j = await (await admin.get("/regions/api/list", headers={"X-Requested-With": "car-media-hub"})).json()
        assert all(x["done"] and x["installOrder"] == 0 for x in j["regions"])
    finally:
        await api.close()
        await admin.close()
        await mgr.stop()
