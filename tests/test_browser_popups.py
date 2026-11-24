"""New tabs / windows: only one tab is viewed. Unwanted ones are closed, a click that opens one loads it in the viewed tab."""
import asyncio
import time
from types import SimpleNamespace

from car_media_hub import browser as br
from car_media_hub.config import Config

MAIN = "MAIN-TARGET"


def make(tmp_path):
    cfg = Config(inbox_dir=tmp_path / "inbox", staging_dir=tmp_path / "st", library_dir=tmp_path / "lib",
                 data_dir=tmp_path / "data", browser_enabled=True)
    cfg.browser_incoming_dir.mkdir(parents=True)
    cfg.browser_ready_dir.mkdir(parents=True)
    browser = br.EmbeddedBrowser(SimpleNamespace(cfg=cfg, sources=[]))
    calls = []

    class Cdp:
        async def send(self, method, params=None, session=None, timeout=20.0):
            calls.append((method, params))
            return {}

        async def fire(self, method, params, session=None):
            calls.append((method, params))

    async def cmd(method, params=None, page=True):
        calls.append((method, params))
        return {}

    browser._cdp = Cdp()
    browser._cmd = cmd
    browser._target = MAIN
    browser._main_frame = MAIN
    return browser, calls


def created(browser, target="POP1", url="", opener=MAIN, kind="page"):
    browser._on_event("Target.targetCreated", {"targetInfo": {"targetId": target, "type": kind, "url": url, "openerId": opener}}, None)


def changed(browser, target, url):
    browser._on_event("Target.targetInfoChanged", {"targetInfo": {"targetId": target, "type": "page", "url": url, "openerId": MAIN}}, None)


async def settle(seconds=0.05):
    await asyncio.sleep(seconds)


def closed(calls):
    return [p["targetId"] for m, p in calls if m == "Target.closeTarget"]


def navigated(calls):
    return [p["url"] for m, p in calls if m == "Page.navigate"]


async def test_popup_without_a_click_is_closed_at_once(tmp_path):
    browser, calls = make(tmp_path)
    created(browser, url="https://ads.example.com/x")
    await settle()
    assert closed(calls) == ["POP1"] and not navigated(calls)  # the viewed tab is not sent to the ad


async def test_popup_after_an_old_click_is_closed(tmp_path):
    browser, calls = make(tmp_path)
    await browser.handle({"t": "key", "k": "down", "key": "a"})
    browser._open_until = time.monotonic() - 1
    calls.clear()
    created(browser, url="https://ads.example.com/x")
    await settle()
    assert closed(calls) == ["POP1"] and not navigated(calls)


async def test_new_tab_after_a_click_loads_in_the_viewed_tab_and_is_closed(tmp_path):
    browser, calls = make(tmp_path)
    browser._open_until = time.monotonic() + 3  # the viewer just clicked
    created(browser, url="")  # Chromium reports the address a moment later
    await settle()
    assert not closed(calls)
    changed(browser, "POP1", "https://files.example.com/album")
    await settle(0.25)
    assert navigated(calls) == ["https://files.example.com/album"]
    assert closed(calls) == ["POP1"]


async def test_new_tab_with_known_address_after_a_click(tmp_path):
    browser, calls = make(tmp_path)
    browser._open_until = time.monotonic() + 3
    created(browser, url="https://files.example.com/a")
    await settle(0.25)
    assert navigated(calls) == ["https://files.example.com/a"] and closed(calls) == ["POP1"]


async def test_wanted_new_tab_to_a_private_address_is_closed_not_opened(tmp_path):
    browser, calls = make(tmp_path)
    browser._open_until = time.monotonic() + 3
    created(browser, url="http://192.168.1.1/admin")
    await settle(0.25)
    assert not navigated(calls) and closed(calls) == ["POP1"]


async def test_tab_opened_by_another_tab_is_closed_even_after_a_click(tmp_path):
    browser, calls = make(tmp_path)
    browser._open_until = time.monotonic() + 3
    created(browser, target="POP2", url="https://x.example.com/", opener="OTHER-TAB")
    await settle()
    assert closed(calls) == ["POP2"] and not navigated(calls)


async def test_wanted_tab_without_an_address_is_closed_after_the_grace_time(tmp_path, monkeypatch):
    monkeypatch.setattr(br, "POPUP_GRACE_SECONDS", 0.2)
    browser, calls = make(tmp_path)
    browser._open_until = time.monotonic() + 3
    created(browser, url="about:blank")
    await settle(0.1)
    assert not closed(calls)
    await settle(0.4)
    assert closed(calls) == ["POP1"] and not navigated(calls)


async def test_main_tab_and_non_page_targets_are_left_alone(tmp_path):
    browser, calls = make(tmp_path)
    created(browser, target=MAIN, opener="")
    created(browser, target="SW1", kind="service_worker")
    created(browser, target="IF1", kind="iframe")
    await settle()
    assert not calls


async def test_targets_seen_before_the_main_tab_is_known_are_ignored(tmp_path):
    browser, calls = make(tmp_path)
    browser._target = None  # start-up: the first tab is being created
    created(browser, target="FIRST", opener="")
    await settle()
    assert not calls


async def test_late_events_about_a_closed_tab_do_nothing(tmp_path):
    browser, calls = make(tmp_path)
    created(browser, url="https://ads.example.com/x")
    await settle()
    calls.clear()
    changed(browser, "POP1", "https://ads.example.com/y")
    created(browser, url="https://ads.example.com/y")
    await settle()
    assert not calls


def test_page_script_keeps_links_and_forms_in_the_viewed_tab():
    # target=_blank links and forms become same-tab ones; window.open only follows a real click (user activation)
    assert "t.target='_self'" in br.PAGE_SCRIPT and "f.target='_self'" in br.PAGE_SCRIPT
    assert "userActivation.isActive" in br.PAGE_SCRIPT


async def test_download_from_a_wanted_new_tab_is_not_blocked_as_a_frame(tmp_path):
    browser, calls = make(tmp_path)
    browser._open_until = time.monotonic() + 3
    browser._gesture_until = time.monotonic() + 3
    created(browser, url="about:blank")
    browser._on_event("Browser.downloadWillBegin", {"guid": "g1", "suggestedFilename": "Album.zip",
                                                    "url": "https://x.example.com/a.zip", "frameId": "POP1"}, None)
    assert browser.downloads["g1"]["state"] == "running"


async def test_download_from_an_unknown_frame_is_still_blocked(tmp_path):
    browser, calls = make(tmp_path)
    browser._gesture_until = time.monotonic() + 3
    browser._on_event("Browser.downloadWillBegin", {"guid": "g2", "suggestedFilename": "Album.zip",
                                                    "url": "https://x.example.com/a.zip", "frameId": "EVIL"}, None)
    assert browser.downloads["g2"]["state"] == "blocked"


async def test_key_down_opens_the_window_reload_does_not(tmp_path):
    browser, _ = make(tmp_path)
    await browser.handle({"t": "reload"})
    assert browser._open_until == 0.0
    await browser.handle({"t": "key", "k": "down", "key": "a"})
    assert browser._open_until > time.monotonic()
