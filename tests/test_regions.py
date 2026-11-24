"""Offline map regions: storage, catalogue (shape of the car app's regions.json v2), hashing, files with ranges,
uploads, the admin API and the storage-dir option. The import from a URL is in test_regions_import.py."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from yarl import URL

from car_media_hub import regions as R
from car_media_hub.config import load_config
from car_media_hub.regions_api import Unsatisfiable, parse_range
from car_media_hub.regions_web import register_region_pages
from car_media_hub.sync import LibraryIndex, create_sync_app

FIXTURE = Path(__file__).parent / "fixtures" / "app_regions.json"
TOKEN = "t" * 24
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ADMIN = {"X-Requested-With": "car-media-hub", "Content-Type": "application/json"}


def pmtiles(size=4096, fill=b"x"):
    return b"PMTiles\x03" + fill * (size - 8)


def sqlite_bytes(size=2048):
    return b"SQLite format 3\x00" + (4096).to_bytes(2, "big") + b"\x00" * (size - 18)


def tar_bytes(size=2048, name=b"a.bin"):
    head = bytearray(512)
    head[: len(name)] = name
    head[257:263] = b"ustar\x00"
    head[148:156] = b" " * 8
    head[148:156] = b"%06o\x00 " % sum(head)  # octal checksum of the header (the field counted as spaces)
    return bytes(head) + b"\x00" * (size - 512)


def old(path: Path, age=3600):
    t = time.time() - age
    os.utime(path, (t, t))


def make_region(root: Path, rid="oberpfalz", *, map_=True, routing=False, places=False, manifest=True, group=None, age=3600):
    d = root / rid
    d.mkdir(parents=True, exist_ok=True)
    for ok, name, data in ((map_, "map.pmtiles", pmtiles()), (routing, "routing.tar", tar_bytes()), (places, "places.sqlite", sqlite_bytes())):
        if ok:
            (d / name).write_bytes(data)
            old(d / name, age)
    if manifest:
        m = {"id": rid, "name": {"de": "Oberpfalz", "en": "Upper Palatinate"}, "bounds": [11.6, 48.8, 13.0, 50.0],
             "version": "2026-11", "source": "Geofabrik", "license": "ODbL", "created": "2026-11-16T10:00:00Z"}
        if group:
            m["routingGroup"] = group
        (d / "manifest.json").write_text(json.dumps(m))
    return d


async def wait_hashed(mgr, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        mgr.invalidate()
        regions = await mgr.rescan()
        if all(f.sha256 for r in regions for f in r.files.values()):
            return regions
        await asyncio.sleep(0.05)
    raise AssertionError("hashing did not finish")


class State(dict):
    def get_state(self, key):
        return self.get(key)

    def set_state(self, key, value):
        self[key] = value


@pytest.fixture
def root(tmp_path):
    return tmp_path / "regions"


@pytest.fixture
async def mgr(root):
    m = R.RegionManager(root, 1024 * 1024, min_free=0)
    await m.start()
    yield m
    await m.stop()


async def sync_client(mgr, tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir(exist_ok=True)
    app = create_sync_app(LibraryIndex(lib, State()), TOKEN, regions=mgr)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


# ------------------------------------------------------------------ the app's parser (RegionCatalogue.kt), ported

BASE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
EXT = {"map": ".pmtiles", "routing": ".tar", "places": ".sqlite"}


def app_parse(doc):
    """Same acceptance rules as RegionCatalogue.parsePacks: entries that fail are skipped as a whole."""
    seen, out = set(), []
    for dto in doc["regions"]:
        if not dto.get("id") or not isinstance(dto.get("name"), str) or not dto["name"].strip():
            continue
        files = dto.get("files")
        if not files:
            continue
        kinds = [f["kind"] for f in files]
        if any(k not in EXT for k in kinds) or len(set(kinds)) != len(kinds):
            continue
        ok = True
        for f in files:
            name, url = f["name"], f["url"]
            ok &= (len(name) > len(EXT[f["kind"]]) and name.endswith(EXT[f["kind"]]) and BASE.fullmatch(name) is not None
                   and ".." not in name and isinstance(f["bytes"], int) and f["bytes"] > 0
                   and re.fullmatch(r"[0-9a-fA-F]{64}", f["sha256"]) is not None
                   and url.strip() != "" and not re.search(r"[\s\\]", url) and ".." not in url and "://" not in url)
        if ok and dto["id"] not in seen:
            seen.add(dto["id"])
            out.append(dto)
    return out


def test_fixture_is_accepted_by_the_ported_parser():
    assert [r["id"] for r in app_parse(json.loads(FIXTURE.read_text()))] == ["mittelfranken", "oberpfalz"]


async def test_catalogue_matches_the_apps_schema(root, mgr, tmp_path):
    make_region(root, "oberpfalz", routing=True, places=True)
    make_region(root, "mittelfranken", routing=True, places=True, group="franken")
    make_region(root, "nomap", map_=False, routing=True)
    await wait_hashed(mgr)
    client = await sync_client(mgr, tmp_path)
    try:
        resp = await client.get("/api/regions/catalogue", headers=AUTH)
        assert resp.status == 200 and resp.headers["Content-Type"].startswith("application/json")
        doc = await resp.json()
    finally:
        await client.close()
    fixture = json.loads(FIXTURE.read_text())
    assert doc["version"] == fixture["version"] == 2
    assert [r["id"] for r in doc["regions"]] == ["mittelfranken", "oberpfalz"]  # no map: not listed
    assert [r["id"] for r in app_parse(doc)] == ["mittelfranken", "oberpfalz"]  # the app keeps all of them
    fx_region_keys = set(fixture["regions"][0])
    fx_file_keys = set(fixture["regions"][0]["files"][0])
    for region in doc["regions"]:
        assert fx_region_keys <= set(region)
        assert isinstance(region["name"], str)
        assert region["state"] == "ready" and region["version"] == "2026-11"
        assert region["names"] == {"de": "Oberpfalz", "en": "Upper Palatinate"} and region["bounds"] == [11.6, 48.8, 13.0, 50.0]
        assert [f["kind"] for f in region["files"]] == ["map", "routing", "places"]
        for f in region["files"]:
            assert fx_file_keys <= set(f) and f["bytes"] == f["sizeBytes"] > 0
            assert f["url"].startswith("/api/regions/files/") and f["url"].endswith(("map.pmtiles", "routing.tar", "places.sqlite"))
            assert re.fullmatch(r"[0-9a-f]{64}", f["sha256"])
    ober = next(r for r in doc["regions"] if r["id"] == "oberpfalz")
    assert [f["name"] for f in ober["files"]] == ["oberpfalz.pmtiles", "oberpfalz-tiles.tar", "oberpfalz-places.sqlite"]
    assert ober["files"][0]["sha256"] == hashlib.sha256(pmtiles()).hexdigest()
    mfr = next(r for r in doc["regions"] if r["id"] == "mittelfranken")
    assert re.fullmatch(r"franken-[0-9a-f]{8}-tiles\.tar", mfr["files"][1]["name"])


async def test_catalogue_needs_the_token(root, mgr, tmp_path):
    client = await sync_client(mgr, tmp_path)
    try:
        assert (await client.get("/api/regions/catalogue")).status == 401
        assert (await client.get("/api/regions/files/oberpfalz/map.pmtiles")).status == 401
        assert (await client.post("/api/regions/catalogue", headers=AUTH)).status == 405
        assert (await client.get("/api/regions/catalogue", headers={"Authorization": "Bearer " + "x" * 24})).status == 401
    finally:
        await client.close()


async def test_routing_group_shares_one_archive(root, mgr):
    make_region(root, "a-north", routing=True, group="nord")
    make_region(root, "b-south", routing=False, group="nord")
    make_region(root, "c-alone", routing=True)
    await wait_hashed(mgr)
    doc = json.loads(await mgr.catalogue_body())
    by = {r["id"]: r for r in doc["regions"]}
    a, b = by["a-north"]["files"][1], by["b-south"]["files"][1]
    assert a["name"] == b["name"] == f"nord-{a['sha256'][:8]}-tiles.tar"
    assert a["url"] == b["url"] == "/api/regions/files/a-north/routing.tar"
    assert a["sha256"] == b["sha256"] and a["bytes"] == b["bytes"]
    assert by["c-alone"]["files"][1]["name"] == "c-alone-tiles.tar"


async def test_hash_state_then_ready_and_persisted(root, mgr):
    make_region(root)
    mgr._hasher.stop()  # no worker: the hash stays pending
    mgr.invalidate()
    doc = json.loads(await mgr.catalogue_body())
    entry = doc["regions"][0]
    assert entry["state"] == "hashing" and entry["files"] == [] and entry["hashing"] == [{"kind": "map", "progress": 0.0}]
    assert app_parse(doc) == []  # the app does not offer a region whose hash is pending
    mgr._hasher = R.HashWorker(root, mgr._hash_done)
    mgr.invalidate()
    await wait_hashed(mgr)
    doc = json.loads(await mgr.catalogue_body())
    assert doc["regions"][0]["state"] == "ready" and "hashing" not in doc["regions"][0]
    cache = json.loads((root / "oberpfalz" / "files.json").read_text())
    assert cache["files"]["map.pmtiles"]["sha256"] == hashlib.sha256(pmtiles()).hexdigest()
    # a second manager reads the persisted hash without hashing anything
    other = R.RegionManager(root, 1 << 20)
    other._hasher.stop()
    assert json.loads(await other.catalogue_body())["regions"][0]["state"] == "ready"
    # changing the file invalidates the cached hash
    (root / "oberpfalz" / "map.pmtiles").write_bytes(pmtiles(5000, b"y"))
    old(root / "oberpfalz" / "map.pmtiles")
    other.invalidate()
    assert json.loads(await other.catalogue_body())["regions"][0]["state"] == "hashing"


async def test_hash_worker_reports_progress_and_never_blocks(root):
    make_region(root)
    big = root / "oberpfalz" / "map.pmtiles"
    big.write_bytes(pmtiles(R.HASH_CHUNK * 3))
    old(big)
    done = []
    worker = R.HashWorker(root, lambda: done.append(1))
    t0 = time.perf_counter()
    worker.submit("oberpfalz", "map.pmtiles")
    assert time.perf_counter() - t0 < 0.5  # submit returns at once
    worker.submit("oberpfalz", "map.pmtiles")  # a duplicate is ignored
    assert worker.progress("oberpfalz", "map.pmtiles") is not None or done
    for _ in range(100):
        if done:
            break
        await asyncio.sleep(0.05)
    worker.stop()
    assert len(done) == 1 and not worker.busy()
    assert R.read_hash_cache(root / "oberpfalz")["map.pmtiles"]["sha256"] == hashlib.sha256(big.read_bytes()).hexdigest()


def test_hash_worker_ignores_names_outside_the_whitelist(root):
    make_region(root)
    (root / "oberpfalz" / "secret.txt").write_text("x")
    worker = R.HashWorker(root, lambda: None)
    worker._hash_one("oberpfalz", "secret.txt")
    worker._hash_one("../etc", "map.pmtiles")
    assert not (root / "oberpfalz" / "files.json").exists()


async def test_fresh_file_waits_until_the_copy_is_finished(root, mgr):
    make_region(root, age=0)  # just written: may still be copied
    mgr.invalidate()
    entry = json.loads(await mgr.catalogue_body())["regions"][0]
    assert entry["state"] == "hashing" and mgr._hasher.progress("oberpfalz", "map.pmtiles") is None


async def test_legacy_pack_names_and_missing_manifest(root, mgr):
    d = root / "oberpfalz"
    d.mkdir(parents=True)
    (d / "oberpfalz.pmtiles").write_bytes(pmtiles())
    (d / "oberpfalz-tiles.tar").write_bytes(tar_bytes())
    for n in ("oberpfalz.pmtiles", "oberpfalz-tiles.tar"):
        old(d / n)
    (root / ".hidden").mkdir()
    (root / "Bad_ID").mkdir()
    (root / "file.txt").write_text("x")
    regions = await mgr.rescan()
    assert [r.id for r in regions] == ["oberpfalz"]
    assert (d / "map.pmtiles").exists() and (d / "routing.tar").exists()
    assert regions[0].manifest_problem and regions[0].manifest["name"]["en"] == "oberpfalz"


# ------------------------------------------------------------------ files, ranges, whitelist


@pytest.fixture
async def served(root, mgr, tmp_path):
    make_region(root, routing=True, places=True)
    await wait_hashed(mgr)
    client = await sync_client(mgr, tmp_path)
    yield client
    await client.close()


URL_MAP = "/api/regions/files/oberpfalz/map.pmtiles"


async def test_full_download_head_and_headers(served):
    data = pmtiles()
    resp = await served.get(URL_MAP, headers=AUTH)
    assert resp.status == 200 and await resp.read() == data
    assert resp.headers["Content-Length"] == str(len(data)) and resp.headers["Accept-Ranges"] == "bytes"
    assert resp.headers["ETag"].startswith('"') and "GMT" in resp.headers["Last-Modified"]
    head = await served.head(URL_MAP, headers=AUTH)
    assert head.status == 200 and head.headers["Content-Length"] == str(len(data)) and head.headers["ETag"] == resp.headers["ETag"]
    assert await head.read() == b""
    assert (await served.get("/api/regions/files/oberpfalz/places.sqlite", headers=AUTH)).status == 200
    assert (await served.get("/api/regions/files/oberpfalz/routing.tar", headers=AUTH)).status == 200


@pytest.mark.parametrize("header,start,end", [
    ("bytes=0-9", 0, 9), ("bytes=100-", 100, 4095), ("bytes=4090-99999", 4090, 4095), ("bytes=-16", 4080, 4095),
    ("bytes=4095-4095", 4095, 4095), (" bytes=5-6 ", 5, 6),
])
async def test_single_range_resume(served, header, start, end):
    data = pmtiles()
    resp = await served.get(URL_MAP, headers={**AUTH, "Range": header})
    assert resp.status == 206 and await resp.read() == data[start:end + 1]
    assert resp.headers["Content-Range"] == f"bytes {start}-{end}/{len(data)}"
    assert resp.headers["Content-Length"] == str(end - start + 1)


@pytest.mark.parametrize("header", ["bytes=0-1,5-6", "bytes=9-3", "bytes=abc", "items=0-5", "bytes=", "bytes=-", "bytes=1-2-3", "bytes=0x1-5"])
async def test_multi_and_invalid_ranges_are_ignored(served, header):
    resp = await served.get(URL_MAP, headers={**AUTH, "Range": header})
    assert resp.status == 200 and await resp.read() == pmtiles()
    assert "Content-Range" not in resp.headers


@pytest.mark.parametrize("header", ["bytes=4096-", "bytes=99999-100000", "bytes=-0"])
async def test_unsatisfiable_range_is_416(served, header):
    resp = await served.get(URL_MAP, headers={**AUTH, "Range": header})
    assert resp.status == 416 and resp.headers["Content-Range"] == "bytes */4096"


async def test_if_range(served):
    first = await served.get(URL_MAP, headers=AUTH)
    etag, lm = first.headers["ETag"], first.headers["Last-Modified"]
    ok = await served.get(URL_MAP, headers={**AUTH, "Range": "bytes=10-19", "If-Range": etag})
    assert ok.status == 206 and (await ok.read()) == pmtiles()[10:20]
    by_date = await served.get(URL_MAP, headers={**AUTH, "Range": "bytes=10-19", "If-Range": lm})
    assert by_date.status == 206
    stale = await served.get(URL_MAP, headers={**AUTH, "Range": "bytes=10-19", "If-Range": '"deadbeef"'})
    assert stale.status == 200 and len(await stale.read()) == 4096  # the file changed: the whole file again
    weak = await served.get(URL_MAP, headers={**AUTH, "Range": "bytes=10-19", "If-Range": "W/" + etag})
    assert weak.status == 200
    no_range = await served.get(URL_MAP, headers={**AUTH, "If-Range": etag})
    assert no_range.status == 200


def test_parse_range_unit():
    assert parse_range("bytes=1-2", 10) == (1, 2)
    assert parse_range("bytes=1-", 10) == (1, 9)
    assert parse_range("bytes=-3", 10) == (7, 9)
    assert parse_range("bytes=-30", 10) == (0, 9)
    assert parse_range("bytes=1-2,4-5", 10) is None
    with pytest.raises(Unsatisfiable):
        parse_range("bytes=" + "9" * 5000 + "-", 10)  # huge numbers are no crash, just behind the end
    with pytest.raises(Unsatisfiable):
        parse_range("bytes=10-", 10)


@pytest.mark.parametrize("path", [
    "/api/regions/files/oberpfalz/manifest.json", "/api/regions/files/oberpfalz/files.json", "/api/regions/files/oberpfalz/other.txt",
    "/api/regions/files/oberpfalz/MAP.PMTILES", "/api/regions/files/oberpfalz/", "/api/regions/files/oberpfalz",
    "/api/regions/files/OBERPFALZ/map.pmtiles", "/api/regions/files/o/map.pmtiles", "/api/regions/files/-ab/map.pmtiles",
    "/api/regions/files/%2e%2e/oberpfalz/map.pmtiles", "/api/regions/files/oberpfalz/%2e%2e%2fmap.pmtiles",
    "/api/regions/files/oberpfalz%2Fx/map.pmtiles", "/api/regions/files/oberpfalz/map.pmtiles/extra",
    "/api/regions/files/oberpfalz/map.pmtiles%00", "/api/regions/files/oberpfalz%5C..%5Cx/map.pmtiles", "/api/regions/files//map.pmtiles",
    "/api/regions/files/" + "a" * 50 + "/map.pmtiles", "/api/regions/files/oberpfalz/%6dap.pmtiles",
])
async def test_whitelist_and_traversal(served, path):
    resp = await served.get(URL(path, encoded=True), headers=AUTH)
    assert resp.status == 404, path
    body = await resp.read()
    assert b"PMTiles" not in body and b"SQLite" not in body


async def test_raw_dot_segments_are_refused(served):
    for target in ("/api/regions/files/oberpfalz/../oberpfalz/map.pmtiles", "/api/regions/files/../files/oberpfalz/map.pmtiles",
                   "/api/regions/files/oberpfalz/./map.pmtiles", "/api/regions/files/oberpfalz/..%2fmap.pmtiles"):
        reader, writer = await asyncio.open_connection("127.0.0.1", served.port)
        writer.write((f"GET {target} HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\nConnection: close\r\n\r\n").encode())
        await writer.drain()
        raw = await reader.read()
        writer.close()
        assert raw.startswith(b"HTTP/1.1 404") and b"PMTiles" not in raw, (target, raw[:60])


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows (covered in WSL)")
async def test_symlinks_are_never_followed(root, mgr, tmp_path, served):
    secret = tmp_path / "secret.pmtiles"
    secret.write_bytes(pmtiles(100, b"S"))
    make_region(root, "linked", map_=False)
    os.symlink(secret, root / "linked" / "map.pmtiles")
    assert (await served.get("/api/regions/files/linked/map.pmtiles", headers=AUTH)).status == 404
    os.symlink(root / "oberpfalz", root / "dirlink")
    assert (await served.get("/api/regions/files/dirlink/map.pmtiles", headers=AUTH)).status == 404
    # a link is neither listed nor hashed
    regions = await mgr.rescan()
    assert [r.id for r in regions] == ["linked", "oberpfalz"] and regions[0].files == {}
    assert any("not a regular file" in p for p in regions[0].problems)
    os.unlink(root / "oberpfalz" / "places.sqlite")
    os.symlink(secret, root / "oberpfalz" / "places.sqlite")
    assert (await served.get("/api/regions/files/oberpfalz/places.sqlite", headers=AUTH)).status == 404


async def test_a_directory_in_place_of_a_file_is_404(root, served):
    os.unlink(root / "oberpfalz" / "routing.tar")
    (root / "oberpfalz" / "routing.tar").mkdir()
    assert (await served.get("/api/regions/files/oberpfalz/routing.tar", headers=AUTH)).status == 404


async def test_download_cap_answers_503(served, mgr):
    taken = [mgr.begin_download(("127.0.0.1", "other", f"f{i}"), None) for i in range(mgr.streams.per_client)]
    resp = await served.get(URL_MAP, headers=AUTH)
    assert resp.status == 503 and resp.headers["Retry-After"] == "2" and resp.headers["X-Max-Streams"] == "8"
    assert (await served.head(URL_MAP, headers=AUTH)).status == 200
    for stream in taken:
        mgr.end_download(stream)
    assert mgr.active_downloads == 0


async def test_unreadable_storage_is_503_not_an_empty_catalogue(root, mgr, tmp_path):
    mgr._ready_error = "cannot read"
    client = await sync_client(mgr, tmp_path)
    try:
        assert (await client.get("/api/regions/catalogue", headers=AUTH)).status == 503
    finally:
        await client.close()


# ------------------------------------------------------------------ manifest, create, delete


def test_manifest_validation():
    ok = R.normalise_manifest({"name": "Nur Name", "bounds": [1, 2, 3, 4]}, "abc")
    assert ok["name"] == {"de": "Nur Name", "en": "Nur Name"} and ok["bounds"] == [1.0, 2.0, 3.0, 4.0]
    assert R.normalise_manifest({"name": {"en": "E"}}, "abc")["name"] == {"de": "E", "en": "E"}
    for bad in (
        {"name": ""}, {"name": {"de": "", "en": ""}}, {"name": 5}, [], {"name": "x", "id": "other"},
        {"name": "x", "bounds": [1, 2, 3]}, {"name": "x", "bounds": [5, 2, 3, 4]}, {"name": "x", "bounds": [1, 2, 3, 99]},
        {"name": "x", "bounds": [1, 2, "a", 4]}, {"name": "x", "bounds": [1, 2, float("nan"), 4]},
        {"name": "x", "routingGroup": "../x"}, {"name": "x", "version": "v" * 41}, {"name": "x\x00"},
    ):
        with pytest.raises(R.RegionError):
            R.normalise_manifest(bad, "abc")


@pytest.mark.parametrize("rid", ["", "a", "A1", "ab/cd", "..", "ab..", "-ab", "_ab", "a b", "x" * 42, "ab\n", "äö", ".hidden"])
async def test_bad_region_ids_are_refused(mgr, rid):
    with pytest.raises(R.RegionError):
        await mgr.create_region(rid, {"name": "x"})


async def test_create_edit_delete(root, mgr):
    await mgr.create_region("neu-1", {"name": {"de": "Neu", "en": "New"}, "version": "1"})
    with pytest.raises(R.RegionError) as dup:
        await mgr.create_region("neu-1", {"name": "x"})
    assert dup.value.status == 409
    await mgr.update_manifest("neu-1", {"name": "Zwei", "bounds": [1, 2, 3, 4]})
    assert json.loads((root / "neu-1" / "manifest.json").read_text())["name"]["en"] == "Zwei"
    with pytest.raises(R.RegionError) as unknown:
        await mgr.update_manifest("zzz", {"name": "x"})
    assert unknown.value.status == 404
    (root / "neu-1" / "map.pmtiles").write_bytes(pmtiles())
    await mgr.delete_region("neu-1")
    assert not (root / "neu-1").exists()
    assert (await mgr.rescan()) == []


# ------------------------------------------------------------------ upload


async def gen(*parts):
    for p in parts:
        yield p


async def test_upload_stores_atomically_and_hashes(root, mgr):
    await mgr.create_region("up-1", {"name": "Up"})
    data = pmtiles(3000)
    size = await mgr.save_upload("up-1", "map", gen(data[:10], data[10:]), len(data))
    assert size == 3000 and (root / "up-1" / "map.pmtiles").read_bytes() == data
    assert not [p for p in (root / "up-1").iterdir() if p.name.startswith(".upload")]
    for kind, blob in (("routing", tar_bytes()), ("places", sqlite_bytes())):
        await mgr.save_upload("up-1", kind, gen(blob[:100], blob[100:]), None)
    assert (root / "up-1" / "routing.tar").exists() and (root / "up-1" / "places.sqlite").exists()
    assert not mgr._busy


@pytest.mark.parametrize("kind,blob", [
    ("map", b"%PDF-1.7" + b"\x00" * 200), ("map", b"PMTiles\x02" + b"\x00" * 200), ("map", b"PMT"),
    ("places", b"SQLite format 4\x00" + b"\x00" * 100), ("places", b"not sqlite at all"),
    ("routing", b"\x00" * 600), ("routing", b"short"), ("map", b""),
])
async def test_upload_validation_rejects_and_keeps_old_file(root, mgr, kind, blob):
    await mgr.create_region("up-2", {"name": "Up"})
    good = {"map": pmtiles(500), "routing": tar_bytes(), "places": sqlite_bytes()}[kind]
    await mgr.save_upload("up-2", kind, gen(good), None)
    name = R.FILE_NAMES[kind]
    before = (root / "up-2" / name).read_bytes()
    with pytest.raises(R.RegionError) as exc:
        await mgr.save_upload("up-2", kind, gen(blob[:7], blob[7:]), None)
    assert exc.value.status == 415
    assert (root / "up-2" / name).read_bytes() == before
    assert not [p for p in (root / "up-2").iterdir() if p.name.startswith(".upload")]
    assert not mgr._busy


async def test_upload_size_limit_and_unknown_kind_and_busy(root, mgr):
    await mgr.create_region("up-3", {"name": "Up"})
    with pytest.raises(R.RegionError) as big:  # declared too large: refused before reading
        await mgr.save_upload("up-3", "map", gen(), mgr.max_file_bytes + 1)
    assert big.value.status == 413
    with pytest.raises(R.RegionError) as streamed:  # no (or a lying) Content-Length: stopped while streaming
        await mgr.save_upload("up-3", "map", gen(pmtiles(1000), b"z" * mgr.max_file_bytes), None)
    assert streamed.value.status == 413 and not (root / "up-3" / "map.pmtiles").exists()
    assert not [p for p in (root / "up-3").iterdir() if p.name.startswith(".upload")]
    with pytest.raises(R.RegionError) as kind:
        await mgr.save_upload("up-3", "evil.sh", gen(b"x"), None)
    assert kind.value.status == 404
    with pytest.raises(R.RegionError) as unknown:
        await mgr.save_upload("nope", "map", gen(pmtiles()), None)
    assert unknown.value.status == 404
    mgr._busy.add(("up-3", "map"))
    with pytest.raises(R.RegionError) as busy:
        await mgr.save_upload("up-3", "map", gen(pmtiles()), None)
    assert busy.value.status == 409
    with pytest.raises(R.RegionError):
        await mgr.delete_region("up-3")
    mgr._busy.clear()


async def test_upload_disk_space_guard(root):
    m = R.RegionManager(root, 1 << 40, min_free=1 << 60)
    await m.start()
    await m.create_region("up-4", {"name": "x"})
    with pytest.raises(R.RegionError) as exc:
        await m.save_upload("up-4", "map", gen(pmtiles()), 4096)
    assert exc.value.status == 507
    await m.stop()


# ------------------------------------------------------------------ admin page API (CSRF, upload through HTTP)


@pytest.fixture
async def admin(root, mgr):
    app = web.Application()
    register_region_pages(app, mgr)
    client = TestClient(TestServer(app))
    await client.start_server()
    yield client
    await client.close()


async def test_admin_page_and_list(admin, mgr):
    page = await admin.get("/regions")
    text = await page.text()
    assert page.status == 200 and "regions/api/list" in text and 'href="./regions"' in text and 'href="./trips"' in text
    j = await (await admin.get("/regions/api/list")).json()
    assert j["ok"] and j["regions"] == [] and j["maxFileBytes"] == mgr.max_file_bytes


async def test_admin_posts_need_the_page_header_and_json(admin):
    body = json.dumps({"id": "ab-1", "name_en": "X"})
    assert (await admin.post("/regions/api/create", data=body, headers={"Content-Type": "application/json"})).status == 400  # no header
    assert (await admin.post("/regions/api/create", data=body, headers={"X-Requested-With": "car-media-hub", "Content-Type": "text/plain"})).status == 400
    cross = await admin.post("/regions/api/create", data=body, headers={**ADMIN, "Sec-Fetch-Site": "cross-site"})
    assert cross.status == 403
    foreign = await admin.post("/regions/api/create", data=body, headers={**ADMIN, "Origin": "https://evil.example"})
    assert foreign.status == 403
    assert (await admin.put("/regions/api/ab-1/upload/map", data=pmtiles())).status == 400
    same = await admin.post("/regions/api/create", data=body, headers={
        **ADMIN, "Origin": "https://ha.example", "X-Forwarded-Host": "ha.example:8123", "Sec-Fetch-Site": "same-origin"})
    assert same.status == 200
    assert (await admin.post("/regions/api/create", data=body, headers=ADMIN)).status == 409


async def test_admin_full_flow_upload_and_delete(admin, root):
    h = {"X-Requested-With": "car-media-hub"}
    created = await admin.post("/regions/api/create", data=json.dumps({"id": "fl-1", "name_en": "Flow", "bounds": "1,2,3,4"}), headers=ADMIN)
    assert created.status == 200
    ok = await admin.put("/regions/api/fl-1/upload/map", data=pmtiles(), headers=h)
    assert ok.status == 200 and (await ok.json())["size"] == 4096
    bad = await admin.put("/regions/api/fl-1/upload/places", data=b"nope" * 10, headers=h)
    assert bad.status == 415 and not (root / "fl-1" / "places.sqlite").exists()
    toobig = await admin.put("/regions/api/fl-1/upload/routing", data=b"\x00" * (1024 * 1024 + 5), headers=h)
    assert toobig.status == 413
    assert (await admin.put("/regions/api/fl-1/upload/passwd", data=b"x", headers=h)).status == 404
    assert (await admin.post("/regions/api/fl-1/files/map/delete", data="{}", headers=ADMIN)).status == 200
    assert (await admin.post("/regions/api/fl-1/delete", data="{}", headers=ADMIN)).status == 200
    assert (await admin.post("/regions/api/fl-1/delete", data="{}", headers=ADMIN)).status == 404


async def test_the_page_is_not_available_without_a_manager():
    app = web.Application()
    register_region_pages(app, None)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        assert (await client.get("/regions/api/list")).status == 503
        assert (await client.post("/regions/api/rescan", data="{}", headers=ADMIN)).status == 503
    finally:
        await client.close()


# ------------------------------------------------------------------ storage-dir option


def options(tmp_path, **extra):
    base = {"inbox_dir": "/share/car_media_hub/inbox", "staging_dir": "/media/.car_media_hub/staging", "library_dir": "/media/music"}
    base.update(extra)
    path = tmp_path / "options.json"
    path.write_text(json.dumps(base))
    return path


def test_region_storage_dir_default_and_valid(tmp_path):
    cfg = load_config(options(tmp_path))
    assert str(cfg.region_storage_dir).replace("\\", "/") == "/share/car_media_hub/regions" and cfg.region_max_file_mb == 8192
    cfg = load_config(options(tmp_path, region_storage_dir="/media/maps/regions", region_max_file_mb=100))
    assert cfg.region_max_file_mb == 100
    assert load_config(options(tmp_path, region_storage_dir="/share/regions")).region_storage_dir


@pytest.mark.parametrize("value", [
    "regions", "relative/path", "/", "/share", "/media", "/config", "/data", "/etc/regions", "/share/../etc", "/share/a/../../etc",
    "/media/music", "/media/music/regions", "/share/car_media_hub/inbox", "/share/car_media_hub/inbox/regions",
    "/media/.car_media_hub/staging", "/media/.car_media_hub", "/tmp/regions", "",
])
def test_region_storage_dir_is_validated(tmp_path, value):
    with pytest.raises(ValueError):
        load_config(options(tmp_path, region_storage_dir=value))


@pytest.mark.parametrize("value", [0, -1, 200_001, "big", True, 1.5])
def test_region_max_file_mb_is_validated(tmp_path, value):
    with pytest.raises(ValueError):
        load_config(options(tmp_path, region_max_file_mb=value))


def test_region_dir_must_not_contain_the_inbox(tmp_path):
    with pytest.raises(ValueError):  # the parent of the inbox: the inbox would import and delete the regions
        load_config(options(tmp_path, region_storage_dir="/share/car_media_hub"))
    cfg = load_config(options(tmp_path, region_storage_dir="/share/car_media_hub/inbox", inbox_enabled=False))
    assert cfg.region_storage_dir


# ------------------------------------------------------------------ misc


async def test_snapshot_cache_is_reused_and_refreshed(root, mgr):
    make_region(root)
    await wait_hashed(mgr)
    gen_before = mgr._generation
    await mgr.catalogue_body()
    await mgr.catalogue_body()
    assert mgr._generation == gen_before  # no rescan for unchanged state
    mgr.invalidate()
    await mgr.catalogue_body()
    assert mgr._generation == gen_before + 1


async def test_start_creates_the_folder_and_survives_a_bad_one(tmp_path):
    m = R.RegionManager(tmp_path / "new" / "regions", 1 << 20)
    await m.start()
    assert (tmp_path / "new" / "regions").is_dir()
    await m.stop()
    blocker = tmp_path / "file"
    blocker.write_text("x")
    m2 = R.RegionManager(blocker / "regions", 1 << 20)
    await m2.start()  # cannot create: reported, not raised
    assert m2.problem
    assert json.loads(await m2.catalogue_body())["regions"] == []
    await m2.stop()
