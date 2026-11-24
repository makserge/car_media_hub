"""Import of a region file from a URL: local test server, resume, limits, netguard refusals."""
from __future__ import annotations

import asyncio
import hashlib

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from car_media_hub import regions as R
from car_media_hub.regions_import import ImportPolicy, _public_address, check_url_syntax, display_url
from test_regions import old, pmtiles, root, wait_hashed  # noqa: F401  (root is a fixture)

LOCAL = ImportPolicy(require_https=False, check_hosts=False)  # a local test server: no https, no address check


@pytest.fixture
async def origin():
    """A local server: whole file, ranged file, a file that breaks half way, junk, a huge file and a redirect."""
    data = pmtiles(300_000)
    calls = []

    async def whole(request):
        calls.append(dict(request.headers))
        return web.Response(body=data, headers={"ETag": '"v1"'})

    async def ranged(request):
        calls.append(dict(request.headers))
        rng = request.headers.get("Range")
        if rng and request.headers.get("If-Range", '"v1"') == '"v1"':
            start = int(rng.split("=")[1].split("-")[0])
            return web.Response(status=206, body=data[start:], headers={
                "ETag": '"v1"', "Content-Range": f"bytes {start}-{len(data) - 1}/{len(data)}"})
        return web.Response(body=data, headers={"ETag": '"v1"'})

    async def flaky(request):
        if request.headers.get("Range"):
            return await ranged(request)
        calls.append(dict(request.headers))
        resp = web.StreamResponse(headers={"Content-Length": str(len(data)), "ETag": '"v1"'})
        await resp.prepare(request)
        await resp.write(data[:100_000])
        await asyncio.sleep(1.5)  # let the client read it before the connection breaks (a loaded machine is slow)
        request.transport.abort()
        return resp

    async def junk(request):
        return web.Response(body=b"<html>not a map</html>" * 50)

    async def huge(request):
        return web.Response(body=b"\x00" * (2 * 1024 * 1024))

    async def redirect(request):
        raise web.HTTPFound(request.query["to"])

    async def forbidden(request):
        return web.Response(status=403)

    app = web.Application()
    for path, handler in (("/whole", whole), ("/ranged", ranged), ("/flaky", flaky), ("/junk", junk), ("/huge", huge),
                          ("/redir", redirect), ("/forbidden", forbidden)):
        app.router.add_get(path, handler)
    server = TestServer(app)
    await server.start_server()
    yield server, data, calls
    await server.close()


async def run_task(task, timeout=15):
    await asyncio.wait_for(asyncio.gather(task.job, return_exceptions=True), timeout)
    return task


@pytest.fixture
async def imgr(root):  # noqa: F811
    m = R.RegionManager(root, 1024 * 1024, min_free=0, import_policy=LOCAL, import_retry_delays=())
    await m.start()
    await m.create_region("imp-1", {"name": "Imp"})
    yield m
    await m.stop()


def url_of(server, path):
    return f"http://127.0.0.1:{server.port}{path}"


def leftovers(root, region="imp-1"):
    return [p.name for p in (root / region).iterdir() if p.name.startswith(".import")]


async def test_import_success_with_sha(imgr, origin, root):
    server, data, _ = origin
    task = imgr.start_import("imp-1", "map", url_of(server, "/whole?token=SECRET"), hashlib.sha256(data).hexdigest().upper())
    assert "SECRET" not in task.url and "token" not in task.url and task.url.startswith("http://127.0.0.1")
    assert imgr._busy == {("imp-1", "map")}
    with pytest.raises(R.RegionError) as busy:  # the same file twice at once
        imgr.start_import("imp-1", "map", url_of(server, "/whole"))
    assert busy.value.status == 409
    await run_task(task)
    assert task.state == "done", task.error
    assert (root / "imp-1" / "map.pmtiles").read_bytes() == data and task.downloaded == len(data) == task.total
    assert leftovers(root) == [] and not imgr._busy
    old(root / "imp-1" / "map.pmtiles")  # a file that was just written waits 10 s before it is hashed
    regions = await wait_hashed(imgr)
    assert regions[0].files["map"].sha256 == hashlib.sha256(data).hexdigest()
    public = task.public()
    assert public["state"] == "done" and public["finished"] and "SECRET" not in str(public)


