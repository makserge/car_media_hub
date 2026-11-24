"""Gzip transfer copies of routing/places (regions_gzip.py): built in the background after the hash, recorded in
transfer.json, offered as ``transfer`` in the catalogue (url/bytes/sha256 still describe the plain file), served like the
plain files (ranges, If-Range, ETag, X-Max-Streams, stream budget), stale ones are never offered and get deleted."""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import os
import time

import pytest

from car_media_hub import regions as R
from car_media_hub import regions_gzip as G
from car_media_hub.diskspace import NoSpaceError

from test_regions import AUTH, app_parse, gen, make_region, old, sqlite_bytes, sync_client, tar_bytes


@pytest.fixture
def root(tmp_path):
    return tmp_path / "regions"


@pytest.fixture
async def gmgr(root, monkeypatch):
    monkeypatch.setattr(G, "MIN_BYTES", 0)  # the test files are a few KB
    m = R.RegionManager(root, 1024 * 1024, min_free=0, transfer_gzip=True)
    await m.start()
    yield m
    await m.stop()


async def settle(mgr, timeout=15):
    """Rescan until every file is hashed and the gzip worker has nothing more to do (the last rescan submitted nothing)."""
    end = time.time() + timeout
    while time.time() < end:
        mgr.invalidate()
        regions = await mgr.rescan()
        if all(f.sha256 for r in regions for f in r.files.values()) and not mgr._hasher.busy() and not mgr._gzip.busy():
            return regions
        await asyncio.sleep(0.05)
    raise AssertionError("hashing or compressing did not finish")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def meta(directory):
    return json.loads((directory / R.TRANSFER_META).read_text())


async def catalogue(mgr):
    return json.loads(await mgr.catalogue_body())


def files_of(doc, region_id):
    return {f["kind"]: f for f in next(r for r in doc["regions"] if r["id"] == region_id)["files"]}


# ------------------------------------------------------------------ build and catalogue


async def test_sidecars_are_built_for_routing_and_places_and_advertised(root, gmgr):
    d = make_region(root, "oberpfalz", routing=True, places=True)
    await settle(gmgr)
    assert (d / "routing.tar.gz").is_file() and (d / "places.sqlite.gz").is_file()
    assert not list(d.glob("map.pmtiles*.gz")) and not list(d.glob(".gzip-*"))
    m = meta(d)
    assert m["version"] == 1 and set(m["files"]) == {"routing.tar.gz", "places.sqlite.gz"}
    for name, plain_name in (("routing.tar.gz", "routing.tar"), ("places.sqlite.gz", "places.sqlite")):
        raw, plain = (d / name).read_bytes(), (d / plain_name).read_bytes()
        assert gzip.decompress(raw) == plain
        assert raw[3] == 0 and raw[4:8] == b"\x00\x00\x00\x00"  # no file name, mtime 0: deterministic
        entry = m["files"][name]
        st = os.stat(d / name)
        assert entry == {"size": len(raw), "mtime_ns": st.st_mtime_ns, "sha256": sha(raw),
                         "sourceSha256": sha(plain), "sourceSize": len(plain)}
    # files.json (the hash cache) never sees a .gz name
    assert set(json.loads((d / R.HASH_CACHE).read_text())["files"]) == {"map.pmtiles", "routing.tar", "places.sqlite"}
    doc = await catalogue(gmgr)
    assert [r["id"] for r in app_parse(doc)] == ["oberpfalz"]  # the app's parser (and an old app) still accepts it
    files = files_of(doc, "oberpfalz")
    assert "transfer" not in files["map"]
    for kind, name in (("routing", "routing.tar"), ("places", "places.sqlite")):
        plain = (d / name).read_bytes()
        raw = (d / f"{name}.gz").read_bytes()
        f = files[kind]
        assert (f["url"], f["bytes"], f["sha256"]) == (f"/api/regions/files/oberpfalz/{name}", len(plain), sha(plain))
        assert f["transfer"] == {"encoding": "gzip", "url": f"/api/regions/files/oberpfalz/{name}.gz",
                                 "bytes": len(raw), "sha256": sha(raw)}
        assert f["transfer"]["bytes"] < f["bytes"]
    over = await gmgr.overview()
    states = {f["kind"]: f["transfer"] for f in over["regions"][0]["files"]}
    assert states["map"] is None and states["routing"]["state"] == "ready" and states["places"]["state"] == "ready"


