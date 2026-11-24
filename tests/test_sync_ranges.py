"""Music files on the sync API: Range with and without If-Range, the strong ETag, 206/416, sendfile, parallel ranges."""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from car_media_hub import fileserve
from car_media_hub.streams import STREAMS
from car_media_hub.sync import _INDEX, LibraryIndex, create_sync_app

TOKEN = "t" * 24
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class State:
    def __init__(self):
        self.values = {}

    def get_state(self, key):
        return self.values.get(key)

    def set_state(self, key, value):
        self.values[key] = value


def make_library(tmp_path, files):
    lib = tmp_path / "lib"
    lib.mkdir(exist_ok=True)
    for name, data in files.items():
        path = lib / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        old = time.time() - 3600
        os.utime(path, (old, old))
    return lib


async def start(tmp_path, files, **index_kw):
    lib = make_library(tmp_path, files)
    index_kw.setdefault("settle", 0.0)
    index = LibraryIndex(lib, State(), **index_kw)
    app = create_sync_app(index, TOKEN)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client, app, lib


def blob(size, seed=1):
    out = bytearray()
    block = hashlib.sha256(str(seed).encode()).digest()
    while len(out) < size:
        block = hashlib.sha256(block).digest()
        out += block
    return bytes(out[:size])


URL = "/api/v1/files/Album/01.mp3"


@pytest.fixture(params=[True, False], ids=["sendfile", "copy"])
async def served(tmp_path, request, monkeypatch):
    monkeypatch.setattr(fileserve, "USE_SENDFILE", request.param)  # both ways of sending give the same answers
    data = blob(5_000_000)  # more than two sendfile segments
    client, app, lib = await start(tmp_path, {"Album/01.mp3": data})
    yield client, app, lib, data
    await client.close()


async def test_full_get_headers_and_body(served):
    client, app, lib, data = served
    resp = await client.get(URL, headers=AUTH)
    assert resp.status == 200
    st = os.stat(lib / "Album" / "01.mp3")
    assert resp.headers["ETag"] == f'"{st.st_size:x}-{st.st_mtime_ns:x}"'
    assert re.fullmatch(r'"[0-9a-f]+-[0-9a-f]+"', resp.headers["ETag"])
    assert resp.headers["Accept-Ranges"] == "bytes" and resp.headers["X-Max-Streams"] == "8"
    assert resp.headers["Content-Length"] == str(len(data)) and resp.headers["Cache-Control"] == "no-store"
    assert resp.headers["Content-Type"] == "application/octet-stream" and "Content-Encoding" not in resp.headers
    assert "Last-Modified" in resp.headers
    assert await resp.read() == data


async def test_range_without_if_range_is_206(served):
    client, app, lib, data = served
    resp = await client.get(URL, headers={**AUTH, "Range": "bytes=100-199"})
    assert resp.status == 206 and resp.headers["Content-Range"] == f"bytes 100-199/{len(data)}"
    assert resp.headers["Content-Length"] == "100" and await resp.read() == data[100:200]
    open_end = await client.get(URL, headers={**AUTH, "Range": "bytes=4999000-"})
    assert open_end.status == 206 and await open_end.read() == data[4999000:]


async def test_range_with_matching_if_range_is_206(served):
    client, app, lib, data = served
    etag = (await client.head(URL, headers=AUTH)).headers["ETag"]
    resp = await client.get(URL, headers={**AUTH, "Range": "bytes=10-19", "If-Range": etag})
    assert resp.status == 206 and await resp.read() == data[10:20]
    assert resp.headers["ETag"] == etag
    modified = (await client.head(URL, headers=AUTH)).headers["Last-Modified"]
    by_date = await client.get(URL, headers={**AUTH, "Range": "bytes=10-19", "If-Range": modified})
    assert by_date.status == 206 and await by_date.read() == data[10:20]


async def test_range_with_stale_or_weak_if_range_gives_the_whole_file(served):
    client, app, lib, data = served
    etag = (await client.head(URL, headers=AUTH)).headers["ETag"]
    for validator in ('"deadbeef"', "W/" + etag, '"1-1"'):
        resp = await client.get(URL, headers={**AUTH, "Range": "bytes=10-19", "If-Range": validator})
        assert resp.status == 200 and "Content-Range" not in resp.headers, validator
        assert await resp.read() == data
    no_range = await client.get(URL, headers={**AUTH, "If-Range": etag})
    assert no_range.status == 200 and await no_range.read() == data


async def test_etag_changes_when_the_file_changes(served):
    client, app, lib, data = served
    first = (await client.head(URL, headers=AUTH)).headers["ETag"]
    path = lib / "Album" / "01.mp3"
    path.write_bytes(data + b"x")
    os.utime(path, (time.time() - 1800, time.time() - 1800))
    app[_INDEX].invalidate()
    second = await client.get(URL, headers={**AUTH, "Range": "bytes=0-9", "If-Range": first})
    assert second.status == 200 and second.headers["ETag"] != first  # the old validator no longer matches: whole file
    assert len(await second.read()) == len(data) + 1


@pytest.mark.parametrize("header", ["bytes=5000000-", "bytes=9999999-", "bytes=5000000-6000000"])
async def test_unsatisfiable_range_is_416(served, header):
    client, app, lib, data = served
    resp = await client.get(URL, headers={**AUTH, "Range": header})
    assert resp.status == 416 and resp.headers["Content-Range"] == f"bytes */{len(data)}"
    assert resp.headers["ETag"] and resp.headers["Accept-Ranges"] == "bytes" and resp.headers["X-Max-Streams"] == "8"
    assert (await resp.json()) == {"error": "range_not_satisfiable"}