async def test_import_sha_mismatch_bad_magic_limit_and_http_error(imgr, origin, root):
    server, data, _ = origin
    t1 = await run_task(imgr.start_import("imp-1", "map", url_of(server, "/whole"), "0" * 64))
    assert t1.state == "failed" and "SHA-256" in t1.error and not (root / "imp-1" / "map.pmtiles").exists()
    t2 = await run_task(imgr.start_import("imp-1", "map", url_of(server, "/junk")))
    assert t2.state == "failed" and "PMTiles" in t2.error and not (root / "imp-1" / "map.pmtiles").exists()
    t3 = await run_task(imgr.start_import("imp-1", "places", url_of(server, "/huge")))
    assert t3.state == "failed" and "larger than the limit" in t3.error
    t4 = await run_task(imgr.start_import("imp-1", "map", url_of(server, "/forbidden")))
    assert t4.state == "failed" and "403" in t4.error
    assert leftovers(root) == [] or leftovers(root) == [".import-map.json"]  # nothing partial that could be reused wrongly
    assert not (root / "imp-1" / "places.sqlite").exists()


async def test_import_resumes_after_a_broken_connection(imgr, origin, root):
    server, data, calls = origin
    t1 = await run_task(imgr.start_import("imp-1", "map", url_of(server, "/flaky")))
    assert t1.state == "failed" and "resume" in t1.error
    part = root / "imp-1" / ".import-map.part"
    assert part.exists() and 0 < part.stat().st_size < len(data)
    t2 = await run_task(imgr.start_import("imp-1", "map", url_of(server, "/flaky")))
    assert t2.state == "done", t2.error
    assert t2.resumed_from > 0
    assert calls[-1]["Range"].startswith("bytes=") and calls[-1]["If-Range"] == '"v1"'
    assert (root / "imp-1" / "map.pmtiles").read_bytes() == data and leftovers(root) == []


async def test_import_restarts_when_the_server_ignores_ranges(imgr, origin, root):
    server, data, _ = origin
    (root / "imp-1" / ".import-map.part").write_bytes(pmtiles(1000))
    import json
    import hashlib as h
    (root / "imp-1" / ".import-map.json").write_text(json.dumps({"url": h.sha256(url_of(server, "/whole").encode()).hexdigest(), "etag": '"v1"'}))
    task = await run_task(imgr.start_import("imp-1", "map", url_of(server, "/whole")))  # answers 200 to a Range request
    assert task.state == "done", task.error
    assert (root / "imp-1" / "map.pmtiles").read_bytes() == data


async def test_import_never_reuses_a_part_of_another_address(imgr, origin, root):
    server, data, _ = origin
    (root / "imp-1" / ".import-map.part").write_bytes(b"junk" * 100)
    task = await run_task(imgr.start_import("imp-1", "map", url_of(server, "/whole")))
    assert task.state == "done" and task.resumed_from == 0
    assert (root / "imp-1" / "map.pmtiles").read_bytes() == data


async def test_import_cancel_keeps_part_for_resume(imgr, root):
    gate = asyncio.Event()

    async def slow(request):
        resp = web.StreamResponse(headers={"Content-Length": "100000"})
        await resp.prepare(request)
        await resp.write(pmtiles(50_000))
        await gate.wait()
        return resp

    app = web.Application()
    app.router.add_get("/slow", slow)
    server = TestServer(app)
    await server.start_server()
    try:
        task = imgr.start_import("imp-1", "map", url_of(server, "/slow"))
        for _ in range(100):
            if task.downloaded >= 50_000:
                break
            await asyncio.sleep(0.05)
        assert task.downloaded >= 50_000 and task.state == "running"
        assert imgr.cancel_task(task.id) is True
        await run_task(task)
        assert task.state == "cancelled"
        assert (root / "imp-1" / ".import-map.part").stat().st_size >= 50_000 and not imgr._busy
        assert imgr.cancel_task(task.id) is False
        with pytest.raises(R.RegionError):
            imgr.cancel_task("nope")
        with pytest.raises(R.RegionError):  # a running or kept download blocks nothing, but the region still exists
            await imgr.delete_region("nope")
    finally:
        gate.set()
        await server.close()