async def test_build_is_deterministic(root, gmgr):
    d = make_region(root, "det-1", routing=True)
    await settle(gmgr)
    first = (d / "routing.tar.gz").read_bytes()
    os.unlink(d / "routing.tar.gz")
    os.unlink(d / R.TRANSFER_META)
    await settle(gmgr)
    assert (d / "routing.tar.gz").read_bytes() == first


async def test_option_off_by_default_keeps_the_catalogue_unchanged(root):
    make_region(root, "plain-1", routing=True, places=True)
    m = R.RegionManager(root, 1024 * 1024, min_free=0)
    await m.start()
    try:
        await settle(m)
        doc = await catalogue(m)
        assert all("transfer" not in f for f in files_of(doc, "plain-1").values())
        assert not list((root / "plain-1").glob("*.gz")) and not (root / "plain-1" / R.TRANSFER_META).exists()
    finally:
        await m.stop()


async def test_small_files_are_not_compressed(root):
    make_region(root, "small-1", routing=True)  # 2 KB, below the real MIN_BYTES of 1 MiB
    m = R.RegionManager(root, 1024 * 1024, min_free=0, transfer_gzip=True)
    await m.start()
    try:
        await settle(m)
        assert "transfer" not in files_of(await catalogue(m), "small-1")["routing"]
        assert not (root / "small-1" / "routing.tar.gz").exists()
    finally:
        await m.stop()


async def test_grouped_routing_transfer_uses_the_provider(root, gmgr):
    make_region(root, "a-north", routing=True, group="nord")
    make_region(root, "b-south", routing=False, group="nord")
    c = make_region(root, "c-west", routing=True, group="nord")  # its own routing.tar is ignored (a-north provides)
    await settle(gmgr)
    doc = await catalogue(gmgr)
    a, b = files_of(doc, "a-north")["routing"], files_of(doc, "b-south")["routing"]
    assert a["transfer"] == b["transfer"]
    assert b["transfer"]["url"] == "/api/regions/files/a-north/routing.tar.gz"
    assert b["url"] == "/api/regions/files/a-north/routing.tar"
    assert not (c / "routing.tar.gz").exists()  # never offered: never compressed


# ------------------------------------------------------------------ stale, skipped, option off, space


async def test_replaced_plain_file_makes_the_sidecar_stale_until_rebuilt(root, gmgr, tmp_path):
    d = make_region(root, "up-1", routing=True)
    await settle(gmgr)
    before = files_of(await catalogue(gmgr), "up-1")["routing"]["transfer"]
    gmgr._gzip.enabled = False  # hold the rebuild so the stale state can be looked at
    new = tar_bytes(4096, name=b"b.bin")
    await gmgr.save_upload("up-1", "routing", gen(new[:100], new[100:]), None)
    doc = await catalogue(gmgr)
    assert "transfer" not in files_of(doc, "up-1").get("routing", {})
    client = await sync_client(gmgr, tmp_path)
    try:
        resp = await client.get("/api/regions/files/up-1/routing.tar.gz", headers=AUTH)
        assert resp.status in (404, 503)  # never the old copy for the new file
        await settle(gmgr)  # hashed: the old sidecar is deleted, nothing is built while disabled
        assert not (d / "routing.tar.gz").exists()
        assert (await client.get("/api/regions/files/up-1/routing.tar.gz", headers=AUTH)).status == 404
        gmgr._gzip.enabled = True
        await settle(gmgr)
        after = files_of(await catalogue(gmgr), "up-1")["routing"]
        assert after["transfer"]["sha256"] != before["sha256"] and after["sha256"] == sha(new)
        assert gzip.decompress((d / "routing.tar.gz").read_bytes()) == new
        assert meta(d)["files"]["routing.tar.gz"]["sourceSha256"] == sha(new)
        resp = await client.get("/api/regions/files/up-1/routing.tar.gz", headers=AUTH)
        assert resp.status == 200 and gzip.decompress(await resp.read()) == new
    finally:
        await client.close()


async def test_a_sidecar_that_changed_on_disk_is_not_offered_and_is_deleted(root, gmgr):
    d = make_region(root, "chg-1", routing=True)
    await settle(gmgr)
    gmgr._gzip.enabled = False
    (d / "routing.tar.gz").write_bytes(b"not the recorded file")
    gmgr.invalidate()
    regions = await gmgr.rescan()
    assert "routing" not in regions[0].transfers and not (d / "routing.tar.gz").exists()
    assert not (d / R.TRANSFER_META).exists()  # its entry is dropped (it was the only one)


