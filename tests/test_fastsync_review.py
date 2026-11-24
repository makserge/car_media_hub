"""Fixes of the independent review of the fast-sync patch (H1, M1, M2, L1 to L6): taking over a stalled stream, the
slot given back before the last byte, the bounded stale listing and its folder check, progress inside the stream
stall rule, the charger hash stamp, the disk probe, fair sharing of the total limit, If-Range with a date, sync_dscp."""
from __future__ import annotations

import asyncio
import os
import socket
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from car_media_hub import fileserve, probe
from car_media_hub import resources_api as RA
from car_media_hub import streams as S
from car_media_hub import sync as SY
from car_media_hub.streams import STREAMS, StreamBudget, dropped_by_budget

from test_sync_ranges import AUTH, TOKEN, blob, make_library, start

BIG = "/api/v1/files/big.mp3"


# ------------------------------------------------------------------------------------------------ H1: taking over a stalled stream


async def open_stalled(port, path):
    """A client that asks for ``path``, reads the headers and then never reads again (a tiny receive buffer)."""
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    sock.setblocking(False)
    await loop.sock_connect(sock, ("127.0.0.1", port))
    reader, writer = await asyncio.open_connection(sock=sock)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: x\r\nAuthorization: {AUTH['Authorization']}\r\n\r\n".encode())
    await writer.drain()
    await reader.readuntil(b"\r\n\r\n")
    return reader, writer


