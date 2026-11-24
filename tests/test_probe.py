"""``GET /api/probe``: exact length, incompressible, no disk, capped, token protected."""
from __future__ import annotations

import builtins
import os
import zlib

import pytest

from car_media_hub import probe

from test_sync_ranges import AUTH, blob, start


@pytest.fixture
async def served(tmp_path):
    client, app, lib = await start(tmp_path, {"a/one.mp3": blob(300_000), "two.mp3": blob(900_000, 2)})
    yield client, app, lib
    await client.close()


async def test_default_is_8_mib_with_the_documented_headers(served):
    client, app, lib = served
    resp = await client.get("/api/probe", headers=AUTH)
    assert resp.status == 200
    assert resp.headers["Content-Length"] == str(8 << 20) == str(probe.DEFAULT_BYTES)
    assert resp.headers["Cache-Control"] == "no-store" and resp.headers["Content-Type"] == "application/octet-stream"
    assert resp.headers["X-Max-Streams"] == "8" and int(resp.headers["X-Probe-Started"]) > 0
    assert "Content-Encoding" not in resp.headers and "ETag" not in resp.headers
    assert len(await resp.read()) == 8 << 20


@pytest.mark.parametrize("query,expected", [
    ("bytes=1", 1), ("bytes=1000", 1000), ("bytes=4194304", 4 << 20), ("bytes=4194305", (4 << 20) + 1),
    ("mb=2", 2 << 20), ("bytes=5&mb=2", 5), ("bytes=999999999999", 64 << 20), ("mb=1000", 64 << 20),
])
async def test_length_is_exact_and_capped(served, query, expected):
    client, app, lib = served
    resp = await client.get("/api/probe?" + query, headers=AUTH)
    assert resp.status == 200 and resp.headers["Content-Length"] == str(expected)
    assert len(await resp.read()) == expected


@pytest.mark.parametrize("query", ["bytes=0", "bytes=-5", "bytes=abc", "bytes=", "mb=0", "bytes=1.5", "bytes=" + "9" * 40])
async def test_bad_sizes_are_400(served, query):
    client, app, lib = served
    resp = await client.get("/api/probe?" + query, headers=AUTH)
    assert resp.status == 400 and (await resp.json()) == {"error": "bad_request"}


async def test_the_data_is_incompressible_and_does_not_repeat_within_a_megabyte(served):
    client, app, lib = served
    body = await (await client.get("/api/probe?bytes=4194304", headers=AUTH)).read()
    sample = body[:1 << 20]
    assert len(zlib.compress(sample, 9)) / len(sample) > 0.99
    assert body[:4096] != body[4096:8192]
    assert body[: 1 << 20] != body[1 << 20: 2 << 20]


async def test_requires_the_token(served):
    client, app, lib = served
    assert (await client.get("/api/probe")).status == 401
    assert (await client.get("/api/probe", headers={"Authorization": "Bearer wrong" + "x" * 20})).status == 401
    assert (await client.post("/api/probe", headers=AUTH)).status == 405
    assert (await client.get("/api/probe/disk")).status == 401


async def test_head_has_the_length_and_no_body(served):
    client, app, lib = served
    resp = await client.head("/api/probe?bytes=12345", headers=AUTH)
    assert resp.status == 200 and resp.headers["Content-Length"] == "12345" and await resp.read() == b""


async def test_a_probe_touches_no_file(served, monkeypatch):
    client, app, lib = served
    probe.random_block()  # created before the watch starts (os.urandom is no file access)
    opened = []
    real_open, real_os_open = builtins.open, os.open
    monkeypatch.setattr(builtins, "open", lambda *a, **k: opened.append(a) or real_open(*a, **k))
    monkeypatch.setattr(os, "open", lambda *a, **k: opened.append(a) or real_os_open(*a, **k))
    resp = await client.get("/api/probe?bytes=3000000", headers=AUTH)
    assert len(await resp.read()) == 3000000
    assert opened == []


async def test_disk_probe_reads_the_largest_library_file(served):
    client, app, lib = served
    doc = await (await client.get("/api/probe/disk?bytes=1000000", headers=AUTH)).json()
    assert doc["bytes"] == 900_000 and doc["seconds"] >= 0 and isinstance(doc["cached"], bool)
    doc = await (await client.get("/api/probe/disk?bytes=1000", headers=AUTH)).json()
    assert doc["bytes"] == 1000


async def test_disk_probe_of_an_empty_library(tmp_path):
    client, app, lib = await start(tmp_path, {})
    try:
        resp = await client.get("/api/probe/disk", headers=AUTH)
        assert resp.status == 200 and (await resp.json())["bytes"] == 0
    finally:
        await client.close()