async def test_not_worth_it_is_recorded_as_skipped_and_not_rebuilt(root, gmgr):
    d = make_region(root, "rnd-1")
    (d / "places.sqlite").write_bytes(sqlite_bytes(100)[:100] + os.urandom(20000))
    old(d / "places.sqlite")
    await settle(gmgr)
    assert not (d / "places.sqlite.gz").exists() and not list(d.glob(".gzip-*"))
    plain = (d / "places.sqlite").read_bytes()
    assert meta(d)["files"]["places.sqlite.gz"] == {"skipped": True, "sourceSha256": sha(plain), "sourceSize": len(plain)}
    assert "transfer" not in files_of(await catalogue(gmgr), "rnd-1")["places"]
    builds = gmgr._gzip.builds
    for _ in range(3):
        await settle(gmgr)
    assert gmgr._gzip.builds == builds  # not tried again for the same file
    over = await gmgr.overview()
    assert next(f for f in over["regions"][0]["files"] if f["kind"] == "places")["transfer"] == {"state": "skipped"}


async def test_option_off_deletes_the_sidecars(root, monkeypatch):
    monkeypatch.setattr(G, "MIN_BYTES", 0)
    d = make_region(root, "off-1", routing=True, places=True)
    on = R.RegionManager(root, 1024 * 1024, min_free=0, transfer_gzip=True)
    await on.start()
    try:
        await settle(on)
    finally:
        await on.stop()
    assert (d / "routing.tar.gz").exists() and (d / R.TRANSFER_META).exists()
    off = R.RegionManager(root, 1024 * 1024, min_free=0, transfer_gzip=False)
    await off.start()
    try:
        await settle(off)
        assert all("transfer" not in f for f in files_of(await catalogue(off), "off-1").values())
        assert not list(d.glob("*.gz")) and not (d / R.TRANSFER_META).exists()
    finally:
        await off.stop()


async def test_not_enough_space_skips_quietly_and_builds_later(root, gmgr, monkeypatch):
    def full(path, need, min_free):
        raise NoSpaceError("full", needed=need)

    monkeypatch.setattr(G, "ensure_free", full)
    d = make_region(root, "full-1", routing=True)
    end = time.time() + 15  # every rescan asks again (cheap), so wait for the worker without rescanning
    while time.time() < end:
        gmgr.invalidate()
        regions = await gmgr.rescan()
        if all(f.sha256 for r in regions for f in r.files.values()) and not gmgr._hasher.busy():
            break
        await asyncio.sleep(0.05)
    while gmgr._gzip.busy() and time.time() < end:
        await asyncio.sleep(0.05)
    assert not gmgr._gzip.busy()
    assert not (d / "routing.tar.gz").exists() and not (d / R.TRANSFER_META).exists() and not list(d.glob(".gzip-*"))
    assert "transfer" not in files_of(await catalogue(gmgr), "full-1")["routing"]
    monkeypatch.undo()
    monkeypatch.setattr(G, "MIN_BYTES", 0)
    await settle(gmgr)
    assert "transfer" in files_of(await catalogue(gmgr), "full-1")["routing"]


async def test_delete_file_removes_its_sidecar(root, gmgr):
    d = make_region(root, "del-1", routing=True, places=True)
    await settle(gmgr)
    await gmgr.delete_file("del-1", "routing")
    assert not (d / "routing.tar.gz").exists() and (d / "places.sqlite.gz").exists()
    assert set(meta(d)["files"]) == {"places.sqlite.gz"}
    await gmgr.delete_file("del-1", "places")
    assert not list(d.glob("*.gz")) and not (d / R.TRANSFER_META).exists()


async def test_gzip_temps_are_cleaned(root, monkeypatch):
    d = make_region(root, "tmp-1")
    left = d / ".gzip-routing-0123abcd.part"
    left.write_bytes(b"x")  # fresh, but nothing compresses before the start: left over
    m = R.RegionManager(root, 1024 * 1024, min_free=0, transfer_gzip=True)
    await m.start()
    try:
        assert not left.exists()
        fresh, stale = d / ".gzip-places-0123abcd.part", d / ".gzip-routing-89abcdef.part"
        fresh.write_bytes(b"x")
        stale.write_bytes(b"x")
        old(stale, R.STALE_PART_SECONDS + 60)
        m.invalidate()
        await m.rescan()
        assert fresh.exists() and not stale.exists()
        assert (await m.overview())["regions"][0]["parts"] == []  # not shown as an upload/import part
    finally:
        await m.stop()


# ------------------------------------------------------------------ the build itself


