"""Re-review of B1: symlink planting (C1), sandbox gate (H2), image (H1), WebRTC (M1), proxy keep-alive (M2),
gesture window (M3), manual_only stored with the release (M4), process kill loop (M5), blocklist gaps (L4)."""
import asyncio
import os
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from car_media_hub import browser as br
from car_media_hub import browser_proc as bp
from car_media_hub import browser_proxy as bpx
from car_media_hub.config import Config
from car_media_hub.netguard import BlockedURL, check_navigation_url, host_problem, ip_is_blocked
from car_media_hub.pipeline import Controller

ROOT = Path(__file__).resolve().parents[1]
posix_fd = pytest.mark.skipif(not bp._FD_OK, reason="needs O_NOFOLLOW, dir_fd and fchown (POSIX only)")
is_root = hasattr(os, "geteuid") and os.geteuid() == 0
NOBODY = (65534, 65534)


def cfg_for(tmp_path, **kw):
    return Config(inbox_dir=tmp_path / "inbox", staging_dir=tmp_path / "st", library_dir=tmp_path / "lib",
                  data_dir=tmp_path / "data", **kw)


def mode(path) -> int:
    return os.lstat(path).st_mode & 0o7777


# ---------------------------------------------------------------- C1: planted symlinks
@posix_fd
@pytest.mark.parametrize("sub", ["tmp", ".config", ".cache"])
def test_prepare_profile_replaces_a_symlinked_subfolder_and_never_touches_its_target(tmp_path, sub):
    victim = tmp_path / "victim"
    victim.mkdir()
    os.chmod(victim, 0o755)
    profile = tmp_path / "browser-profile"
    profile.mkdir()
    os.symlink(victim, profile / sub)
    bp.prepare_profile(profile, None)
    assert not (profile / sub).is_symlink() and (profile / sub).is_dir()
    assert mode(profile / sub) == 0o700
    assert mode(victim) == 0o755 and list(victim.iterdir()) == []