async def until_closed(reader, seconds=10.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            if not await asyncio.wait_for(reader.read(1 << 20), 1.0):
                return True
        except (ConnectionError, OSError):
            return True
        except asyncio.TimeoutError:
            continue
    return False


@pytest.mark.parametrize("what", ["sendfile", "copy", "probe"])
async def test_taking_over_a_stalled_stream_unwinds_it_and_breaks_nothing(tmp_path, monkeypatch, what):
    """The victim of a takeover may be inside ``loop.sendfile``: its transport must not be aborted from outside.

    Aborting it left a selector registration for the closed socket behind (asyncio ERROR in the log) and the next
    connection that got the same file descriptor was never answered. A real transport, real sockets, two later
    connections that must be served, and no exception may reach the loop's exception handler."""
    if what == "sendfile" and sys.platform == "win32":
        pytest.skip("loop.sendfile is not used on Windows (fileserve.USE_SENDFILE is off)")
    monkeypatch.setattr(fileserve, "USE_SENDFILE", what == "sendfile")
    client, app, lib = await start(tmp_path, {"big.mp3": blob(40_000_000)})
    loop = asyncio.get_running_loop()
    problems: list[dict] = []
    loop.set_exception_handler(lambda _loop, context: problems.append(context))
    sendfile_calls: list[int] = []
    if what == "sendfile":
        real = loop.sendfile

        async def spy(*args, **kw):
            sendfile_calls.append(1)
            return await real(*args, **kw)

        monkeypatch.setattr(loop, "sendfile", spy)
    budget = app[STREAMS]
    budget.per_client = 1
    budget.stall_seconds = 0.4
    port = client.server.port
    path = "/api/probe?bytes=67108864" if what == "probe" else BIG
    try:
        reader, writer = await open_stalled(port, path)
        deadline = time.monotonic() + 5
        while budget.active != 1 and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert budget.active == 1
        await asyncio.sleep(0.8)  # the server is stuck in a write to a client that does not read
        if what == "sendfile":
            assert sendfile_calls, "the stalled stream must be inside loop.sendfile"
        first = await client.get(BIG, headers={**AUTH, "Range": "bytes=0-99", "Connection": "close"})
        assert first.status == 206 and await first.read() == blob(40_000_000)[:100]  # the slot of the stalled stream was taken over
        assert await until_closed(reader), "the victim connection is closed"
        writer.transport.abort()
        for i in range(3):  # new connections reuse the descriptor of the closed one: each must be served
            resp = await asyncio.wait_for(
                client.get(BIG, headers={**AUTH, "Range": f"bytes={i}-{i + 9}", "Connection": "close"}), 8)
            assert resp.status == 206 and len(await resp.read()) == 10
        await asyncio.sleep(0.3)
        assert budget.active == 0  # every slot was given back exactly once
        assert problems == [], problems
    finally:
        loop.set_exception_handler(None)
        await client.close()


async def test_a_dropped_stream_is_cancelled_not_aborted_from_outside():
    class Transport:
        aborted = False

        def abort(self):
            Transport.aborted = True

    async def handler(budget, stream_box, started):
        stream_box.append(budget.begin("a", ("f", 1), Transport()))
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            assert dropped_by_budget(stream_box[0])  # unwound; the handler closes its own socket now
            assert asyncio.current_task().cancelling() == 0
            return "unwound"

    budget = StreamBudget(per_client=1, total=4, stall_seconds=0.0)
    box: list = []
    started = asyncio.Event()
    task = asyncio.ensure_future(handler(budget, box, started))
    await started.wait()
    new = budget.begin("a", ("f", 2), Transport())
    assert new is not None and budget.active == 1 and box[0].dropped
    assert Transport.aborted is False  # nobody closed the socket under the running handler
    assert await task == "unwound"


async def test_a_second_cancellation_is_not_swallowed():
    stream = S.Stream(1, "a", ("f",))
    stream.dropped = True
    seen = []

    async def handler():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            seen.append(dropped_by_budget(stream))
            raise

    task = asyncio.ensure_future(handler())
    await asyncio.sleep(0)
    task.cancel()
    task.cancel()  # a shutdown cancels as well
    with pytest.raises(asyncio.CancelledError):
        await task
    assert seen == [False]


def test_a_stream_without_a_task_is_aborted_as_before():
    aborted = []
    budget = StreamBudget(per_client=1, total=4, stall_seconds=0.0)
    budget.begin("a", ("f", 1), types.SimpleNamespace(abort=lambda: aborted.append(1)))
    assert budget.begin("a", ("f", 2)) is not None and aborted == [1]


def test_a_closing_connection_does_not_hold_its_slot_for_the_next_request():
    budget = StreamBudget(per_client=2, total=16)
    closing = types.SimpleNamespace(abort=lambda: None, is_closing=lambda: True)
    budget.begin("a", ("f", 1), closing)
    budget.begin("a", ("f", 2), types.SimpleNamespace(abort=lambda: None, is_closing=lambda: False))
    assert budget.begin("a", ("f", 3)) is not None and budget.active == 2


# ------------------------------------------------------------------------------------------------ M1: slot free before the last byte arrives


@pytest.mark.parametrize("use_sendfile", [True, False], ids=["sendfile", "copy"])
async def test_the_slot_is_free_when_the_client_has_the_whole_body(tmp_path, monkeypatch, use_sendfile):
    """A client with exactly 8 workers asks for its next file right after the last byte: it must never see a busy 503."""
    if use_sendfile and sys.platform == "win32":
        pytest.skip("loop.sendfile is not used on Windows")
    monkeypatch.setattr(fileserve, "USE_SENDFILE", use_sendfile)
    client, app, lib = await start(tmp_path, {"big.mp3": blob(3_000_000)})
    budget = app[STREAMS]
    try:
        for _ in range(5):
            resp = await client.get(BIG, headers=AUTH)
            assert len(await resp.read()) == 3_000_000
            assert budget.active == 0
        await asyncio.gather(*[client.get(BIG, headers={**AUTH, "Range": "bytes=0-9"}) for _ in range(budget.per_client)])
    finally:
        await client.close()


def test_a_stream_in_its_last_segment_has_given_its_slot_back_but_tails_are_bounded():
    budget = StreamBudget(per_client=2, total=3)
    a = budget.begin("a", ("f", 1))
    budget.begin("a", ("f", 2))
    assert budget.begin("a", ("f", 3)) is None
    a.finishing_now()  # the last 256 KiB go out; the client can ask for its next file
    assert budget.active == 1 and budget.finishing == 1 and budget.active_for("a") == 1
    assert budget.begin("a", ("f", 3)) is not None
    budget.end(a)
    assert budget.finishing == 0
    # tails of a stalled client do not pile up without bound: above `total` of them a stalled one is dropped, else refused
    small = StreamBudget(per_client=8, total=2)
    stuck = [small.begin("x", ("t", i)) for i in range(2)]
    for s in stuck:
        s.finishing_now()
    assert small.begin("y", ("f", 0)) is None  # two tails, none stalled
    stuck[0].progress_at -= S.STALL_SECONDS + 1
    assert small.begin("y", ("f", 0)) is not None and stuck[0].dropped


# ------------------------------------------------------------------------------------------------ M2: bounded stale listing, folder check


def test_the_stale_window_is_short_and_named():
    assert SY.LIST_TTL == 30.0 and SY.MAX_STALE == 120.0 and SY.DIR_CHECK_INTERVAL > 0


async def test_a_listing_is_served_stale_only_up_to_the_named_limit(tmp_path):
    now = [1000.0]
    client, app, lib = await start(tmp_path, {"Album/a.mp3": blob(100)}, clock=lambda: now[0], ttl=SY.LIST_TTL)
    try:
        assert [e["name"] for e in await (await client.get("/api/v1/files", headers=AUTH)).json()] == ["Album/a.mp3"]
        make_library(tmp_path, {"Album/b.mp3": blob(100)})
        # the cheap folder check is off here (nothing changes in the top-level folders' times: Album/ is the folder that changed)
        index = app[SY._INDEX]
        index._stamp = None
        now[0] += SY.MAX_STALE - 1
        listed = await (await client.get("/api/v1/files", headers=AUTH)).json()
        assert [e["name"] for e in listed] == ["Album/a.mp3"]  # stale, a background rebuild started
        for _ in range(100):
            if index._built_at == now[0]:
                break
            await asyncio.sleep(0.05)
        now[0] += SY.MAX_STALE + 1
        make_library(tmp_path, {"Album/c.mp3": blob(100)})
        listed = await (await client.get("/api/v1/files", headers=AUTH)).json()
        assert sorted(e["name"] for e in listed) == ["Album/a.mp3", "Album/b.mp3", "Album/c.mp3"]  # too old: waited and rebuilt
    finally:
        await client.close()


async def test_a_change_made_directly_in_the_library_shows_up_within_the_ttl(tmp_path, monkeypatch):
    now = [1000.0]
    client, app, lib = await start(tmp_path, {"a.mp3": blob(100), "Artist/x.mp3": blob(100)}, clock=lambda: now[0], ttl=SY.LIST_TTL)
    scans = []
    real = SY._scan_library
    monkeypatch.setattr(SY, "_scan_library", lambda *a, **k: scans.append(1) or real(*a, **k))
    try:
        names = lambda doc: sorted(e["name"] for e in doc)  # noqa: E731
        assert names(await (await client.get("/api/v1/files", headers=AUTH)).json()) == ["Artist/x.mp3", "a.mp3"]
        assert len(scans) == 1
        now[0] += SY.DIR_CHECK_INTERVAL + 1  # nothing changed: the cheap check finds nothing and no rebuild runs
        await (await client.get("/api/v1/files", headers=AUTH)).json()
        assert len(scans) == 1
        # a file dropped into the library folder, and one into a first-level folder, behind the add-on's back
        time.sleep(0.02)
        make_library(tmp_path, {"new.mp3": blob(100), "Artist/y.mp3": blob(100)})
        os.utime(lib, None)
        os.utime(lib / "Artist", None)
        now[0] += SY.DIR_CHECK_INTERVAL + 1
        assert now[0] - 1000.0 < SY.LIST_TTL  # still inside the TTL
        doc = await (await client.get("/api/v1/files", headers=AUTH)).json()
        assert names(doc) == ["Artist/x.mp3", "Artist/y.mp3", "a.mp3", "new.mp3"] and len(scans) == 2
        time.sleep(0.05)  # a change within one timestamp tick of the stamp is invisible to it (the TTL catches that)
        (lib / "a.mp3").unlink()
        now[0] += SY.DIR_CHECK_INTERVAL + 1
        doc = await (await client.get("/api/v1/files", headers=AUTH)).json()
        assert "a.mp3" not in names(doc)
    finally:
        await client.close()


def test_the_folder_stamp_is_cheap_and_bounded(tmp_path, monkeypatch):
    lib = make_library(tmp_path, {"a.mp3": b"x", "A/1.mp3": b"x", "B/1.mp3": b"x"})
    (lib / ".hidden").mkdir()
    stamp = SY._dir_stamp(lib)
    assert [name for name, _ in stamp] == ["", "A", "B"]
    monkeypatch.setattr(SY, "DIR_STAMP_MAX", 1)
    assert [name for name, _ in SY._dir_stamp(lib)] == ["", "A"]
    assert SY._dir_stamp(tmp_path / "missing") is None


# ------------------------------------------------------------------------------------------------ L1: progress marks inside the stall rule


def test_segments_are_adaptive_and_bounded():
    assert fileserve.SEND_SEGMENT_MIN <= 64 << 10 and fileserve.SEND_SEGMENT <= 1 << 20
    assert fileserve.SEGMENT_FAST_SECONDS < fileserve.SEGMENT_SLOW_SECONDS < S.STALL_SECONDS / 10
    assert fileserve.SEND_SEGMENT_MIN / S.STALL_SECONDS < 2000  # only a stream below ~1 KB/s can fail to finish a segment in time


@pytest.mark.parametrize("rate", [50_000_000, 400_000], ids=["fast", "slow"])
async def test_segment_size_follows_the_speed_of_the_link(tmp_path, monkeypatch, rate):
    """A fast link ramps up to the largest segment; a slow link settles on short segments, so progress keeps being marked."""
    monkeypatch.setattr(fileserve, "USE_SENDFILE", True)
    monkeypatch.setattr(fileserve, "SEND_SEGMENT", 256 << 10)
    monkeypatch.setattr(fileserve, "SEND_SEGMENT_MIN", 16 << 10)
    monkeypatch.setattr(fileserve, "SEGMENT_FAST_SECONDS", 0.05)
    monkeypatch.setattr(fileserve, "SEGMENT_SLOW_SECONDS", 0.2)
    parts, marks = [], []

    async def paced(self, request, offset, count):
        parts.append(count)
        marks.append(time.monotonic())
        sent = await self._copy_part(offset, count)  # the bytes really go out, then the "link" takes its time
        await asyncio.sleep(count / rate)
        return sent

    monkeypatch.setattr(fileserve.SendfileResponse, "_send_part", paced)
    client, app, lib = await start(tmp_path, {"big.mp3": blob(1_500_000)})
    try:
        resp = await client.get(BIG, headers=AUTH)
        assert len(await resp.read()) == 1_500_000
        assert sum(parts) == 1_500_000
        if rate > 1_000_000:
            assert max(parts) == 256 << 10 and parts[0] == 16 << 10 and parts[:3] == [16 << 10, 32 << 10, 64 << 10]
        else:  # 400 KB/s: the segments settle near 0.1 to 0.2 s, never the largest one for long
            assert max(parts[6:-1]) <= 128 << 10, parts
        gaps = [b - a for a, b in zip(marks, marks[1:])]
        assert max(gaps) < 1.5  # never one long silent stretch (the stall rule is 30 s)
    finally:
        await client.close()


@pytest.mark.parametrize("use_sendfile", [True, False], ids=["sendfile", "copy"])
async def test_progress_is_marked_per_segment(tmp_path, monkeypatch, use_sendfile):
    if use_sendfile and sys.platform == "win32":
        pytest.skip("loop.sendfile is not used on Windows")
    monkeypatch.setattr(fileserve, "USE_SENDFILE", use_sendfile)
    marks = []
    real = S.Stream.moved
    monkeypatch.setattr(S.Stream, "moved", lambda self: marks.append(1) or real(self))
    client, app, lib = await start(tmp_path, {"big.mp3": blob(2_000_000)})
    try:
        resp = await client.get(BIG, headers=AUTH)
        assert len(await resp.read()) == 2_000_000
        assert len(marks) >= 2_000_000 // fileserve.SEND_SEGMENT  # the old code marked once per 2 MiB, i.e. never here
        assert len(marks) >= 2
    finally:
        await client.close()


def test_a_slow_stream_that_moved_recently_is_never_taken_over():
    budget = StreamBudget(per_client=1, total=4)
    stream = budget.begin("a", ("f", 1))
    stream.progress_at -= S.STALL_SECONDS - 5  # moved 25 s ago: a slow but moving stream
    assert budget.begin("a", ("f", 2)) is None and not stream.dropped
    stream.progress_at -= 6
    assert budget.begin("a", ("f", 2)) is not None and stream.dropped


# ------------------------------------------------------------------------------------------------ L2: charger hash stamp


def _charger_root(tmp_path):
    folder = tmp_path / "regions" / RA.RESOURCES_DIR / "chargers"
    folder.mkdir(parents=True)
    return tmp_path / "regions", folder


def _prepare(root, cache, name="c.sqlite"):
    prepared = RA._prepare_file(root, cache, "chargers", name)
    prepared[0].close()
    return prepared[2]


def test_a_replaced_file_with_the_same_size_and_mtime_is_hashed_again(tmp_path):
    root, folder = _charger_root(tmp_path)
    path = folder / "c.sqlite"
    path.write_bytes(b"a" * 4096)
    fixed = (1_700_000_000, 1_700_000_000)
    os.utime(path, fixed)
    cache: dict = {}
    first = _prepare(root, cache)
    other = folder / "new.tmp"
    other.write_bytes(b"b" * 4096)  # same size, other bytes
    os.utime(other, fixed)  # same mtime (cp -p, rsync -a, touch -r)
    os.replace(other, path)
    assert os.stat(path).st_mtime_ns == 1_700_000_000_000_000_000
    assert _prepare(root, cache) != first


@pytest.mark.skipif(sys.platform == "win32", reason="on Windows the ctime of a path and of a handle differ: not used there")
def test_an_in_place_rewrite_with_the_mtime_set_back_is_hashed_again(tmp_path):
    root, folder = _charger_root(tmp_path)
    path = folder / "c.sqlite"
    path.write_bytes(b"a" * 4096)
    fixed = (1_700_000_000, 1_700_000_000)
    os.utime(path, fixed)
    cache: dict = {}
    first = _prepare(root, cache)
    time.sleep(0.02)
    with open(path, "r+b") as f:
        f.write(b"b" * 4096)
    os.utime(path, fixed)  # the size and the mtime are back; only the ctime tells
    assert _prepare(root, cache) != first


# ------------------------------------------------------------------------------------------------ L3: disk probe


async def test_head_of_the_disk_probe_reads_nothing_and_takes_no_slot(tmp_path, monkeypatch):
    client, app, lib = await start(tmp_path, {"a.mp3": blob(2_000_000)})
    calls = []
    monkeypatch.setattr(probe, "_read_disk", lambda *a: calls.append(a) or (0, 0.0))
    budget = app[STREAMS]
    monkeypatch.setattr(budget, "begin", lambda *a, **k: calls.append("begin"))
    try:
        resp = await client.head("/api/probe/disk?bytes=1000000", headers=AUTH)
        assert resp.status == 200 and await resp.read() == b"" and calls == []
    finally:
        await client.close()


async def test_one_disk_probe_runs_at_a_time_in_its_own_thread(tmp_path, monkeypatch):
    client, app, lib = await start(tmp_path, {"a.mp3": blob(2_000_000)})
    release, entered, threads = threading.Event(), threading.Event(), []

    def slow_read(path, limit):
        threads.append(threading.current_thread().name)
        entered.set()
        release.wait(10)
        return 123, 0.5

    monkeypatch.setattr(probe, "_read_disk", slow_read)
    try:
        first = asyncio.ensure_future(client.get("/api/probe/disk", headers=AUTH))
        for _ in range(200):
            if entered.is_set():
                break
            await asyncio.sleep(0.02)
        assert entered.is_set()
        busy = await client.get("/api/probe/disk", headers=AUTH)
        assert busy.status == 503 and busy.headers["Retry-After"] == "2" and (await busy.json()) == {"error": "busy"}
        assert (await client.get("/api/probe?bytes=10", headers=AUTH)).status == 200  # other requests are not held up
        assert (await client.get(BIG.replace("big", "a"), headers={**AUTH, "Range": "bytes=0-9"})).status == 206
        release.set()
        resp = await first
        assert resp.status == 200 and (await resp.json())["bytes"] == 123
        assert threads and all(name.startswith("disk-probe") for name in threads)
        again = await client.get("/api/probe/disk", headers=AUTH)  # free again
        assert again.status == 200
    finally:
        release.set()
        await client.close()


@pytest.mark.skipif(sys.platform == "win32", reason="O_NOFOLLOW is a POSIX flag")
def test_the_disk_probe_does_not_follow_a_link(tmp_path):
    target = tmp_path / "target.bin"
    target.write_bytes(b"x" * 5000)
    link = tmp_path / "link.mp3"
    link.symlink_to(target)
    assert probe._read_disk(target, 5000)[0] == 5000
    with pytest.raises(OSError):
        probe._read_disk(link, 5000)


# ------------------------------------------------------------------------------------------------ L4: fair sharing of the total limit


def _fill(budget, client, count):
    return [budget.begin(client, ("f", client, i)) for i in range(count)]


def test_two_busy_clients_cannot_lock_out_a_third():
    budget = StreamBudget()
    a, b = _fill(budget, "a", 8), _fill(budget, "b", 8)
    assert budget.active == 16
    got = []
    while True:
        stream = budget.begin("c", ("f", "c", len(got)))
        if stream is None:
            break
        got.append(stream)
    counts = {c: budget.active_for(c) for c in "abc"}
    assert counts["c"] >= 4 and min(counts.values()) >= 5 and sum(counts.values()) == 16, counts
    assert all(s.dropped for s in (a + b) if s.id not in budget._streams)
    # the oldest streams were given up first
    assert a[0].dropped and b[0].dropped and not a[-1].dropped and not b[-1].dropped


def test_fair_sharing_does_not_touch_a_client_at_its_own_limit_or_a_fair_distribution():
    budget = StreamBudget(per_client=8, total=3)
    _fill(budget, "a", 1), _fill(budget, "b", 1), _fill(budget, "c", 1)
    assert budget.begin("d", ("f", "d", 0)) is None and budget.active == 3  # 1 stream each: nothing is taken away
    budget = StreamBudget(per_client=2, total=4)
    _fill(budget, "a", 2), _fill(budget, "b", 2)
    assert budget.begin("a", ("f", "a", 9)) is None  # a is at its own limit: no one gives way
    assert budget.active == 4


def test_streams_that_move_are_not_given_up_to_the_same_client():
    budget = StreamBudget(per_client=2, total=16)
    _fill(budget, "a", 2)
    assert budget.begin("a", ("f", "a", 9)) is None and budget.active == 2


# ------------------------------------------------------------------------------------------------ L5: If-Range with a date


def test_if_range_with_a_date_needs_the_exact_value_and_a_second_of_age():
    etag, date = '"e"', "Mon, 01 Jan 2024 00:00:00 GMT"
    old, fresh = time.time_ns() - 3_600 * 10**9, time.time_ns() - 200_000_000
    assert fileserve.if_range_ok(None, etag, date, fresh)
    assert fileserve.if_range_ok(etag, etag, date, fresh)  # the ETag is strong at any age
    assert fileserve.if_range_ok(date, etag, date, old)
    assert not fileserve.if_range_ok(date, etag, date, fresh)  # modified less than a second ago: a rewrite keeps the date
    assert not fileserve.if_range_ok(date, etag, date, time.time_ns() + 5 * 10**9)  # dated in the future
    assert not fileserve.if_range_ok("Mon, 01 Jan 2024 00:00:01 GMT", etag, date, old)  # not exactly equal
    assert not fileserve.if_range_ok("W/" + etag, etag, date, old)
    assert fileserve.if_range_ok(date, etag, date)  # age unknown: as before


async def test_a_file_changed_a_moment_ago_is_not_resumed_by_its_date(tmp_path):
    client, app, lib = await start(tmp_path, {"big.mp3": blob(100_000)})
    try:
        os.utime(lib / "big.mp3", None)  # modified just now
        head = await client.head(BIG, headers=AUTH)
        modified, etag = head.headers["Last-Modified"], head.headers["ETag"]
        by_date = await client.get(BIG, headers={**AUTH, "Range": "bytes=10-19", "If-Range": modified})
        assert by_date.status == 200 and len(await by_date.read()) == 100_000
        by_etag = await client.get(BIG, headers={**AUTH, "Range": "bytes=10-19", "If-Range": etag})
        assert by_etag.status == 206
        old = time.time() - 3600
        os.utime(lib / "big.mp3", (old, old))
        modified = (await client.head(BIG, headers=AUTH)).headers["Last-Modified"]
        by_date = await client.get(BIG, headers={**AUTH, "Range": "bytes=10-19", "If-Range": modified})
        assert by_date.status == 206
    finally:
        await client.close()


# ------------------------------------------------------------------------------------------------ L6: sync_dscp


def test_mark_background_is_a_no_op_when_the_option_is_off():
    class Boom:
        def get_extra_info(self, name):
            raise AssertionError("the socket must not be touched")

    off = types.SimpleNamespace(app={fileserve.SYNC_DSCP: False}, transport=Boom())
    fileserve.mark_background(off)  # no exception: returned before looking at the socket
    on = types.SimpleNamespace(app={}, transport=types.SimpleNamespace(get_extra_info=lambda n: None))
    fileserve.mark_background(on)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows ignores IP_TOS set by a plain setsockopt")
async def test_the_option_decides_whether_connections_are_marked(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    for dscp in (True, False):
        (tmp_path / str(dscp)).mkdir()
        lib = make_library(tmp_path / str(dscp), {"big.mp3": blob(1000)})
        seen = []
        app = SY.create_sync_app(SY.LibraryIndex(lib, types.SimpleNamespace(
            get_state=lambda k: None, set_state=lambda k, v: None), settle=0.0), TOKEN, dscp=dscp)
        client = TestClient(TestServer(app))
        await client.start_server()
        real = fileserve.mark_background

        def watching(request, real=real, seen=seen):
            real(request)
            seen.append(request.transport.get_extra_info("socket").getsockopt(socket.IPPROTO_IP, socket.IP_TOS))

        fileserve.mark_background = watching
        try:
            await (await client.get(BIG, headers={**AUTH, "Range": "bytes=0-9"})).read()
        finally:
            fileserve.mark_background = real
            await client.close()
        assert seen == [fileserve.DSCP_CS1_TOS if dscp else 0], (dscp, seen)


def test_the_option_is_in_the_config_schema_translations_and_docs():
    root = Path(__file__).resolve().parent.parent
    config = (root / "config.yaml").read_text(encoding="utf-8")
    assert "  sync_dscp: true\n" in config and "  sync_dscp: bool\n" in config
    for rel in ("translations/en.yaml", "translations/de.yaml"):
        assert "  sync_dscp:\n    name:" in (root / rel).read_text(encoding="utf-8"), rel
    assert "`sync_dscp`" in (root / "DOCS.md").read_text(encoding="utf-8")
    assert "sync_dscp" in (root / "CHANGELOG.md").read_text(encoding="utf-8")


def test_sync_dscp_is_read_from_the_options(tmp_path):
    from car_media_hub import config as C

    assert C.Config.__dataclass_fields__["sync_dscp"].default is True
    assert C._bool({"sync_dscp": False}, "sync_dscp", True) is False and C._bool({}, "sync_dscp", True) is True