async def hashed_folder(root, rid="b-1"):
    d = make_region(root, rid, routing=True)
    m = R.RegionManager(root, 1024 * 1024, min_free=0)
    await m.start()
    try:
        await settle(m)
    finally:
        await m.stop()
    return d


async def test_build_aborts_when_the_plain_file_changes(root, monkeypatch):
    monkeypatch.setattr(G, "MIN_BYTES", 0)
    monkeypatch.setattr(G, "CHUNK", 256)
    d = await hashed_folder(root)
    worker = G.GzipWorker(root, lambda: None)
    calls = []

    def touch(region, kind):
        calls.append(kind)
        if len(calls) == 2:
            t = time.time() - 100
            os.utime(d / "routing.tar", (t, t))

    worker.after_chunk = touch
    assert await asyncio.to_thread(worker._build, "b-1", "routing") is False
    assert len(calls) > 2 and not (d / "routing.tar.gz").exists() and not (d / R.TRANSFER_META).exists()
    assert not list(d.glob(".gzip-*"))


async def test_build_needs_the_hash_and_skips_a_built_sidecar(root, monkeypatch):
    monkeypatch.setattr(G, "MIN_BYTES", 0)
    d = make_region(root, "nh-1", routing=True)
    worker = G.GzipWorker(root, lambda: None)
    assert worker._build("nh-1", "routing") is False and not (d / "routing.tar.gz").exists()  # not hashed yet
    await hashed_folder(root, "nh-1")
    assert worker._build("nh-1", "routing") is True and (d / "routing.tar.gz").exists()
    assert worker._build("nh-1", "routing") is False  # built already
    worker.enabled = False
    os.unlink(d / "routing.tar.gz")
    assert worker._build("nh-1", "routing") is False  # disabled: nothing


def test_transfer_meta_ignores_malformed_entries(tmp_path):
    good_sha = "a" * 64
    (tmp_path / R.TRANSFER_META).write_text(json.dumps({"version": 1, "files": {
        "routing.tar.gz": {"size": 10, "mtime_ns": 5, "sha256": good_sha, "sourceSha256": good_sha, "sourceSize": 20},
        "places.sqlite.gz": {"size": "10", "mtime_ns": 5, "sha256": good_sha, "sourceSha256": good_sha, "sourceSize": 20},
        "map.pmtiles.gz": {"size": 10, "mtime_ns": 5, "sha256": good_sha, "sourceSha256": good_sha, "sourceSize": 20},
    }}))
    assert set(G.read_transfer_meta(tmp_path)) == {"routing.tar.gz"}
    (tmp_path / R.TRANSFER_META).write_text(json.dumps({"version": 2, "files": {}}))
    assert G.read_transfer_meta(tmp_path) == {}
    (tmp_path / R.TRANSFER_META).write_text("not json")
    assert G.read_transfer_meta(tmp_path) == {}


def test_sidecar_of_a_file_being_rehashed_is_kept_but_not_offered(tmp_path):
    (tmp_path / "routing.tar.gz").write_bytes(b"gz")
    st = os.stat(tmp_path / "routing.tar.gz")
    src = "b" * 64
    m = {"routing.tar.gz": {"size": 2, "mtime_ns": st.st_mtime_ns, "sha256": "c" * 64, "sourceSha256": src, "sourceSize": 99}}
    rehash = {"routing": R.FileInfo("routing", "routing.tar", 99, 1, None)}
    assert G.sidecars(tmp_path, rehash, m) == ({}, [], frozenset())
    known = {"routing": R.FileInfo("routing", "routing.tar", 99, 1, src)}
    valid, stale, _ = G.sidecars(tmp_path, known, m)
    assert valid["routing"].sha256 == "c" * 64 and stale == []
    other = {"routing": R.FileInfo("routing", "routing.tar", 99, 1, "d" * 64)}
    assert G.sidecars(tmp_path, other, m)[:2] == ({}, ["routing.tar.gz"])
    assert G.sidecars(tmp_path, {}, m)[:2] == ({}, ["routing.tar.gz"])  # orphaned: the plain file is gone


# ------------------------------------------------------------------ option


