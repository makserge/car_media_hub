"""Host benchmark of the sync API: N parallel range clients against the file route (and the probe), MB/s per stream count.

Regression tool for the add-on's serving code, not a measurement of the real Wi-Fi: by default the server runs in this
process on localhost with a temporary library (one random file), so the numbers show what the Python side costs.

    python tools/bench_sync.py                          # 256 MiB file, 1/2/4/8 streams, 8 MiB segments
    python tools/bench_sync.py --size-mb 64 --streams 1,2,4 --repeat 3
    python tools/bench_sync.py --url http://192.168.8.233:8100 --token <sync_token>      # a real add-on, from this PC
    python tools/bench_sync.py --url http://host:8100 --token <t> --file "Album/01.mp3"  # a given listed file

With ``--url`` the biggest listed file (or ``--file``) is fetched with ``Range`` requests and the probe is run as well.
The client is aiohttp only; the in-process mode needs the add-on package (``app/`` next to ``tools/``).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import statistics
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

import aiohttp

APP = Path(__file__).resolve().parents[1] / "app"
if APP.is_dir() and str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

TOKEN = "bench-token-" + "x" * 12
MIB = 1 << 20


def make_file(path: Path, size: int) -> None:
    block = os.urandom(MIB)
    with open(path, "wb") as f:
        left = size
        while left:
            n = min(left, MIB)
            f.write(block[:n])
            left -= n
    old = time.time() - 3600
    os.utime(path, (old, old))


async def fetch_range(session: aiohttp.ClientSession, url: str, headers: dict, start: int, end: int) -> int:
    for attempt in range(50):
        async with session.get(url, headers={**headers, "Range": f"bytes={start}-{end}"}) as resp:
            if resp.status == 503:  # over the stream budget: wait as the server says
                await asyncio.sleep(float(resp.headers.get("Retry-After", "1")) / 4)
                continue
            if resp.status != 206:
                raise RuntimeError(f"expected 206, got {resp.status}")
            got = 0
            async for chunk in resp.content.iter_chunked(256 * 1024):
                got += len(chunk)
            if got != end - start + 1:
                raise RuntimeError(f"short body: {got} of {end - start + 1}")
            return got
    raise RuntimeError("the server stayed busy")


async def run_ranges(base: str, headers: dict, name: str, size: int, streams: int, segment: int) -> float:
    """Download the whole file as ``segment`` sized ranges with ``streams`` workers; seconds taken."""
    url = f"{base}/api/v1/files/{urllib.parse.quote(name)}"
    segments = [(s, min(s + segment, size) - 1) for s in range(0, size, segment)]
    queue: asyncio.Queue = asyncio.Queue()
    for item in segments:
        queue.put_nowait(item)

    async def worker(session: aiohttp.ClientSession) -> None:
        while True:
            try:
                start, end = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            await fetch_range(session, url, headers, start, end)

    connector = aiohttp.TCPConnector(limit=streams, force_close=False)
    async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=None)) as session:
        t0 = time.perf_counter()
        await asyncio.gather(*(worker(session) for _ in range(streams)))
        return time.perf_counter() - t0


async def run_probe(base: str, headers: dict, streams: int, mib: int) -> tuple[float, int]:
    async def one(session: aiohttp.ClientSession) -> int:
        async with session.get(f"{base}/api/probe?bytes={mib * MIB}", headers=headers) as resp:
            if resp.status != 200:
                raise RuntimeError(f"probe answered {resp.status}")
            got = 0
            async for chunk in resp.content.iter_chunked(256 * 1024):
                got += len(chunk)
            return got

    connector = aiohttp.TCPConnector(limit=streams, force_close=False)
    async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=None)) as session:
        t0 = time.perf_counter()
        sizes = await asyncio.gather(*(one(session) for _ in range(streams)))
        return time.perf_counter() - t0, sum(sizes)


async def remote_file(base: str, headers: dict, wanted: str | None) -> tuple[str, int]:
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{base}/api/v1/files", headers=headers) as resp:
            resp.raise_for_status()
            listing = await resp.json()
    if wanted:
        for entry in listing:
            if entry["name"] == wanted:
                return entry["name"], entry["size"]
        raise SystemExit(f"{wanted!r} is not in the listing")
    if not listing:
        raise SystemExit("the library is empty")
    biggest = max(listing, key=lambda e: e["size"])
    return biggest["name"], biggest["size"]


async def local_server(tmp: Path, size: int):
    from aiohttp import web

    from car_media_hub.sync import LibraryIndex, create_sync_app

    class State:
        def __init__(self) -> None:
            self.values: dict[str, str] = {}

        def get_state(self, key: str):
            return self.values.get(key)

        def set_state(self, key: str, value: str) -> None:
            self.values[key] = value

    lib = tmp / "lib"
    lib.mkdir()
    await asyncio.to_thread(make_file, lib / "bench.mp3", size)
    app = create_sync_app(LibraryIndex(lib, State(), settle=0.0), TOKEN)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    return runner, f"http://127.0.0.1:{port}"


async def main(args: argparse.Namespace) -> int:
    streams_list = [int(s) for s in args.streams.split(",") if s.strip()]
    segment = args.segment_mb * MIB
    tmp = None
    runner = None
    if args.url:
        base, headers = args.url.rstrip("/"), {"Authorization": f"Bearer {args.token}"}
        name, size = await remote_file(base, headers, args.file)
        print(f"remote {base}: {name!r}, {size / MIB:.1f} MiB")
    else:
        from car_media_hub import fileserve

        if args.sendfile != "auto":
            fileserve.USE_SENDFILE = args.sendfile == "on"
        tmp = Path(tempfile.mkdtemp(prefix="bench_sync_"))
        runner, base = await local_server(tmp, args.size_mb * MIB)
        headers = {"Authorization": f"Bearer {TOKEN}"}
        name, size = "bench.mp3", args.size_mb * MIB
        print(f"in-process server {base}, {size / MIB:.0f} MiB file, "
              f"{'sendfile' if fileserve.USE_SENDFILE else 'copy (no sendfile on this platform)'}, python {sys.version.split()[0]}")
    try:
        print(f"ranges of {args.segment_mb} MiB, best and median of {args.repeat} runs (MB/s = 10^6 bytes per second)")
        print(f"{'streams':>8} {'best MB/s':>11} {'median MB/s':>12} {'seconds':>9}")
        await run_ranges(base, headers, name, size, 1, segment)  # warm-up (page cache, connections)
        for streams in streams_list:
            times = [await run_ranges(base, headers, name, size, streams, segment) for _ in range(args.repeat)]
            rates = [size / t / 1e6 for t in times]
            print(f"{streams:>8} {max(rates):>11.1f} {statistics.median(rates):>12.1f} {min(times):>9.2f}")
        if args.probe_mb:
            print(f"probe /api/probe, {args.probe_mb} MiB per stream")
            for streams in streams_list:
                seconds, total = await run_probe(base, headers, streams, args.probe_mb)
                print(f"{streams:>8} {total / seconds / 1e6:>11.1f} MB/s aggregate")
    finally:
        if runner is not None:
            await runner.cleanup()
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
    return 0


def parse() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", help="base URL of a running add-on (default: start one in this process on localhost)")
    p.add_argument("--token", default="", help="sync token for --url")
    p.add_argument("--file", help="listed file name to fetch with --url (default: the biggest)")
    p.add_argument("--size-mb", type=int, default=256, help="size of the temporary file in the in-process mode")
    p.add_argument("--streams", default="1,2,4,8", help="comma separated parallel stream counts (at most 8 per client)")
    p.add_argument("--segment-mb", type=int, default=8, help="range segment size")
    p.add_argument("--repeat", type=int, default=3)
    p.add_argument("--sendfile", choices=("auto", "on", "off"), default="auto",
                   help="in-process mode: force sendfile on or off (auto: on except on Windows, see fileserve.USE_SENDFILE)")
    p.add_argument("--probe-mb", type=int, default=8, help="also run /api/probe with this many MiB per stream (0 = off)")
    args = p.parse_args()
    if args.url and not args.token:
        p.error("--token is needed with --url")
    return args


if __name__ == "__main__":
    sys.exit(asyncio.run(main(parse())))
