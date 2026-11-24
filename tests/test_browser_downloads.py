"""H1: downloads of the embedded browser need a user gesture, must be archives and never reach the library."""
import asyncio
import os
import time
from types import SimpleNamespace

import pytest

from car_media_hub import browser as br
from car_media_hub.config import Config


def make(tmp_path, enabled=True, **kw):
    cfg = Config(inbox_dir=tmp_path / "inbox", staging_dir=tmp_path / "st", library_dir=tmp_path / "lib",
                 data_dir=tmp_path / "data", browser_enabled=enabled, **kw)
    cfg.browser_incoming_dir.mkdir(parents=True)
    cfg.browser_ready_dir.mkdir(parents=True)
    browser = br.EmbeddedBrowser(SimpleNamespace(cfg=cfg, sources=[]))
    calls = []

    async def cmd(method, params=None, page=True):
        calls.append((method, params))
        return {}

    browser._cmd = cmd
    return browser, cfg, calls


async def settle():
    await asyncio.sleep(0.05)


def begin(browser, name="Album.zip", guid="11111111-2222-3333-4444-555555555555"):
    browser._on_event("Browser.downloadWillBegin", {"guid": guid, "suggestedFilename": name, "url": "https://x.example.com/f"}, None)
    return guid


async def test_download_without_a_gesture_is_cancelled(tmp_path):
    browser, _, calls = make(tmp_path)
    guid = begin(browser)  # no click, key press or opened address: a page pushed it
    await settle()
    assert browser.downloads[guid]["state"] == "blocked"
    assert ("Browser.cancelDownload", {"guid": guid}) in calls
    assert guid not in browser._dl_names


async def test_download_after_a_gesture_runs_and_old_gestures_expire(tmp_path):
    browser, _, calls = make(tmp_path)
    browser._user_action()  # a click / key press / address of the viewer
    guid = begin(browser)
    await settle()
    assert browser.downloads[guid]["state"] == "running" and not calls
    browser._gesture_until = time.monotonic() - 1  # the gesture is old
    late = begin(browser, guid="aaaaaaaa-2222-3333-4444-555555555555")
    await settle()
    assert browser.downloads[late]["state"] == "blocked"


async def test_page_script_cannot_create_a_gesture(tmp_path):
    browser, _, _ = make(tmp_path)
    # only the viewer's own messages call _user_action (mouse down, key down, reload, nav): everything a page does
    # (events from DevTools) leaves the window closed
    before = browser._gesture_until
    browser._on_event("Page.frameNavigated", {"frame": {"id": "f", "url": "https://x.example.com/"}}, None)
    browser._on_event("Browser.downloadProgress", {"guid": "none", "state": "inProgress"}, None)
    assert browser._gesture_until == before


async def test_mouse_move_is_no_gesture_but_click_is(tmp_path):
    browser, _, _ = make(tmp_path)

    async def fire(*a, **k):
        return None

    browser._fire = fire
    browser._cursor_at = fire
    await browser.handle({"t": "mouse", "k": "move", "x": 1, "y": 1})
    assert browser._gesture_until == 0.0
    await browser.handle({"t": "mouse", "k": "down", "x": 1, "y": 1, "b": 0})
    assert browser._gesture_until > time.monotonic()


@pytest.mark.parametrize("name", ["payload.exe", "page.html", "song.mp3", "x.zip.exe", ""])
async def test_non_archives_are_cancelled_even_after_a_click(tmp_path, name):
    browser, _, calls = make(tmp_path)
    browser._user_action()
    guid = begin(browser, name)
    await settle()
    assert browser.downloads[guid]["state"] == "blocked"
    assert any(c[0] == "Browser.cancelDownload" for c in calls)


async def test_too_many_parallel_downloads_are_cancelled(tmp_path):
    browser, _, _ = make(tmp_path)
    browser._user_action()
    ids = []
    for i in range(br.MAX_RUNNING_DOWNLOADS + 1):
        browser._user_action()  # every download needs its own click
        ids.append(begin(browser, f"a{i}.zip", guid=f"{i:08d}-2222-3333-4444-555555555555"))
    await settle()
    assert [browser.downloads[i]["state"] for i in ids].count("blocked") == 1