def test_option_default_on_and_passed_by_main(tmp_path):
    from pathlib import Path

    from car_media_hub.config import load_config
    from car_media_hub.main import make_region_manager
    from test_regions import options

    assert load_config(options(tmp_path)).region_transfer_gzip is True
    assert make_region_manager(load_config(options(tmp_path))).transfer_gzip is True
    assert make_region_manager(load_config(options(tmp_path, region_transfer_gzip=False))).transfer_gzip is False
    assert R.RegionManager(tmp_path / "r").transfer_gzip is False  # the constructor default keeps old catalogues
    root = Path(__file__).resolve().parents[1]
    config = (root / "config.yaml").read_text(encoding="utf-8")
    assert "  region_transfer_gzip: true\n" in config.replace("\r\n", "\n")
    assert "  region_transfer_gzip: bool\n" in config.replace("\r\n", "\n")
    for rel in ("translations/en.yaml", "translations/de.yaml", "DOCS.md"):
        assert "region_transfer_gzip" in (root / rel).read_text(encoding="utf-8")


# ------------------------------------------------------------------ serving


@pytest.fixture
async def gz_served(root, gmgr, tmp_path):
    make_region(root, "oberpfalz", routing=True, places=True)
    await settle(gmgr)
    client = await sync_client(gmgr, tmp_path)
    yield client
    await client.close()


URL_GZ = "/api/regions/files/oberpfalz/routing.tar.gz"


async def test_gz_is_served_like_the_plain_files(root, gmgr, gz_served):
    raw = (root / "oberpfalz" / "routing.tar.gz").read_bytes()
    etag = f'"{sha(raw)}"'
    resp = await gz_served.get(URL_GZ, headers=AUTH)
    assert resp.status == 200 and await resp.read() == raw
    assert resp.headers["Content-Length"] == str(len(raw)) and resp.headers["ETag"] == etag
    assert resp.headers["Accept-Ranges"] == "bytes" and resp.headers["X-Max-Streams"] == "8"
    assert resp.headers["Content-Type"] == "application/octet-stream"
    head = await gz_served.head(URL_GZ, headers=AUTH)
    assert head.status == 200 and head.headers["ETag"] == etag and await head.read() == b""
    part = await gz_served.get(URL_GZ, headers={**AUTH, "Range": "bytes=10-"})
    assert part.status == 206 and await part.read() == raw[10:]
    assert part.headers["Content-Range"] == f"bytes 10-{len(raw) - 1}/{len(raw)}"
    ok = await gz_served.get(URL_GZ, headers={**AUTH, "Range": "bytes=0-9", "If-Range": etag})
    assert ok.status == 206 and await ok.read() == raw[:10]
    stale = await gz_served.get(URL_GZ, headers={**AUTH, "Range": "bytes=0-9", "If-Range": '"' + "0" * 64 + '"'})
    assert stale.status == 200 and await stale.read() == raw
    bad = await gz_served.get(URL_GZ, headers={**AUTH, "Range": f"bytes={len(raw)}-"})
    assert bad.status == 416 and bad.headers["Content-Range"] == f"bytes */{len(raw)}"
    places = await gz_served.get("/api/regions/files/oberpfalz/places.sqlite.gz", headers=AUTH)
    assert places.status == 200
    for name in ("map.pmtiles.gz", "routing.tar.gz.part", "transfer.json", "files.json", "routing.gz"):
        assert (await gz_served.get(f"/api/regions/files/oberpfalz/{name}", headers=AUTH)).status == 404
    assert (await gz_served.get(URL_GZ)).status == 401


async def test_gz_downloads_share_the_stream_budget(gmgr, gz_served):
    held = [gmgr.streams.begin("127.0.0.1", ("x", i)) for i in range(8)]
    assert all(held)
    try:
        busy = await gz_served.get(URL_GZ, headers=AUTH)
        assert busy.status == 503 and busy.headers["X-Max-Streams"] == "8" and busy.headers["Retry-After"]
        assert (await gz_served.head(URL_GZ, headers=AUTH)).status == 200  # HEAD takes no slot
    finally:
        for s in held:
            gmgr.streams.end(s)
    assert (await gz_served.get(URL_GZ, headers=AUTH)).status == 200


async def test_option_off_serves_no_gz(root, tmp_path, monkeypatch):
    monkeypatch.setattr(G, "MIN_BYTES", 0)
    make_region(root, "srv-off", routing=True)
    m = R.RegionManager(root, 1024 * 1024, min_free=0)
    await m.start()
    client = await sync_client(m, tmp_path)
    try:
        await settle(m)
        (root / "srv-off" / "routing.tar.gz").write_bytes(gzip.compress(b"x"))  # left by hand: never served
        resp = await client.get("/api/regions/files/srv-off/routing.tar.gz", headers=AUTH)
        assert resp.status in (404, 503)
    finally:
        await client.close()
        await m.stop()
