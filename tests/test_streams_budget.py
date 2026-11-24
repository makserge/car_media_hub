"""The stream budget: 8 streams per client, 16 in total, stalled streams are replaced, 503 + Retry-After only above that."""
from __future__ import annotations

import asyncio
import time

from car_media_hub import streams as S
from car_media_hub.streams import STREAMS, StreamBudget

from test_sync_ranges import AUTH, blob, start

URL = "/api/v1/files/big.mp3"


def test_limits_are_eight_per_client_and_sixteen_in_total():
    assert (S.MAX_STREAMS_PER_CLIENT, S.MAX_STREAMS_TOTAL) == (8, 16)
    budget = StreamBudget()
    mine = [budget.begin("10.0.0.1", ("f", i)) for i in range(8)]
    assert all(mine) and budget.begin("10.0.0.1", ("f", 99)) is None  # the 9th of one client
    other = [budget.begin("10.0.0.2", ("f", i)) for i in range(8)]
    assert all(other) and budget.active == 16
    # the 17th in total, from a client that has none: the clients with 8 streams give way (fair share, see test_fastsync_review)
    third = budget.begin("10.0.0.3", ("f", 0))
    assert third is not None and budget.active == 16 and mine[0].dropped
    budget.end(mine[1])
    assert budget.begin("10.0.0.3", ("f", 1)) is not None and budget.active == 16


def test_ending_a_stream_twice_is_harmless():
    budget = StreamBudget()
    stream = budget.begin("a", ("f", 1))
    budget.end(stream)
    budget.end(stream)
    assert budget.active == 0


def test_streams_that_move_are_never_dropped():
    budget = StreamBudget()
    for i in range(8):
        budget.begin("a", ("f", i))
    assert budget.begin("a", ("f", 9)) is None and budget.active == 8


def test_a_stalled_stream_of_the_same_client_and_file_is_taken_over():
    budget = StreamBudget()
    aborted = []

    class Transport:
        def __init__(self, n):
            self.n = n

        def abort(self):
            aborted.append(self.n)

    streams = [budget.begin("a", ("f", i), Transport(i)) for i in range(8)]
    for s in streams:
        s.progress_at -= S.STALL_SECONDS + 1 + s.key[1]  # stream 7 stalled longest
    new = budget.begin("a", ("f", 3), Transport("new"))
    assert new is not None and aborted == [3] and streams[3].dropped and budget.active == 8
    again = budget.begin("a", ("f", 100), Transport("new2"))
    assert again is not None and aborted == [3, 7]  # then the one stalled longest


def test_a_stalled_stream_of_another_client_makes_room_in_the_total():
    budget = StreamBudget(per_client=8, total=2)
    old = budget.begin("a", ("f", 1))
    budget.begin("b", ("f", 2))
    assert budget.begin("c", ("f", 3)) is None
    old.progress_at -= S.STALL_SECONDS + 1
    assert budget.begin("c", ("f", 3)) is not None and old.dropped


async def wait_until(check, seconds=8.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return True
        await asyncio.sleep(0.02)
    return False


async def open_slow(port, count):
    """``count`` connections that ask for the big file and read only the headers: their downloads stay open."""
    pairs = []
    for _ in range(count):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write((f"GET {URL} HTTP/1.1\r\nHost: x\r\nAuthorization: {AUTH['Authorization']}\r\n\r\n").encode())
        await writer.drain()
        await reader.readuntil(b"\r\n\r\n")
        pairs.append((reader, writer))
    return pairs


async def test_nine_real_downloads_of_one_client_the_ninth_gets_503(tmp_path):
    client, app, lib = await start(tmp_path, {"big.mp3": blob(60_000_000)})
    budget = app[STREAMS]
    try:
        pairs = await open_slow(client.server.port, 8)
        assert await wait_until(lambda: budget.active == 8)
        busy = await client.get(URL, headers=AUTH)
        assert busy.status == 503 and busy.headers["Retry-After"] == "2" and busy.headers["X-Max-Streams"] == "8"
        assert (await busy.json()) == {"error": "busy"}
        assert (await client.head(URL, headers=AUTH)).status == 200  # HEAD takes no slot
        assert (await client.get("/api/v1/files", headers=AUTH)).status == 200  # nor does the listing
        for _, writer in pairs[:2]:
            writer.transport.abort()
        assert await wait_until(lambda: budget.active == 6)
        ok = await client.get(URL, headers={**AUTH, "Range": "bytes=0-9"})
        assert ok.status == 206 and len(await ok.read()) == 10
        for _, writer in pairs[2:]:
            writer.transport.abort()
        assert await wait_until(lambda: budget.active == 0)
    finally:
        await client.close()


async def test_probe_and_music_share_the_budget(tmp_path):
    client, app, lib = await start(tmp_path, {"big.mp3": blob(1000)})
    budget = app[STREAMS]
    try:
        held = [budget.begin("127.0.0.1", ("x", i)) for i in range(8)]
        assert all(held)
        assert (await client.get("/api/probe?bytes=10", headers=AUTH)).status == 503
        assert (await client.get(URL, headers=AUTH)).status == 503
        budget.end(held[0])
        assert (await client.get("/api/probe?bytes=10", headers=AUTH)).status == 200
    finally:
        await client.close()