async def test_import_stops_when_the_task_limit_is_reached(imgr, origin):
    server, data, _ = origin
    from car_media_hub import regions_import

    regions_import.MAX_QUEUED_IMPORTS = 2
    tasks = [imgr.start_import("imp-1", kind, url_of(server, "/whole")) for kind in ("map", "routing")]
    assert len(tasks) == regions_import.MAX_QUEUED_IMPORTS
    with pytest.raises(R.RegionError) as exc:
        imgr.start_import("imp-1", "places", url_of(server, "/whole"))
    assert exc.value.status == 429
    for t in tasks:
        await run_task(t)
    regions_import.MAX_QUEUED_IMPORTS = 20


async def test_import_redirect_to_private_address_is_refused(root, origin):
    server, data, _ = origin

    async def resolve(host, port):
        return "127.0.0.1"  # "good.test" is pinned to the local server

    policy = ImportPolicy(require_https=False, check_hosts=True, resolve=resolve)
    m = R.RegionManager(root, 1 << 20, min_free=0, import_policy=policy)
    await m.start()
    await m.create_region("imp-2", {"name": "x"})
    try:
        ok = await run_task(m.start_import("imp-2", "map", f"http://good.test:{server.port}/whole"))
        assert ok.state == "done", ok.error  # connects to the pinned address, Host stays good.test
        for target in (f"http://127.0.0.1:{server.port}/whole", "http://192.168.1.10/x", "http://localhost/x", "http://169.254.169.254/",
                       "ftp://example.com/x"):
            bad = await run_task(m.start_import("imp-2", "places", f"http://good.test:{server.port}/redir?to={target}"))
            assert bad.state == "failed" and ("refused" in bad.error or "only https" in bad.error), (target, bad.error)
    finally:
        await m.stop()


@pytest.mark.parametrize("url", [
    "http://example.com/map.pmtiles", "ftp://example.com/a", "file:///etc/passwd", "https://127.0.0.1/a.pmtiles",
    "https://localhost/a", "https://192.168.1.5/a", "https://10.0.0.1/a", "https://172.30.32.1:8123/a",
    "https://169.254.169.254/latest", "https://[::1]/a", "https://[fd00::1]/a", "https://router.local/a",
    "https://supervisor/a", "https://homeassistant/a", "https://user:pw@example.com/a", "https://example.com:25/a",
    "https://2130706433/a", "https://0x7f.1/a", "https://example.com:99999/a", "https://100.64.1.1/a",
    "", "   ", "https://", "//example.com/a", "https://exa mple.com/a", "javascript:alert(1)",
    "https://example.com/" + "a" * 3000,
])
async def test_import_refuses_unsafe_addresses(root, url):
    strict = R.RegionManager(root, 1 << 20, min_free=0)  # the production policy (https only, netguard)
    await strict.create_region("imp-9", {"name": "x"})
    with pytest.raises(R.RegionError):
        strict.start_import("imp-9", "map", url)
    assert not strict._busy and not strict._tasks


def test_public_https_addresses_pass_the_syntax_check():
    for url in ("https://example.com/map.pmtiles", "https://cdn.example.org:8443/a/b?x=1", "https://93.184.216.34/a"):
        assert check_url_syntax(url, ImportPolicy()) == url


def test_display_url_drops_query_and_credentials():
    assert display_url("https://user:pw@example.com:8443/a/b.pmtiles?sig=SECRET#frag") == "https://example.com:8443/a/b.pmtiles"


async def test_name_resolving_to_a_private_address_is_refused():
    for host in ("localhost", "127.0.0.1", "10.1.2.3"):
        with pytest.raises(R.RegionError):
            await _public_address(host, 443)


async def test_import_argument_validation(root):
    strict = R.RegionManager(root, 1 << 20, min_free=0)
    await strict.create_region("imp-3", {"name": "x"})
    for args in (("imp-3", "map", "https://example.com/a", "xyz"), ("imp-3", "bogus", "https://example.com/a"),
                 ("nope", "map", "https://example.com/a"), ("imp-3", "map", 12345), ("../x", "map", "https://example.com/a")):
        with pytest.raises(R.RegionError):
            strict.start_import(*args)
    assert not strict._busy


async def test_logs_name_regions_and_files_but_no_secrets(imgr, origin, caplog):
    caplog.set_level("DEBUG")
    server, data, _ = origin
    await run_task(imgr.start_import("imp-1", "map", url_of(server, "/whole?token=TOPSECRET")))
    ours = " ".join(r.getMessage() for r in caplog.records if r.name.startswith("car_media_hub"))  # not the test server's access log
    assert "TOPSECRET" not in ours and "imp-1" in ours