@pytest.mark.parametrize("header", ["bytes=-5", "bytes=1-2,5-6", "items=1-2", "bytes=9-1", "garbage"])
async def test_ranges_that_are_ignored_give_200(served, header):
    client, app, lib, data = served
    resp = await client.get(URL, headers={**AUTH, "Range": header})
    assert resp.status == 200 and len(await resp.read()) == len(data)


async def test_head_has_the_headers_and_no_body_and_takes_no_slot(served):
    client, app, lib, data = served
    resp = await client.head(URL, headers=AUTH)
    assert resp.status == 200 and resp.headers["Content-Length"] == str(len(data))
    assert resp.headers["Accept-Ranges"] == "bytes" and await resp.read() == b""
    assert app[STREAMS].active == 0


async def test_parallel_ranges_reassemble_to_the_same_sha256(served):
    client, app, lib, data = served
    size, segment = len(data), 700_000

    async def fetch(start):
        end = min(start + segment, size) - 1
        resp = await client.get(URL, headers={**AUTH, "Range": f"bytes={start}-{end}"})
        assert resp.status == 206 and resp.headers["Content-Range"] == f"bytes {start}-{end}/{size}"
        return start, await resp.read()

    parts = await asyncio.gather(*(fetch(s) for s in range(0, size, segment)))
    joined = b"".join(chunk for _, chunk in sorted(parts))
    assert hashlib.sha256(joined).hexdigest() == hashlib.sha256(data).hexdigest()
    assert app[STREAMS].active == 0


async def test_range_across_sendfile_segment_boundaries(served):
    client, app, lib, data = served
    start, end = fileserve.SEND_SEGMENT - 10, 2 * fileserve.SEND_SEGMENT + 10
    resp = await client.get(URL, headers={**AUTH, "Range": f"bytes={start}-{end}"})
    assert resp.status == 206 and await resp.read() == data[start:end + 1]


async def test_the_body_is_sent_with_sendfile_in_segments(served, monkeypatch):
    client, app, lib, data = served
    calls = []
    original = fileserve.SendfileResponse._send_part

    async def counting(self, request, offset, count):
        calls.append((offset, count))
        return await original(self, request, offset, count)

    monkeypatch.setattr(fileserve.SendfileResponse, "_send_part", counting)
    resp = await client.get(URL, headers=AUTH)
    assert await resp.read() == data
    assert sum(c for _, c in calls) == len(data) and len(calls) >= -(-len(data) // fileserve.SEND_SEGMENT)  # adaptive sizes
    assert all(0 < c <= fileserve.SEND_SEGMENT for _, c in calls) and calls[0][0] == 0
    assert all(calls[i][0] + calls[i][1] == calls[i + 1][0] for i in range(len(calls) - 1))  # contiguous, in order


async def test_keep_alive_serves_several_requests_on_one_connection(served):
    client, app, lib, data = served
    for _ in range(3):
        resp = await client.get(URL, headers={**AUTH, "Range": "bytes=0-99"})
        assert resp.status == 206 and "close" not in resp.headers.get("Connection", "").lower()
        await resp.read()
    assert len(client.session.connector._conns) <= 1  # the connection went back to the pool each time


async def test_no_compression_even_when_the_client_offers_it(served):
    client, app, lib, data = served
    resp = await client.get(URL, headers={**AUTH, "Accept-Encoding": "gzip, deflate, br"}, auto_decompress=False)
    assert "Content-Encoding" not in resp.headers and len(await resp.read()) == len(data)


async def test_auth_and_unknown_files_are_unchanged(served):
    client, app, lib, data = served
    assert (await client.get(URL)).status == 401
    assert (await client.get("/api/v1/files/Album/none.mp3", headers=AUTH)).status == 404
    assert (await client.get("/api/v1/files/..%2Fsecret", headers=AUTH)).status == 404
    listing = await (await client.get("/api/v1/files", headers=AUTH)).json()
    assert listing == [{"name": "Album/01.mp3", "size": len(data), "mtime": listing[0]["mtime"], "sha256": None}]


async def test_a_shorter_file_aborts_the_connection_instead_of_ending_cleanly(tmp_path):
    data = blob(3_000_000)
    client, app, lib = await start(tmp_path, {"a.mp3": data})
    try:
        original = fileserve.SendfileResponse._send_part

        async def truncating(self, request, offset, count):
            return max(0, await original(self, request, offset, count) - 1)  # claims one byte less than asked

        fileserve.SendfileResponse._send_part = truncating
        try:
            with pytest.raises(Exception):
                resp = await client.get("/api/v1/files/a.mp3", headers=AUTH)
                await resp.read()
        finally:
            fileserve.SendfileResponse._send_part = original
        assert app[STREAMS].active == 0
    finally:
        await client.close()


async def test_file_and_probe_connections_are_marked_dscp_cs1(served, monkeypatch):
    import socket
    import sys

    client, app, lib, data = served
    seen = []
    original = fileserve.mark_background

    def watching(request):
        original(request)
        sock = request.transport.get_extra_info("socket")
        seen.append(sock.getsockopt(socket.IPPROTO_IP, socket.IP_TOS))

    monkeypatch.setattr(fileserve, "mark_background", watching)
    monkeypatch.setattr("car_media_hub.probe.mark_background", watching)
    await (await client.get(URL, headers={**AUTH, "Range": "bytes=0-9"})).read()
    await (await client.get("/api/probe?bytes=10", headers=AUTH)).read()
    assert len(seen) == 2
    if sys.platform != "win32":  # Windows ignores IP_TOS set by a plain setsockopt
        assert seen == [fileserve.DSCP_CS1_TOS] * 2