async def test_completed_download_is_copied_into_the_browser_inbox_only(tmp_path):
    browser, cfg, _ = make(tmp_path)
    browser._user_action()
    guid = begin(browser, "../../Evil Album.zip")
    (cfg.browser_incoming_dir / guid).write_bytes(b"PK-data")
    browser._on_event("Browser.downloadProgress", {"guid": guid, "state": "completed", "receivedBytes": 7, "totalBytes": 7}, None)
    await asyncio.sleep(0.3)
    assert browser.downloads[guid]["state"] == "done"
    assert [p.name for p in cfg.browser_ready_dir.iterdir()] == ["Evil Album.zip"]
    assert (cfg.browser_ready_dir / "Evil Album.zip").read_bytes() == b"PK-data"
    assert not (cfg.browser_incoming_dir / guid).exists()
    assert not cfg.inbox_dir.exists()  # never the shared inbox


async def test_name_collision_gets_a_number(tmp_path):
    browser, cfg, _ = make(tmp_path)
    (cfg.browser_ready_dir / "A.zip").write_bytes(b"old")
    (cfg.browser_incoming_dir / "11111111-1").write_bytes(b"new")
    assert browser._hand_over("11111111-1", "A.zip").name == "A (2).zip"
    assert (cfg.browser_ready_dir / "A.zip").read_bytes() == b"old"


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="needs O_NOFOLLOW")
async def test_a_symlink_swapped_in_by_the_browser_user_is_not_followed(tmp_path):
    browser, cfg, _ = make(tmp_path)
    secret = tmp_path / "options.json"
    secret.write_text("{secret}")
    os.symlink(secret, cfg.browser_incoming_dir / "11111111-1")
    with pytest.raises(OSError):
        browser._hand_over("11111111-1", "A.zip")
    assert not any(cfg.browser_ready_dir.iterdir())


async def test_oversized_and_bad_ids_are_refused(tmp_path):
    browser, cfg, _ = make(tmp_path, max_release_size_mb=1)
    (cfg.browser_incoming_dir / "11111111-1").write_bytes(b"x" * (1024 * 1024 + 1))
    with pytest.raises(ValueError):
        browser._hand_over("11111111-1", "A.zip")
    with pytest.raises(ValueError):
        browser._hand_over("../../etc/passwd", "A.zip")
    assert not any(cfg.browser_ready_dir.iterdir())


async def test_download_over_the_size_limit_is_cancelled_while_running(tmp_path):
    browser, _, calls = make(tmp_path, max_release_size_mb=1)
    browser._user_action()
    guid = begin(browser)
    browser._on_event("Browser.downloadProgress", {"guid": guid, "state": "inProgress", "receivedBytes": 2 * 1024 * 1024,
                                                    "totalBytes": 0}, None)
    await settle()
    assert ("Browser.cancelDownload", {"guid": guid}) in calls


async def test_scheme_and_internal_hosts_are_blocked_for_pages_and_frames(tmp_path):
    browser, _, calls = make(tmp_path)
    for url in ("file:///etc/passwd", "ftp://example.com/", "chrome://settings", "http://127.0.0.1:8099/",
                "http://172.30.32.2/", "http://supervisor/", "https://nas.local/"):
        calls.clear()
        await browser._gate({"requestId": "r1", "frameId": "f", "resourceType": "Document", "request": {"url": url}})
        assert calls[0][0] == "Fetch.failRequest", url
    calls.clear()
    await browser._gate({"requestId": "r2", "frameId": "f", "resourceType": "Document", "request": {"url": "https://example.com/"}})
    assert calls[0][0] == "Fetch.continueRequest"


async def test_navigate_refuses_internal_addresses(tmp_path):
    browser, _, _ = make(tmp_path)
    for url in ("javascript:alert(1)", "file:///etc/passwd", "http://127.0.0.1:8100/", "http://169.254.169.254/", "http://homeassistant:8123"):
        with pytest.raises(br.BrowserError):
            await browser.navigate(url)
    await browser.navigate("example.com/page")  # becomes https://example.com/page
