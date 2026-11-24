"""Charger files on the sync API: no whole-file read, no re-hash per request, sendfile with Range and a strong ETag."""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

from car_media_hub import resources_api as RA

from test_regions import AUTH, mgr, root, sync_client  # noqa: F401  (fixtures)

URL = "/api/resources/files/chargers/chargers-de.sqlite"


def put(root, name, data):
    d = root / RA.RESOURCES_DIR / "chargers"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(data)
    return data


def data_of(size, seed=0):
    out, block = bytearray(), hashlib.sha256(bytes([seed])).digest()
    while len(out) < size:
        block = hashlib.sha256(block).digest()
        out += block
    return bytes(out[:size])


class CountingSha:
    """Stands in for ``hashlib`` in the module and counts the digests that are started."""

    def __init__(self):
        self.started = 0

    def sha256(self, *args):
        self.started += 1
        return hashlib.sha256(*args)


async def test_the_file_is_never_read_whole_and_never_hashed_again(mgr, root, tmp_path, monkeypatch):
    data = put(root, "chargers-de.sqlite", data_of(3_000_000))

    def refuse(self):
        raise AssertionError("the whole file was read into memory")

    monkeypatch.setattr(Path, "read_bytes", refuse)
    counter = CountingSha()
    monkeypatch.setattr(RA, "hashlib", counter)
    client = await sync_client(mgr, tmp_path)
    try:
        manifest = await (await client.get("/api/resources/manifest", headers=AUTH)).json()
        assert manifest["resources"][0]["sha256"] == hashlib.sha256(data).hexdigest()
        assert counter.started == 1  # hashed once, when the manifest first saw the file
        for _ in range(3):
            resp = await client.get(URL, headers=AUTH)
            assert resp.status == 200 and await resp.read() == data
        assert counter.started == 1  # served from the cache: size and mtime unchanged
    finally:
        await client.close()


async def test_headers_etag_and_range(mgr, root, tmp_path):
    data = put(root, "chargers-de.sqlite", data_of(2_500_000))
    sha = hashlib.sha256(data).hexdigest()
    client = await sync_client(mgr, tmp_path)
    try:
        resp = await client.get(URL, headers=AUTH)
        assert resp.status == 200 and resp.headers["ETag"] == f'"{sha}"' and resp.headers["Accept-Ranges"] == "bytes"
        assert resp.headers["Content-Length"] == str(len(data)) and resp.headers["X-Max-Streams"] == "8"
        assert resp.headers["Cache-Control"] == "no-store" and await resp.read() == data
        part = await client.get(URL, headers={**AUTH, "Range": "bytes=1000-1999", "If-Range": f'"{sha}"'})
        assert part.status == 206 and await part.read() == data[1000:2000]
        assert part.headers["Content-Range"] == f"bytes 1000-1999/{len(data)}"
        stale = await client.get(URL, headers={**AUTH, "Range": "bytes=1000-1999", "If-Range": '"nope"'})
        assert stale.status == 200 and len(await stale.read()) == len(data)
        gone = await client.get(URL, headers={**AUTH, "Range": f"bytes={len(data)}-"})
        assert gone.status == 416 and gone.headers["Content-Range"] == f"bytes */{len(data)}"
        head = await client.head(URL, headers=AUTH)
        assert head.status == 200 and head.headers["Content-Length"] == str(len(data)) and await head.read() == b""
    finally:
        await client.close()


async def test_a_changed_file_is_hashed_again_and_gets_a_new_etag(mgr, root, tmp_path):
    put(root, "chargers-de.sqlite", data_of(1000, 1))
    client = await sync_client(mgr, tmp_path)
    try:
        first = (await client.head(URL, headers=AUTH)).headers["ETag"]
        new = put(root, "chargers-de.sqlite", data_of(1500, 2))
        later = time.time() + 5
        os.utime(root / RA.RESOURCES_DIR / "chargers" / "chargers-de.sqlite", (later, later))
        resp = await client.get(URL, headers=AUTH)
        assert resp.status == 200 and await resp.read() == new
        assert resp.headers["ETag"] == f'"{hashlib.sha256(new).hexdigest()}"' != first
    finally:
        await client.close()


async def test_missing_and_foreign_names_stay_404_and_the_token_is_required(mgr, root, tmp_path):
    put(root, "chargers-de.sqlite", data_of(100))
    client = await sync_client(mgr, tmp_path)
    try:
        assert (await client.get(URL)).status == 401
        assert (await client.get("/api/resources/files/chargers/missing.sqlite", headers=AUTH)).status == 404
        assert (await client.get("/api/resources/files/chargers/a.txt", headers=AUTH)).status == 404
    finally:
        await client.close()