@posix_fd
def test_prepare_profile_replaces_a_symlink_to_a_file_and_a_symlinked_profile(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("x")
    os.chmod(secret, 0o644)
    profile = tmp_path / "browser-profile"
    os.symlink(tmp_path, profile)  # the profile folder itself is a link
    bp.prepare_profile(profile, None)
    assert profile.is_dir() and not profile.is_symlink()
    os.rmdir(profile / "tmp"), os.rmdir(profile / ".config"), os.rmdir(profile / ".cache")
    os.symlink(secret, profile / "tmp")  # a link to a file, not a folder
    bp.prepare_profile(profile, None)
    assert (profile / "tmp").is_dir() and mode(secret) == 0o644 and secret.read_text() == "x"


@posix_fd
def test_prepare_profile_survives_dangling_and_looping_links(tmp_path):
    profile = tmp_path / "browser-profile"
    profile.mkdir()
    os.symlink(tmp_path / "does-not-exist", profile / "tmp")
    os.symlink(".config", profile / ".config")  # loops onto itself
    os.symlink("../outside", profile / ".cache")
    bp.prepare_profile(profile, None)
    assert all((profile / s).is_dir() and not (profile / s).is_symlink() for s in ("tmp", ".config", ".cache"))
    assert not (tmp_path / "outside").exists()


@posix_fd
@pytest.mark.skipif(not is_root, reason="needs root to change owners")
def test_prepare_profile_as_root_does_not_chown_what_a_link_points_to(tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    vfile = victim / "f"
    vfile.write_text("x")
    profile = tmp_path / "browser-profile"
    profile.mkdir()
    os.symlink(victim, profile / ".cache")
    os.symlink(victim, profile / "tmp")
    (profile / ".config").mkdir()
    os.symlink(vfile, profile / ".config" / "nested-link")  # reached by the migration walk
    os.link(vfile, profile / ".config" / "hardlink")  # more than one link: must be left alone
    bp.prepare_profile(profile, NOBODY)
    assert os.lstat(victim).st_uid == 0 and os.lstat(vfile).st_uid == 0 and mode(victim) == 0o755
    assert os.lstat(profile / ".cache").st_uid == NOBODY[0] and (profile / ".cache").is_dir()
    assert os.lstat(profile).st_uid == NOBODY[0] and mode(profile) == 0o700
    assert os.lstat(profile / ".config" / "nested-link").st_uid == NOBODY[0]  # the link itself, not its target
    assert os.lstat(profile / ".config" / "hardlink").st_uid == 0


@posix_fd
def test_prepare_incoming_replaces_planted_links_and_empties_the_folder(tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    os.chmod(victim, 0o755)
    (victim / "keep").write_text("k")
    root = tmp_path / "browser-incoming"
    root.mkdir()
    os.symlink(victim, root / "dl")  # the download folder is a link
    os.symlink(victim, root / "other")
    (root / "old.zip").write_text("stale")
    folder = bp.prepare_incoming(root, None)
    assert folder == root / "dl" and folder.is_dir() and not folder.is_symlink()
    assert sorted(p.name for p in root.iterdir()) == ["dl"]
    assert mode(root) == 0o711 and mode(folder) == 0o700
    assert (victim / "keep").read_text() == "k" and mode(victim) == 0o755
    # content planted inside the folder (also a link to a folder) is removed without following it
    (folder / "x").mkdir()
    os.symlink(victim, folder / "x" / "link")
    bp.prepare_incoming(root, None)
    assert list(folder.iterdir()) == [] and (victim / "keep").exists()


@posix_fd
@pytest.mark.skipif(not is_root, reason="needs root to change owners")
def test_incoming_root_stays_root_owned_and_only_dl_belongs_to_the_browser_user(tmp_path):
    root = tmp_path / "browser-incoming"
    folder = bp.prepare_incoming(root, NOBODY)
    assert os.lstat(root).st_uid == 0 and mode(root) == 0o711
    assert os.lstat(folder).st_uid == NOBODY[0] and mode(folder) == 0o700


def test_downloads_are_outside_the_browser_profile(tmp_path):
    cfg = cfg_for(tmp_path)
    assert cfg.browser_profile_dir not in cfg.browser_incoming_dir.parents
    assert cfg.browser_incoming_root.parent == cfg.data_dir and cfg.browser_incoming_dir.parent == cfg.browser_incoming_root


@posix_fd
async def test_browser_prepare_folders_never_follows_planted_links(tmp_path):
    cfg = cfg_for(tmp_path)
    victim = tmp_path / "victim"
    victim.mkdir()
    os.chmod(victim, 0o755)
    cfg.browser_profile_dir.mkdir(parents=True)
    for sub in (".cache", ".config", "tmp", "downloads"):
        os.symlink(victim, cfg.browser_profile_dir / sub)
    cfg.browser_incoming_root.mkdir()
    os.symlink(victim, cfg.browser_incoming_dir)
    folder = br.EmbeddedBrowser._prepare_folders(cfg.browser_profile_dir, cfg.browser_incoming_root, cfg.browser_ready_dir, None)
    assert folder == cfg.browser_incoming_dir and folder.is_dir() and not folder.is_symlink()
    assert mode(victim) == 0o755 and list(victim.iterdir()) == []
    assert mode(cfg.browser_ready_dir) == 0o700


def test_read_small_file_refuses_links_and_non_files(tmp_path):
    real = tmp_path / "DevToolsActivePort"
    real.write_bytes(b"9222\n/devtools/browser/abc\n")
    assert bp.read_small_file(real) == "9222\n/devtools/browser/abc\n"
    assert bp.read_small_file(tmp_path / "missing") is None
    assert bp.read_small_file(tmp_path) is None
    if hasattr(os, "O_NOFOLLOW"):
        link = tmp_path / "link"
        os.symlink(real, link)
        assert bp.read_small_file(link) is None
    assert len(bp.read_small_file(real, 5)) == 5


@posix_fd
def test_harden_data_dir_makes_data_enterable_but_not_listable(tmp_path):
    bp.harden_data_dir(tmp_path)
    assert mode(tmp_path) == 0o711


@posix_fd
async def test_hand_over_refuses_hard_links_and_never_overwrites(tmp_path):
    from test_browser_downloads import make  # same helpers as the download tests

    browser, cfg, _ = make(tmp_path)
    secret = tmp_path / "secret.bin"
    secret.write_bytes(b"root only")
    os.link(secret, cfg.browser_incoming_dir / "11111111-1")
    with pytest.raises(ValueError):
        browser._hand_over("11111111-1", "A.zip")
    assert not any(cfg.browser_ready_dir.iterdir())
    for i, guid in enumerate(("22222222-1", "33333333-1")):  # two downloads of the same name keep both
        (cfg.browser_incoming_dir / guid).write_bytes(b"data%d" % i)
        browser._hand_over(guid, "Same.zip")
    assert sorted(p.name for p in cfg.browser_ready_dir.iterdir()) == ["Same (2).zip", "Same.zip"]
    assert (cfg.browser_ready_dir / "Same.zip").read_bytes() == b"data0"


# ---------------------------------------------------------------- H2: no browser without the sandbox
def browser_for(tmp_path, monkeypatch, sandbox: bool, **kw):
    from test_browser_downloads import make

    browser, cfg, _ = make(tmp_path, **kw)
    monkeypatch.setattr(br, "find_binary", lambda: "/usr/bin/chromium")
    monkeypatch.setattr(br, "unprivileged_user", lambda: None)

    async def version(binary, ids, timeout=15.0):
        return "Chromium 150.0.1.2 built on Debian"

    monkeypatch.setattr(br, "read_chromium_version", version)

    async def probe(ids, timeout=10.0):
        return sandbox

    monkeypatch.setattr(br, "detect_sandbox_support", probe)
    return browser, cfg


async def test_browser_does_not_start_without_a_sandbox_by_default(tmp_path, monkeypatch):
    browser, cfg = browser_for(tmp_path, monkeypatch, sandbox=False)
    spawned = []

    async def no_spawn(*a, **k):
        spawned.append(a)
        raise AssertionError("must not start a process")

    monkeypatch.setattr(br.asyncio, "create_subprocess_exec", no_spawn)
    with pytest.raises(br.BrowserError, match="browser_allow_unsandboxed"):
        await browser.ensure_started()
    assert spawned == [] and browser._proc is None and browser._proxy is None
    assert not cfg.browser_profile_dir.exists()  # not even the folders were prepared


async def test_explicit_option_lets_the_browser_pass_the_sandbox_gate(tmp_path, monkeypatch):
    browser, cfg = browser_for(tmp_path, monkeypatch, sandbox=False, browser_allow_unsandboxed=True)
    started = []

    async def fake_spawn(*args, **kwargs):
        started.append(args)
        raise OSError("stop here")  # past the gate: the first process start is reached

    monkeypatch.setattr(br.asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr(br.shutil, "which", lambda name: None)
    with pytest.raises(OSError):
        await browser.ensure_started()
    assert started and "--no-sandbox" in started[0]


async def test_a_failed_sandbox_start_is_not_retried_without_it_unless_allowed(tmp_path, monkeypatch):
    for allowed in (False, True):
        browser, _ = browser_for(tmp_path / str(allowed), monkeypatch, sandbox=True, browser_allow_unsandboxed=allowed)
        attempts = []
        browser._sandbox = True  # the probe said yes at the first start

        async def fake_start():
            attempts.append(browser._sandbox)
            raise br.BrowserError("Chromium exited at start (code 1)")

        browser._start_locked = fake_start
        with pytest.raises(br.BrowserError):
            await browser.ensure_started()
        assert attempts == ([True, False] if allowed else [True])


def test_option_default_is_off_and_documented_with_a_warning():
    yaml = pytest.importorskip("yaml")
    data = yaml.safe_load((ROOT / "config.yaml").read_text())
    assert data["options"]["browser_allow_unsandboxed"] is False and data["schema"]["browser_allow_unsandboxed"] == "bool"
    assert Config(inbox_dir=Path("a"), staging_dir=Path("b"), library_dir=Path("c")).browser_allow_unsandboxed is False
    for lang in ("en", "de"):
        text = yaml.safe_load((ROOT / "translations" / f"{lang}.yaml").read_text(encoding="utf-8"))["configuration"]
        warn = text["browser_allow_unsandboxed"]["description"]
        assert "WARN" in warn and "LAN" in warn
        for key in ("browser_enabled", "browser_allow_unsandboxed"):  # no claim that the filter contains a compromised browser
            low = text[key]["description"].lower()
            assert "cannot reach private networks" not in low and "erreicht weder private" not in low
    assert "browser_allow_unsandboxed" in (ROOT / "DOCS.md").read_text()
    assert "not** a boundary against a compromised" in (ROOT / "DOCS.md").read_text()


# ---------------------------------------------------------------- H1 + static config
def test_image_is_debian_based_and_the_ha_config_is_complete():
    yaml = pytest.importorskip("yaml")
    build = yaml.safe_load((ROOT / "build.yaml").read_text())["build_from"]
    assert set(build) == {"aarch64", "amd64"}
    for arch, image in build.items():
        assert re.fullmatch(rf"ghcr\.io/home-assistant/{arch}-base-debian:(trixie|bookworm)", image), image
    config = yaml.safe_load((ROOT / "config.yaml").read_text())
    assert config["timeout"] == 30
    assert "browser-profile/**" in config["backup_exclude"] and "browser-incoming/**" in config["backup_exclude"]
    docker = (ROOT / "Dockerfile").read_text()
    assert "disable_non_proxied_udp" in (ROOT / "policy" / "cmh.json").read_text()
    assert "0711" in (ROOT / "run.sh").read_text()


def test_sync_server_is_stopped_before_the_ha_session_closes():
    source = (ROOT / "app" / "car_media_hub" / "main.py").read_text()
    inside = source.index("async with aiohttp.ClientSession()")
    assert source.index("await sync_server.stop()", inside) < source.index("    finally:", inside)


# ---------------------------------------------------------------- M1: WebRTC flags
def test_webrtc_flags_have_the_policy_on_the_switch():
    a = bp.chromium_args("/usr/bin/chromium", "/p", headless=True, sandbox=True, proxy_port=1, window=(1, 1))
    assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in a
    assert "--webrtc-ip-handling-policy=disable_non_proxied_udp" in a
    assert "--force-webrtc-ip-handling-policy" not in a  # the bare switch (no value) does nothing
    features = next(x for x in a if x.startswith("--disable-features="))
    assert "WebRtcHideLocalIpsWithMdns" not in features  # mDNS hiding stays on


# ---------------------------------------------------------------- M2: one checked request per proxy connection
def test_request_body_length():
    assert bpx.request_body_length(["POST http://a/ HTTP/1.1", "Content-Length: 12"]) == 12
    assert bpx.request_body_length(["GET http://a/ HTTP/1.1", "Accept: */*"]) == 0
    for bad in (["Transfer-Encoding: chunked"], ["Content-Length: 1", "Content-Length: 1"], ["Content-Length: -1"],
                ["Content-Length: 1e3"], [f"Content-Length: {bpx.MAX_BODY + 1}"]):
        with pytest.raises(BlockedURL):
            bpx.request_body_length(["POST http://a/ HTTP/1.1", *bad])


async def _keepalive_server(seen: list[bytes]):
    async def handler(reader, writer):
        try:
            while True:
                data = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
                seen.append(data)
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: keep-alive\r\nKeep-Alive: timeout=5\r\n\r\nok")
                await writer.drain()
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def test_second_request_on_a_kept_alive_connection_is_never_forwarded(monkeypatch):
    seen: list[bytes] = []
    server, target = await _keepalive_server(seen)

    async def fake_resolve(host, port, resolver=None):
        return "127.0.0.1"

    monkeypatch.setattr(bpx, "resolve_public", fake_resolve)
    proxy = bpx.FilteringProxy()
    port = await proxy.start()
    try:
        # (a) a follow-up request sent after the first response, on the same socket (what a pooled connection does)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(f"GET http://good.example.com:{target}/a HTTP/1.1\r\nHost: good.example.com\r\n\r\n".encode())
        await writer.drain()
        data = await asyncio.wait_for(reader.read(), 5)  # the proxy closes the connection after the response
        head = data.split(b"\r\n\r\n")[0].lower()
        assert data.endswith(b"ok") and b"connection: close" in head and b"keep-alive" not in head and head.count(b"connection:") == 1
        try:
            writer.write(f"GET http://other.example.com:{target}/b HTTP/1.1\r\nCookie: s=1\r\n\r\n".encode())
            await writer.drain()
        except ConnectionError:
            pass
        await asyncio.sleep(0.2)
        writer.close()
        # (b) two requests pipelined in one go: only the first is ever forwarded
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(f"GET http://good.example.com:{target}/a2 HTTP/1.1\r\nHost: good.example.com\r\n\r\n"
                     f"GET http://other.example.com:{target}/b2 HTTP/1.1\r\nCookie: s=1\r\n\r\n".encode())
        await writer.drain()
        await asyncio.wait_for(reader.read(), 5)
        await asyncio.sleep(0.2)
        writer.close()
    finally:
        await proxy.stop()
        server.close()
    assert [h.split(b" ")[1] for h in seen] == [b"/a", b"/a2"]  # nothing else reached the server
    assert all(b"Cookie" not in h and b"Connection: close" in h for h in seen)


async def test_request_body_is_forwarded_and_chunked_bodies_are_refused(monkeypatch):
    bodies: list[bytes] = []

    async def handler(reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        length = int(re.search(rb"Content-Length: (\d+)", head).group(1))
        bodies.append(await reader.readexactly(length))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    target = server.sockets[0].getsockname()[1]

    async def fake_resolve(host, port, resolver=None):
        return "127.0.0.1"

    monkeypatch.setattr(bpx, "resolve_public", fake_resolve)
    proxy = bpx.FilteringProxy()
    port = await proxy.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(f"POST http://up.example.com:{target}/u HTTP/1.1\r\nContent-Length: 5\r\n\r\nhel".encode())
        await writer.drain()
        await asyncio.sleep(0.1)
        writer.write(b"lo")
        await writer.drain()
        assert (await asyncio.wait_for(reader.read(), 5)).endswith(b"ok")
        writer.close()
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(f"POST http://up.example.com:{target}/u HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n".encode())
        await writer.drain()
        assert (await asyncio.wait_for(reader.read(), 5)).startswith(b"HTTP/1.1 403")
        writer.close()
    finally:
        await proxy.stop()
        server.close()
    assert bodies == [b"hello"]


async def test_upstream_protocol_switch_is_refused(monkeypatch):
    async def handler(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n\r\nraw")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    target = server.sockets[0].getsockname()[1]

    async def fake_resolve(host, port, resolver=None):
        return "127.0.0.1"

    monkeypatch.setattr(bpx, "resolve_public", fake_resolve)
    proxy = bpx.FilteringProxy()
    port = await proxy.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(f"GET http://ws.example.com:{target}/ HTTP/1.1\r\n\r\n".encode())
        await writer.drain()
        data = await asyncio.wait_for(reader.read(), 5)
        writer.close()
    finally:
        await proxy.stop()
        server.close()
    assert data.startswith(b"HTTP/1.1 502") and b"raw" not in data


# ---------------------------------------------------------------- M3: gesture window
def download_browser(tmp_path):
    from test_browser_downloads import make

    return make(tmp_path)


def begin(browser, guid, frame=None, name="A.zip"):
    params = {"guid": guid, "suggestedFilename": name, "url": "https://x.example.com/f"}
    if frame is not None:
        params["frameId"] = frame
    browser._on_event("Browser.downloadWillBegin", params, None)
    return guid


async def test_one_click_allows_exactly_one_download(tmp_path):
    browser, _, calls = download_browser(tmp_path)
    browser._user_action()
    first = begin(browser, "aaaaaaaa-1")
    second = begin(browser, "bbbbbbbb-1")  # same click: refused
    await asyncio.sleep(0.05)
    assert browser.downloads[first]["state"] == "running" and browser.downloads[second]["state"] == "blocked"
    assert ("Browser.cancelDownload", {"guid": second}) in calls
    browser._user_action()
    assert browser.downloads[begin(browser, "cccccccc-1")]["state"] == "running"
    assert br.DOWNLOAD_GESTURE_SECONDS <= 5


async def test_a_refused_download_does_not_use_up_the_click(tmp_path):
    browser, _, _ = download_browser(tmp_path)
    browser._user_action()
    assert browser.downloads[begin(browser, "aaaaaaaa-1", name="x.exe")]["state"] == "blocked"
    assert browser.downloads[begin(browser, "bbbbbbbb-1")]["state"] == "running"


async def test_download_must_come_from_the_main_frame(tmp_path):
    browser, _, _ = download_browser(tmp_path)
    browser._main_frame = "MAIN"
    browser._user_action()
    ad = begin(browser, "aaaaaaaa-1", frame="AD-IFRAME")
    assert browser.downloads[ad]["state"] == "blocked" and "frame" in browser.downloads[ad]["note"]
    assert browser._gesture_until > time.monotonic()  # the click is still there for the page itself
    assert browser.downloads[begin(browser, "bbbbbbbb-1", frame="MAIN")]["state"] == "running"


async def test_reload_and_feed_items_are_no_gesture_but_clicks_and_typed_addresses_are(tmp_path):
    browser, _, _ = download_browser(tmp_path)
    await browser.handle({"t": "reload"})
    assert browser._gesture_until == 0.0
    await browser.open_item("rss", "1", "https://example.com/item", "1")
    assert browser._gesture_until == 0.0
    await browser.navigate("https://example.com/typed")  # the viewer typed an address
    assert browser._gesture_until > time.monotonic()
    browser._gesture_until = 0.0
    with pytest.raises(br.BrowserError):  # no browser behind it here: the gesture is noted before the event is sent
        await browser.handle({"t": "key", "k": "down", "key": "a", "code": "KeyA", "vk": 65, "text": "a"})
    assert browser._gesture_until > time.monotonic()


# ---------------------------------------------------------------- M4: manual_only survives the source
def test_manual_only_is_decided_by_the_stored_flag_not_the_current_sources():
    ctl = SimpleNamespace(sources=[])  # the browser source is switched off / removed
    release = lambda report: SimpleNamespace(report=report)  # noqa: E731
    assert Controller._manual_only(ctl, release({"source": "browser", "manual_only": True})) is True
    assert Controller._manual_only(ctl, release({"source": "whatever", "manual_only": True})) is True
    assert Controller._manual_only(ctl, release({"source": "browser"})) is True  # made before the flag existed
    assert Controller._manual_only(ctl, release({"source": "rss"})) is False
    assert Controller._manual_only(ctl, release(None)) is False


def test_the_flag_is_written_when_the_release_is_created():
    source = (ROOT / "app" / "car_media_hub" / "pipeline.py").read_text()
    assert 'report["manual_only"] = True' in source
    assert source.index('report["manual_only"] = True') < source.index('self.db.update(release_id, status="pending", report=report)')


async def test_expire_pending_never_approves_a_manual_only_release_without_its_source():
    class Rel(SimpleNamespace):
        pass

    pending = [Rel(id="a", name="A", created_at=0, report={"source": "browser", "manual_only": True}),
               Rel(id="b", name="B", created_at=0, report={"source": "rss"})]
    approved, declined = [], []

    async def approve(rid):
        approved.append(rid)

    async def decline(rid):
        declined.append(rid)

    ctl = SimpleNamespace(
        cfg=SimpleNamespace(pending_timeout_hours=1, pending_timeout_action="approve"), clock=lambda: 10 * 3600,
        db=SimpleNamespace(list=lambda statuses, limit: pending), sources=[], _expiry_logged=set(), _space_warned=set(),
        approve=approve, decline=decline)
    ctl._manual_only = lambda release: Controller._manual_only(ctl, release)
    await Controller.expire_pending(ctl)
    assert approved == ["b"] and declined == []


# ---------------------------------------------------------------- M5: kill until none are left
@pytest.mark.skipif(os.name != "posix", reason="POSIX only")
def test_kill_loop_repeats_until_no_process_is_left(tmp_path, monkeypatch):
    def add(pid):
        d = tmp_path / str(pid)
        d.mkdir()
        (d / "status").write_text("Name:\tx\nUid:\t1010\t1010\t1010\t1010\n")

    add(100)
    next_pid = [200]
    killed = []

    def fake_kill(pid, sig):
        killed.append(pid)
        (tmp_path / str(pid) / "status").unlink()
        (tmp_path / str(pid)).rmdir()
        if len(killed) < 4:  # the forker had spawned a child before it died
            add(next_pid[0])
            next_pid[0] += 1

    monkeypatch.setattr(bp.os, "kill", fake_kill)
    assert bp.kill_user_processes((1010, 1010), str(tmp_path), pause=0) == 4
    assert bp.find_user_pids(1010, str(tmp_path)) == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX only")
def test_kill_loop_is_capped(tmp_path, monkeypatch):
    d = tmp_path / "100"
    d.mkdir()
    (d / "status").write_text("Uid:\t1010\t1010\t1010\t1010\n")
    calls = []
    monkeypatch.setattr(bp.os, "kill", lambda pid, sig: calls.append(sig))  # a process that never dies
    assert bp.kill_user_processes((1010, 1010), str(tmp_path), rounds=3, pause=0) == 3 and len(calls) == 3
    assert set(calls) == {bp.signal.SIGKILL}


# ---------------------------------------------------------------- L4: blocklist gaps
@pytest.mark.parametrize("url", [
    "http://[::127.0.0.1]/", "http://[::7f00:1]/", "http://[::10.0.0.1]/", "http://[fec0::1]/", "http://[feb0::1]/",
    "http://%31%32%37.0.0.1/", "http://%6c%6f%63%61%6c%68%6f%73%74/", "http://%73upervisor/", "http://127.0.0.1%2e/",
])
def test_remaining_blocklist_gaps_are_closed(url):
    with pytest.raises(BlockedURL):
        check_navigation_url(url)


@pytest.mark.parametrize("url", ["https://%65xample.com/", "https://example.com/%31%32%37", "https://[2606:4700:4700::1111]/"])
def test_encoded_but_public_addresses_still_work(url):
    assert check_navigation_url(url) == url


def test_host_problem_handles_bad_percent_sequences():
    assert host_problem("%ff%fe.example.com") is not None or host_problem("%ff%fe.example.com") is None  # must not raise
    assert host_problem("%31%32%37.0.0.1") is not None
    assert ip_is_blocked(__import__("ipaddress").ip_address("::127.0.0.1"))
