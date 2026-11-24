"""Embedded browser: a headless Chromium inside the add-on that the Media page shows as a live picture.

The browser is an opt-in option (``browser_enabled``, default off): nothing here starts unless it is on.
Chromium and Xvfb run as the unprivileged ``browser`` user with a clean environment (no Supervisor token),
all their traffic goes through a local filtering proxy that refuses private, local and Home Assistant
addresses, and the Chromium sandbox is required: without it the browser starts only with the explicit option
browser_allow_unsandboxed. Chromium is started on demand
and stopped after a while without a viewer. Downloads need a click or key press of the viewer, must be
archives, are saved into a private folder and handed to the "browser" inbox source only when complete;
that source is never auto-approved. The page's picture arrives as JPEG frames
(DevTools ``Page.startScreencast``); mouse and keyboard events of the viewer are sent back with
``Input.dispatch*``. Everything goes through one DevTools connection to Chromium on 127.0.0.1.
"""
from __future__ import annotations

import asyncio
import atexit
import base64
import json
import logging
import os
import platform
import re
import shutil
import signal
import stat
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web

from .browser_proc import (
    MAX_LIFETIME_SECONDS, build_browser_env, chromium_args, chromium_version_problem, is_wrapper_script, read_chromium_version, detect_sandbox_support, harden_data_dir, kill_user_processes,
    popen_drop_kwargs, ensure_sticky_dir, prepare_incoming, prepare_profile, read_small_file, unprivileged_user,
)
from .browser_proxy import FilteringProxy
from .config import ARCHIVE_EXTS
from .session_guard import SESSION_GUARD_TAG
from .netguard import BlockedURL, check_navigation_url, safe_download_name

log = logging.getLogger(__name__)

BROWSER_KEY = web.AppKey("car_media_hub.browser", object)

WIDTH, HEIGHT = 1280, 800  # the first size of the page (the viewer then sends its own)
WINDOW = (2560, 1600)  # the Chromium window: its content area (window minus toolbar) must be larger than any page size, or screenshots are cut
MAX_PAGE = (2560, 1440)  # the largest page size the viewer may ask for (inside WINDOW, with room for the toolbar)
ALLOWED_REDIRECTS = 3  # redirects (HTTP, script or refresh) allowed after a click or an opened address; the next one is refused. 0 = none, -1 = no limit here
NAV_WINDOW = 10.0  # seconds after a click, key press or opened address in which ONE page load is expected
MAX_REDIRECTS = 3  # redirects (HTTP or by script) after a click or an opened address; the next one is stopped
IDLE_SECONDS = 600  # stop Chromium this long after the last viewer left (and no download runs)
START_TIMEOUT = 25.0
DOWNLOAD_GESTURE_SECONDS = 5.0  # ONE download is allowed this long after a click or key press of the viewer (then the window is used up)
MAX_RUNNING_DOWNLOADS = 2
JPEG_QUALITY = 100  # while the page moves (the maximum; lower it, e.g. to 80, if the host is too slow)
SHARP_DELAY = 0.4
FRAME_GAP = 0.05  # seconds between two frames asked of Chromium (about 20 per second at most)
POPUP_GESTURE_SECONDS = 3.0  # a new tab / window opened this long after a click or key press of the viewer was asked for
POPUP_GRACE_SECONDS = 4.0  # an asked-for window may take this long to learn its address (or to start a download) before it is closed
REAL_CHROMIUM = "/usr/lib/chromium/chromium"  # Debian: the binary behind the /usr/bin/chromium wrapper script
BINARIES = ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable")
# runs in every page and frame: a new tab or window would be a second picture nobody sees
PAGE_SCRIPT = r"""(function(){
try{window.open=function(u){var ua=false;try{ua=!!(navigator.userActivation&&navigator.userActivation.isActive)}catch(e){}
if(u&&ua){location.href=u}return null}}catch(e){}
try{if(!window.chrome)window.chrome={runtime:{}}}catch(e){}
try{var cs=new CSSStyleSheet();cs.replaceSync('::-webkit-scrollbar{width:11px!important;height:11px!important;background:transparent!important}::-webkit-scrollbar-thumb{background:rgba(110,110,110,.65)!important;border-radius:6px!important}::-webkit-scrollbar-button{display:none!important;width:0!important;height:0!important}::-webkit-scrollbar-corner{background:transparent!important}');document.adoptedStyleSheets=[].concat(Array.from(document.adoptedStyleSheets),[cs])}catch(e){}
document.addEventListener('click',function(e){var t=e.target;
while(t&&t.tagName!=='A')t=t.parentNode;
if(t&&t.target&&t.target!=='_self'&&t.target!=='_top'&&t.target!=='_parent')t.target='_self'},true);
document.addEventListener('submit',function(e){var f=e.target;
if(f&&f.target&&f.target!=='_self'&&f.target!=='_top'&&f.target!=='_parent')f.target='_self'},true);
})();"""
_BUTTONS = {0: "left", 1: "middle", 2: "right"}
# the cursor of the element under the mouse: a link has "pointer", text fields "text" ...
CURSOR_JS = """function(x,y){try{var e=document.elementFromPoint(x,y);if(!e)return'';var c=getComputedStyle(e).cursor;
if(c==='auto'){var n=e.tagName;if(n==='INPUT'||n==='TEXTAREA'||e.isContentEditable)c='text';
else if(e.closest&&e.closest('a[href]'))c='pointer'}return c}catch(err){return''}}"""
CURSORS = {"pointer", "text", "crosshair", "move", "grab", "grabbing", "not-allowed", "wait", "progress", "help", "cell", "copy",
           "alias", "context-menu", "no-drop", "zoom-in", "zoom-out", "vertical-text", "ew-resize", "ns-resize", "nesw-resize",
           "nwse-resize", "col-resize", "row-resize", "e-resize", "w-resize", "n-resize", "s-resize"}


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) of a JPEG, read from its header without decoding it."""
    i, n = 2, len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            return int.from_bytes(data[i + 7:i + 9], "big"), int.from_bytes(data[i + 5:i + 7], "big")
        if marker == 0xFF:
            i += 1
        elif marker == 0x01 or 0xD0 <= marker <= 0xD9:
            i += 2
        else:
            i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    return None


def _png_size(data: bytes) -> tuple[int, int] | None:
    if len(data) > 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    return None


class BrowserError(Exception):
    pass


def _guard_request_url(url: str) -> None:
    """Raise BlockedURL for a page (or frame) address the browser must not load: only http(s) to public hosts;
    about:, blob: and data: documents cannot reach the network by themselves."""
    scheme = urlsplit(url).scheme.lower()
    if scheme in ("http", "https"):
        check_navigation_url(url)
    elif scheme not in ("about", "blob", "data"):
        raise BlockedURL("only http and https addresses can be opened")


class _Cdp:
    """A DevTools connection: commands with answers, and events passed to ``on_event``."""

    def __init__(self, ws: aiohttp.ClientWebSocketResponse, on_event: Any) -> None:
        self._ws = ws
        self._next = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._on_event = on_event
        self.closed = asyncio.Event()
        self._reader = asyncio.ensure_future(self._read())

    async def send(self, method: str, params: dict[str, Any] | None = None, session: str | None = None,
                   timeout: float = 20.0) -> dict[str, Any]:
        self._next += 1
        ident = self._next
        message: dict[str, Any] = {"id": ident, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        future = asyncio.get_running_loop().create_future()
        self._pending[ident] = future
        try:
            await self._ws.send_str(json.dumps(message))
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(ident, None)

    async def fire(self, method: str, params: dict[str, Any], session: str | None = None) -> None:
        """Send a command without waiting for its answer (mouse and key events: they must not queue up)."""
        self._next += 1
        message: dict[str, Any] = {"id": self._next, "method": method, "params": params}
        if session:
            message["sessionId"] = session
        await self._ws.send_str(json.dumps(message))

    async def _read(self) -> None:
        try:
            async for msg in self._ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = json.loads(msg.data)
                if "id" in data:
                    future = self._pending.get(data["id"])
                    if future and not future.done():
                        if "error" in data:
                            future.set_exception(BrowserError(data["error"].get("message", "DevTools error")))
                        else:
                            future.set_result(data.get("result", {}))
                elif "method" in data:
                    try:
                        self._on_event(data["method"], data.get("params", {}), data.get("sessionId"))
                    except Exception:  # noqa: BLE001 - one bad event must not stop the reader
                        log.exception("browser: handling %s failed", data.get("method"))
        except Exception:  # noqa: BLE001
            log.debug("browser: DevTools reader ended", exc_info=True)
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(BrowserError("the browser connection closed"))
            self.closed.set()

    async def close(self) -> None:
        self._reader.cancel()
        try:
            await self._ws.close()
        except Exception:  # noqa: BLE001
            pass


class _Viewer:
    """One open viewer page: the newest frame and the pending texts, pumped to its WebSocket."""

    def __init__(self) -> None:
        self.frame: bytes | None = None
        self.texts: list[str] = []
        self.event = asyncio.Event()

    def push_frame(self, data: bytes) -> None:
        self.frame = data  # a slow viewer skips frames instead of queueing them
        self.event.set()

    def push_text(self, text: str) -> None:
        self.texts.append(text)
        self.event.set()


def find_binary() -> str | None:
    override = os.environ.get("CAR_MEDIA_HUB_CHROMIUM")
    if override and os.path.exists(override):
        return override
    # Debian's /usr/bin/chromium is a shell script that sources files (/etc/chromium.d, and per-user init files below
    # HOME, which is the browser user's own profile). The real binary reads none of them.
    if os.access(REAL_CHROMIUM, os.X_OK):
        return REAL_CHROMIUM
    for name in BINARIES:
        path = shutil.which(name)
        if path:
            return path
    return None


class EmbeddedBrowser:
    def __init__(self, controller: Any) -> None:
        self.controller = controller
        self._lock = asyncio.Lock()
        self._proc: asyncio.subprocess.Process | None = None
        self._xvfb: asyncio.subprocess.Process | None = None
        self._http: aiohttp.ClientSession | None = None
        self._cdp: _Cdp | None = None
        self._session: str | None = None
        self._target: str | None = None
        self._viewers: set[_Viewer] = set()
        self._proxy: FilteringProxy | None = None
        self._ids: tuple[int, int] | None = None  # (uid, gid) of the unprivileged browser user
        self._sandbox: bool | None = None  # does the Chromium sandbox work here? (detected at the first start)
        self._started_at = 0.0
        self._gesture_until = 0.0  # downloads are allowed until then (monotonic): set only by the viewer's own input
        self._tasks: set[asyncio.Task] = set()
        self._latest: bytes | None = None
        self._requested: tuple[str, str, str, str] | None = None
        self._nonce = 0  # the time of the newest click on a feed item (ms)
        self._redirects = 0  # redirects since the last click, key press or opened address
        self._gate_redirects = 0  # redirects let through since the last click, key press or opened address
        self._nav_until = 0.0  # a page load is expected until this time (monotonic); the first one uses it up
        self._last_meta: tuple | None = None
        self._frames_ok: bool | None = None  # do the moving frames show exactly the page area (same size, no offset)?
        self._shot_busy = False
        self._have_frame = False  # the picture shown now is a moving frame that matched the page
        self._still_ok: bool | None = None
        self._cursor_xy = (0.0, 0.0)  # the newest mouse position: the cursor is asked for it, also after the mouse stopped
        self._cursor_timer: asyncio.TimerHandle | None = None
        self._cursor_again = False
        self._commits = 0  # pages committed since then (the first one is the page asked for, the others are redirects)
        self._main_frame = ""
        self._popups: dict[str, str] = {}  # new tabs / windows that were asked for: target id -> the address it reported so far
        self._popup_closed: set[str] = set()  # ids of new tabs already closed or handled (a late event about them is ignored)
        self._open_until = 0.0  # a new tab / window is wanted until then (monotonic): set only by the viewer's own click or key press
        self._size = (WIDTH, HEIGHT, 1.0)  # the viewport (CSS pixels, device pixel ratio) follows the viewer's window
        self._frame_seq = 0
        self._casting = False  # the screencast (frames of the page) runs only while a viewer is open
        self._offset_warned = False
        self._cursor = "default"  # the mouse cursor the page asks for, shown over the picture
        self._cursor_busy = False
        self._cursor_t = 0.0
        self._loading = False  # a new page was asked for and has not been committed yet: no frames of the old one are shown
        self._loading_timer: asyncio.TimerHandle | None = None
        self._cast_lock = asyncio.Lock()
        self._sharp_timer: asyncio.TimerHandle | None = None
        self._sharp_until = 0.0
        self._item: tuple[str, str] | None = None  # (rss source name, item id) the downloads belong to
        self._idle_since = time.time()
        self.url = ""
        self.title = ""
        self.downloads: dict[str, dict[str, Any]] = {}
        self._dl_names: dict[str, str] = {}  # guid -> safe file name of a download that was let through

    @property
    def cfg(self) -> Any:
        return self.controller.cfg

    # ---- helpers
    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(lambda t: t.cancelled() or t.exception() and log.debug("browser: %s", t.exception()))

    def _alive(self) -> bool:
        return bool(self._cdp and not self._cdp.closed.is_set() and self._proc and self._proc.returncode is None)

    def _broadcast(self, message: dict[str, Any]) -> None:
        text = json.dumps(message)
        for viewer in self._viewers:
            viewer.push_text(text)

    def _status(self) -> dict[str, Any]:
        return {"t": "state", "url": self.url, "title": self.title, "downloads": list(self.downloads.values())[-8:]}

    async def _cmd(self, method: str, params: dict[str, Any] | None = None, page: bool = True) -> dict[str, Any]:
        if not self._cdp:
            raise BrowserError("the browser is not running")
        return await self._cdp.send(method, params, self._session if page else None)

    def _user_action(self, gesture: bool = True) -> None:
        """A click, a key press or an address starts a new count: only what the site does by itself is a redirect.
        ``gesture`` opens the download window (one download); a reload or a feed item opened by the page is no gesture."""
        self._redirects = 0
        self._commits = 0
        if gesture:
            self._gesture_until = time.monotonic() + DOWNLOAD_GESTURE_SECONDS  # a person did something: one download may follow
        self._nav_until = time.monotonic() + NAV_WINDOW
        self._gate_redirects = 0

    def _note_redirect(self, url: str) -> None:
        self._redirects += 1
        log.info("browser: redirect %d to %s", self._redirects, url)
        if self._redirects > MAX_REDIRECTS:
            self._redirects = 0
            self._spawn(self._break_loop(url))

    async def _break_loop(self, url: str) -> None:
        log.warning("browser: more than %d redirects, stopped at %s", MAX_REDIRECTS, url)
        await self._cmd("Page.stopLoading")
        await self._cmd("Page.navigate", {"url": "about:blank"})  # ends the scripts that keep redirecting
        await asyncio.sleep(1.0)
        self._broadcast({"t": "notice", "message": f"Stopped after more than {MAX_REDIRECTS} redirects (at {url}). The site keeps "
                                                     "sending the browser on; it may need a cookie, a captcha or a step that this "
                                                     "browser cannot do. Open it in your own browser (the arrow next to the title)."})

    async def _gate(self, params: dict[str, Any]) -> None:
        """Fetch.requestPaused: let through the page that was asked for, refuse the ones the site sends the browser to."""
        request_id = params.get("requestId", "")
        main = not self._main_frame or params.get("frameId") == self._main_frame
        block = False
        why = ""
        try:
            _guard_request_url(params.get("request", {}).get("url", ""))
        except BlockedURL as exc:  # file:, ftp:, chrome: ... or a private / local / Home Assistant address
            block, why = True, str(exc)
        if not block and ALLOWED_REDIRECTS >= 0 and params.get("resourceType") == "Document" and main:
            counted = True
            if not params.get("redirectedRequestId") and time.monotonic() <= self._nav_until:
                self._nav_until = 0.0  # the page the click or the address asked for
                counted = False
            if counted:  # an HTTP redirect (301, 302, 307 ...), or a script or a refresh loading a page by itself
                self._gate_redirects += 1
                block = self._gate_redirects > ALLOWED_REDIRECTS
        try:
            if block:
                url = params.get("request", {}).get("url", "")
                log.info("browser: request blocked to %s (%s)", url, why or "too many redirects")
                await self._cmd("Fetch.failRequest", {"requestId": request_id, "errorReason": "BlockedByClient"})
                if why:
                    self._broadcast({"t": "notice", "message": f"Blocked (to {url}): {why}."})
                else:
                    self._broadcast({"t": "notice", "message": f"Redirect blocked (to {url}). Only {ALLOWED_REDIRECTS} redirect(s) per click are allowed in this browser."})
            else:
                await self._cmd("Fetch.continueRequest", {"requestId": request_id})
        except (BrowserError, asyncio.TimeoutError) as exc:
            log.debug("browser: gate %s failed: %s", request_id, exc)

    def _begin_loading(self) -> None:
        self._loading = True
        self._latest = None  # the picture of the previous page must not be shown for the new one
        self._have_frame = False
        self._broadcast({"t": "loading"})
        if self._loading_timer:
            self._loading_timer.cancel()
        self._loading_timer = asyncio.get_running_loop().call_later(15, self._end_loading)  # never stay loading for ever

    def _end_loading(self) -> None:
        if self._loading_timer:
            self._loading_timer.cancel()
            self._loading_timer = None
        if not self._loading:
            return
        self._loading = False
        self._broadcast({"t": "loaded"})
        if self._casting:
            self._frame_seq += 1
            self._spawn(self._sharp(self._frame_seq))

    async def _cursor_at(self, x: float, y: float) -> None:
        """Ask the page which cursor it shows at this point and pass it to the viewers."""
        if self._cursor_busy:
            self._cursor_again = True  # the mouse moved while the page was asked: ask again for the newest position
            return
        self._cursor_busy = True
        try:
            result = await self._cmd("Runtime.evaluate", {"expression": f"({CURSOR_JS})({x:.1f},{y:.1f})", "returnByValue": True})
            value = str(result.get("result", {}).get("value", "")).split(",")[-1].strip().lower()
            value = value if value in CURSORS else "default"
            if value != self._cursor:
                self._cursor = value
                self._broadcast({"t": "cursor", "c": value})
        except (BrowserError, asyncio.TimeoutError):
            pass
        finally:
            self._cursor_busy = False
            if self._cursor_again:
                self._cursor_again = False
                self._spawn(self._cursor_at(*self._cursor_xy))

    def _cursor_later(self, delay: float) -> None:
        """Ask for the cursor once more after the mouse stopped or the page scrolled under it."""
        if self._cursor_timer is None:
            self._cursor_timer = asyncio.get_running_loop().call_later(delay, self._cursor_trailing)

    def _cursor_trailing(self) -> None:
        self._cursor_timer = None
        self._cursor_t = time.monotonic()
        self._spawn(self._cursor_at(*self._cursor_xy))

    async def _fire(self, method: str, params: dict[str, Any]) -> None:
        if not self._cdp:
            raise BrowserError("the browser is not running")
        await self._cdp.fire(method, params, self._session)

    async def _sharp(self, seq: int) -> None:
        """The page stopped changing: replace the last (compressed) frame by one lossless picture (PNG)."""
        if seq != self._frame_seq or not self._viewers or not self._alive():
            return
        result = await self._cmd("Page.captureScreenshot", {"format": "png"})
        if seq != self._frame_seq:
            return
        self._sharp_until = time.monotonic() + 0.5
        data = base64.b64decode(result.get("data", ""))
        size = _png_size(data)
        width, height, ratio = self._size
        ok = size is None or (abs(size[0] - width * ratio) <= 3 and abs(size[1] - height * ratio) <= 3)
        if ok != self._still_ok:
            self._still_ok = ok
            log.info("browser: still picture (PNG) %s (PNG %s, page %sx%s)", "matches the page" if ok else
                     "DOES NOT match the page: the last moving frame stays", size, int(width * ratio), int(height * ratio))
        if not ok and self._have_frame:
            return  # a cut or stretched still would put the links somewhere else than the mouse; the moving frame is exact
        self._latest = data
        for viewer in self._viewers:
            viewer.push_frame(data)

    async def _shot(self) -> None:
        """A moving frame that does not match the page area is replaced by a screenshot of the page itself."""
        if self._shot_busy or self._loading or not self._viewers or not self._alive():
            return
        self._shot_busy = True
        try:
            result = await self._cmd("Page.captureScreenshot", {"format": "jpeg", "quality": 90})
            data = base64.b64decode(result.get("data", ""))
            if data and not self._loading:
                self._latest = data
                for viewer in self._viewers:
                    viewer.push_frame(data)
        except (BrowserError, asyncio.TimeoutError) as exc:
            log.debug("browser: screenshot failed: %s", exc)
        finally:
            self._shot_busy = False

    async def _ack(self, frame_session: int) -> None:
        await asyncio.sleep(FRAME_GAP)  # the pause is what limits the frame rate: Chromium sends the next frame after the ack
        await self._cmd("Page.screencastFrameAck", {"sessionId": frame_session})

    # ---- start / stop
    async def ensure_started(self) -> None:
        async with self._lock:
            if self._alive():
                return
            await self._stop_locked()
            try:
                try:
                    await self._start_locked()
                except BrowserError as exc:
                    if not (self._sandbox and str(exc).startswith("Chromium exited at start")
                            and getattr(self.cfg, "browser_allow_unsandboxed", False)):
                        raise  # without the explicit option the browser never runs unsandboxed
                    # the probe said yes but Chromium could not set its sandbox up: fall back (unprivileged user, clean env, proxy stay)
                    log.warning("browser: Chromium did not start with its sandbox (%s); retrying without it", exc)
                    self._sandbox = False
                    await self._stop_locked()
                    await self._start_locked()
            except BaseException:
                await self._stop_locked()
                raise

    @staticmethod
    def _prepare_folders(profile: Path, incoming_root: Path, ready: Path, ids: tuple[int, int] | None) -> Path:
        """Blocking. The profile belongs to the browser user and is prepared without following links it planted; the
        download folder lies outside it, below a root-owned parent; the folder of finished downloads belongs to root;
        the add-on's own files in /data are not readable by the browser user. Returns the download folder."""
        harden_data_dir(profile.parent)
        prepare_profile(profile, ids)
        incoming = prepare_incoming(incoming_root, ids)
        ready.mkdir(parents=True, exist_ok=True)
        os.chmod(ready, 0o700)
        return incoming

    def kill_now(self) -> None:
        """Synchronous last resort (shutdown, atexit): kill Chromium, Xvfb and every process of the browser user."""
        for process in (self._proc, self._xvfb):
            if process and process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
        try:
            kill_user_processes(self._ids if self._ids is not None else None)
        except Exception:  # noqa: BLE001
            pass

    async def _start_locked(self) -> None:
        if not getattr(self.cfg, "browser_enabled", False):
            raise BrowserError("the embedded browser is switched off (option browser_enabled)")
        binary = find_binary()
        if not binary:
            raise BrowserError("Chromium was not found in this add-on image, although the image build installs it (Debian package 'chromium', see the Documentation: Chromium). The image is probably an old or incomplete build: update or rebuild the add-on, and check the build log for the apt-get step")
        try:
            self._ids = unprivileged_user()
        except RuntimeError as exc:  # root and no 'browser' user: never run Chromium as root
            raise BrowserError(str(exc)) from None
        ids = self._ids
        problem = chromium_version_problem(await read_chromium_version(binary, ids))
        if problem:
            log.warning("browser: REFUSING TO START: %s", problem)
            raise BrowserError(problem)
        profile = Path(self.cfg.browser_profile_dir)
        ready = Path(self.cfg.browser_ready_dir)
        if self._sandbox is None:
            self._sandbox = await detect_sandbox_support(ids)
        if self._sandbox:
            log.info("browser: the Chromium sandbox is available and stays on")
        elif getattr(self.cfg, "browser_allow_unsandboxed", False):
            log.warning("browser: the Chromium sandbox does not work in this container and option browser_allow_unsandboxed is "
                        "ON: Chromium runs WITHOUT it. A compromised page then has the add-on's network access (Home "
                        "Assistant, other add-ons, your LAN); the filtering proxy does not contain it")
        else:
            raise BrowserError(
                "the Chromium sandbox cannot work in this container (no user namespaces), so the browser is not started. "
                "Only if you accept that a malicious page could then reach your network, set browser_allow_unsandboxed (see the Documentation)")
        await asyncio.to_thread(kill_user_processes, ids)  # leftovers of a run that was killed
        incoming = await asyncio.to_thread(self._prepare_folders, profile, Path(self.cfg.browser_incoming_root), ready, ids)
        for leftover in ("SingletonLock", "SingletonSocket", "SingletonCookie", "DevToolsActivePort"):
            try:
                (profile / leftover).unlink()
            except OSError:
                pass
        self._proxy = FilteringProxy()
        proxy_port = await self._proxy.start()
        headless = True
        display = None
        xvfb = shutil.which("Xvfb")
        if xvfb:  # a virtual screen: Chromium then runs as a normal (not headless) browser, which sites such as Cloudflare accept more often
            for leftover in ("/tmp/.X99-lock", "/tmp/.X11-unix/X99"):
                try:
                    os.unlink(leftover)
                except OSError:
                    pass
            try:
                ensure_sticky_dir("/tmp/.X11-unix")  # fd-based: never follows a link
            except OSError:
                pass
            self._xvfb = await asyncio.create_subprocess_exec(
                xvfb, ":99", "-screen", "0", "2560x1600x24", "-nolisten", "tcp", stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, env=build_browser_env(profile),
                cwd=str(profile), **popen_drop_kwargs(ids),
            )
            await asyncio.sleep(1.0)
            if self._xvfb.returncode is None:
                display = ":99"
                headless = False
            else:
                log.warning("browser: Xvfb exited (code %s); using headless Chromium", self._xvfb.returncode)
                self._xvfb = None
        env = build_browser_env(profile, display, wrapper=is_wrapper_script(binary))  # explicit minimal environment: no SUPERVISOR_TOKEN, no add-on secrets
        args = chromium_args(binary, profile, headless=headless, sandbox=bool(self._sandbox), proxy_port=proxy_port, window=WINDOW)
        log.info("browser: starting %s (%s, sandbox %s)", binary, "headless" if headless else "on a virtual screen",
                 "on" if self._sandbox else "OFF")
        self._started_at = time.monotonic()
        self._proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL, env=env, cwd=str(profile), **popen_drop_kwargs(ids),
        )
        atexit.register(self.kill_now)
        port_file = profile / "DevToolsActivePort"
        deadline = time.monotonic() + START_TIMEOUT
        endpoint = ""
        while time.monotonic() < deadline:
            if self._proc.returncode is not None:
                raise BrowserError(f"Chromium exited at start (code {self._proc.returncode})")
            text = await asyncio.to_thread(read_small_file, port_file)  # as root, from a browser-owned folder: no links
            lines = (text or "").split("\n")
            if len(lines) >= 2 and lines[0].strip().isdigit() and re.fullmatch(r"/devtools/browser/[0-9A-Za-z-]{8,64}", lines[1].strip()):
                endpoint = f"ws://127.0.0.1:{lines[0].strip()}{lines[1].strip()}"
                break
            await asyncio.sleep(0.2)
        if not endpoint:
            raise BrowserError("Chromium did not start in time")
        self._http = aiohttp.ClientSession()
        ws = await self._http.ws_connect(endpoint, max_msg_size=0, heartbeat=None)
        self._cdp = _Cdp(ws, self._on_event)
        cdp = self._cdp
        await cdp.send("Target.setDiscoverTargets", {"discover": True})
        targets = (await cdp.send("Target.getTargets")).get("targetInfos", [])
        page = next((t for t in targets if t.get("type") == "page"), None)
        if page is None:
            self._target = (await cdp.send("Target.createTarget", {"url": "about:blank"}))["targetId"]
        else:
            self._target = page["targetId"]
        self._session = (await cdp.send("Target.attachToTarget", {"targetId": self._target, "flatten": True}))["sessionId"]
        # allowAndName: the file is saved under its GUID in a private folder; the add-on names it and moves it (H1)
        await cdp.send("Browser.setDownloadBehavior", {"behavior": "allowAndName", "downloadPath": str(incoming), "eventsEnabled": True})
        await self._cmd("Page.enable")
        try:
            self._main_frame = (await self._cmd("Page.getFrameTree"))["frameTree"]["frame"]["id"]
        except (BrowserError, KeyError):
            self._main_frame = ""
        await self._cmd("Emulation.setDeviceMetricsOverride", self._metrics())
        version = await cdp.send("Browser.getVersion")
        log.info("browser: running %s", version.get("product", "an unknown Chromium"))
        agent = str(version.get("userAgent", "")).replace("HeadlessChrome", "Chrome")
        if agent:
            override: dict[str, Any] = {"userAgent": agent, "acceptLanguage": "en-US,en"}
            full = str(version.get("product", "")).split("/")[-1]
            major = full.split(".")[0]
            if major.isdigit():  # the client hints (Sec-CH-UA) name "HeadlessChrome" too; say plain Chrome there as well
                machine = platform.machine().lower()
                override["userAgentMetadata"] = {
                    "brands": [{"brand": "Chromium", "version": major}, {"brand": "Google Chrome", "version": major},
                               {"brand": "Not-A.Brand", "version": "99"}],
                    "fullVersionList": [{"brand": "Chromium", "version": full}, {"brand": "Google Chrome", "version": full},
                                        {"brand": "Not-A.Brand", "version": "99.0.0.0"}],
                    "platform": "Linux", "platformVersion": "6.1.0", "architecture": "arm" if "arm" in machine or "aarch" in machine else "x86",
                    "model": "", "mobile": False, "bitness": "64", "wow64": False,
                }
            await self._cmd("Emulation.setUserAgentOverride", override)
        await self._cmd("Page.addScriptToEvaluateOnNewDocument", {"source": PAGE_SCRIPT})
        await self._cmd("Network.enable")
        # every page and frame request is checked (scheme, host) before it is sent; sub-resources go through the proxy
        await self._cmd("Fetch.enable", {"patterns": [{"resourceType": "Document", "requestStage": "Request"}]})
        await self._cmd("Network.setCacheDisabled", {"cacheDisabled": True})  # a page is always fetched again
        try:
            await self._cmd("Network.setBypassServiceWorker", {"bypass": True})
        except BrowserError:
            pass
        try:
            await self._cmd("Emulation.setFocusEmulationEnabled", {"enabled": True})
        except BrowserError:
            pass  # an older Chromium; typing still works in most pages
        self._casting = False
        self._idle_since = time.time()
        self._spawn(self._watch())
        log.info("browser: ready, downloads are checked and handed to the 'browser' inbox %s", ready)

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_locked()

    async def _stop_locked(self) -> None:
        cdp, proc, http, xvfb, proxy = self._cdp, self._proc, self._http, self._xvfb, self._proxy
        self._cdp, self._proc, self._http, self._session, self._target, self._xvfb = None, None, None, None, None, None
        self._popups.clear()
        self._popup_closed.clear()
        self._open_until = 0.0
        self._proxy = None
        self._latest = None
        self._requested = None
        self._nonce = 0
        self._casting = False
        self._loading = False
        if self._loading_timer:
            self._loading_timer.cancel()
            self._loading_timer = None
        if cdp:
            await cdp.close()
        if http:
            await http.close()
        for process in (proc, xvfb):
            if process and process.returncode is None:
                log.info("browser: stopping")
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        os.killpg(process.pid, sig)
                    except (ProcessLookupError, PermissionError):
                        break
                    try:
                        await asyncio.wait_for(process.wait(), 3)
                        break
                    except asyncio.TimeoutError:
                        continue
        if proxy:
            await proxy.stop()
        # helpers that left their process group (or outlived Chromium) must not stay behind
        await asyncio.to_thread(kill_user_processes, self._ids)
        for viewer in self._viewers:
            viewer.push_text(json.dumps({"t": "closed"}))

    async def _watch(self) -> None:
        """Stop Chromium once nobody has looked at it for a while and no download runs."""
        while self._alive():
            await asyncio.sleep(30)
            if time.monotonic() - self._started_at > MAX_LIFETIME_SECONDS:
                log.info("browser: session limit reached, stopping (it starts again on the next use)")
                await self.stop()
                return
            if self._viewers or any(d["state"] == "running" for d in self.downloads.values()):
                self._idle_since = time.time()
            elif time.time() - self._idle_since > IDLE_SECONDS:
                log.info("browser: idle, stopping")
                await self.stop()
                return

    # ---- DevTools events (called from the reader: only schedule work, never wait for an answer)
    def _on_event(self, method: str, params: dict[str, Any], session: str | None) -> None:
        if method == "Page.screencastFrame":
            if session != self._session:
                return
            self._spawn(self._ack(params.get("sessionId", 0)))
            offset = params.get("metadata", {}).get("offsetTop", 0)
            if offset and not self._offset_warned:
                self._offset_warned = True
                log.warning("browser: the picture is shifted down by %s px (a bar above the page?); clicks will miss", offset)
            meta = params.get("metadata", {})
            key = (meta.get("deviceWidth"), meta.get("deviceHeight"), meta.get("offsetTop"), meta.get("pageScaleFactor"))
            if key != self._last_meta:  # logged only when it changes: a jump of the picture shows up here
                self._last_meta = key
                log.info("browser: screencast frame device %sx%s, offsetTop %s, scale %s", *key)
            if self._loading:
                return
            frame = base64.b64decode(params.get("data", ""))
            width, height, ratio = self._size
            size = _jpeg_size(frame)
            # the picture must be exactly the page area: a frame with an offset (a bar above the page) or another size would
            # be drawn shifted or stretched, so links would not be where the mouse is (hand over the wrong link, wrong click)
            ok = abs(offset or 0) < 1 and (size is None or (abs(size[0] - width * ratio) <= 3 and abs(size[1] - height * ratio) <= 3))
            if ok != self._frames_ok:
                self._frames_ok = ok
                log.info("browser: moving frames %s (frame %s, page %sx%s, offsetTop %s)", "match the page" if ok else
                         "DO NOT match the page: screenshots are shown while the page moves", size, int(width * ratio), int(height * ratio), offset)
            if ok:
                self._have_frame = True
                self._latest = frame
                for viewer in self._viewers:
                    viewer.push_frame(frame)
            else:
                self._spawn(self._shot())
            if time.monotonic() > self._sharp_until:  # a frame caused by the still picture itself does not ask for another
                self._frame_seq += 1
                if self._sharp_timer:
                    self._sharp_timer.cancel()
                seq = self._frame_seq
                self._sharp_timer = asyncio.get_running_loop().call_later(SHARP_DELAY, lambda: self._spawn(self._sharp(seq)))
        elif method == "Page.frameNavigated":
            frame = params.get("frame", {})
            if session == self._session and not frame.get("parentId"):
                log.info("browser: page loaded %s", frame.get("url", ""))
                self._main_frame = frame.get("id", self._main_frame)
                self._commits += 1
                if self._commits > 1:  # a page that was not asked for: sent on by a script or a refresh
                    self._note_redirect(frame.get("url", ""))
                self._end_loading()  # the new page is committed
        elif method == "Fetch.requestPaused":
            if session == self._session:
                self._spawn(self._gate(params))
        elif method == "Network.requestWillBeSent":
            if params.get("type") == "Document" and session == self._session and params.get("redirectResponse") \
                    and (not self._main_frame or params.get("frameId") == self._main_frame):
                self._note_redirect(params.get("request", {}).get("url", ""))  # an HTTP redirect (301, 302, 307 ...)
            if params.get("type") == "Document" and session == self._session and log.isEnabledFor(logging.DEBUG):
                headers = {k.lower(): v for k, v in params.get("request", {}).get("headers", {}).items()}
                keep = ("user-agent", "accept-language", "accept", "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform", "sec-fetch-site", "sec-fetch-mode", "upgrade-insecure-requests")
                log.debug("browser: request %s headers %s", params["request"].get("url", ""), {k: headers[k] for k in keep if k in headers})
        elif method == "Target.targetInfoChanged":
            info = params.get("targetInfo", {})
            if info.get("targetId") == self._target:
                self.url, self.title = info.get("url", ""), info.get("title", "")
                self._broadcast(self._status())
            else:
                self._popup(info)
        elif method == "Target.targetCreated":
            self._popup(params.get("targetInfo", {}))
        elif method == "Page.javascriptDialogOpening":
            self._spawn(self._cmd("Page.handleJavaScriptDialog", {"accept": True}))
        elif method == "Browser.downloadWillBegin":
            self._download_begins(params)
        elif method == "Browser.downloadProgress":
            item = self.downloads.get(params.get("guid", ""))
            if item is None or item["state"] not in ("running", "blocked"):
                return
            guid = item["id"]
            item["received"], item["total"] = int(params.get("receivedBytes", 0)), max(0, int(params.get("totalBytes", 0)))
            now = time.time()
            if now - item["t"] >= 1.0:  # smoothed speed in bytes per second, for the estimated time left
                instant = max(0.0, (item["received"] - item["r"]) / (now - item["t"]))
                item["speed"] = instant if not item["speed"] else 0.7 * item["speed"] + 0.3 * instant
                item["t"], item["r"] = now, item["received"]
            state = params.get("state", "inProgress")
            limit = int(self.cfg.max_release_size_mb) * 1024 * 1024
            if state == "completed" and item["state"] == "running":
                item["state"] = "moving"
                self._spawn(self._finish_download(guid))
            elif state == "canceled":
                if item["state"] == "running":
                    item["state"] = "canceled"
                item["ended"] = time.time()
            elif item["state"] == "running" and (item["received"] > limit or item["total"] > limit):
                item["note"] = f"larger than max_release_size_mb ({self.cfg.max_release_size_mb} MB)"
                self._spawn(self._cancel_quietly(guid))
            self._broadcast(self._status())
        elif method in ("Target.targetDestroyed", "Target.detachedFromTarget"):
            self._popups.pop(str(params.get("targetId", "")), None)
            if params.get("targetId") == self._target or params.get("sessionId") == self._session:
                log.warning("browser: the page was closed")

    def _popup(self, info: dict[str, Any]) -> None:
        """A new tab or window (target=_blank that the page script did not catch, window.open, an ad). There is only
        ONE tab and it is the one that is viewed: a new page that the viewer did not just ask for by a click or key
        press is closed at once; one that was asked for is loaded in the viewed tab (its address, once it is known)
        and then closed. Only the first event of a target decides; later ones just report its address."""
        target = str(info.get("targetId") or "")
        if info.get("type") != "page" or not target or target == self._target or self._target is None:
            return
        if target in self._popup_closed:
            return
        if target in self._popups:  # an address change of a window that was asked for
            self._popups[target] = str(info.get("url", ""))
            return
        opener = str(info.get("openerId") or "")
        wanted = time.monotonic() <= self._open_until and (not opener or opener == self._target)
        if not wanted:
            log.info("browser: a new tab (%s) that nobody asked for was closed", str(info.get("url", ""))[:120])
            self._close_popup(target)
            return
        self._popups[target] = str(info.get("url", ""))
        self._spawn(self._take_popup(target))

    def _close_popup(self, target: str) -> None:
        self._popup_closed.add(target)
        self._popups.pop(target, None)
        if len(self._popup_closed) > 200:  # the set of old ids must not grow for ever
            self._popup_closed = set(list(self._popup_closed)[-100:])
        if self._cdp:
            self._spawn(self._close_target(target))

    async def _close_target(self, target: str) -> None:
        cdp = self._cdp
        if cdp is None:
            return
        try:
            await cdp.send("Target.closeTarget", {"targetId": target})
        except Exception:  # noqa: BLE001  (the tab is gone already, or the connection is)
            pass

    async def _take_popup(self, target: str) -> None:
        """Wait for the address of a new tab that the viewer asked for, open it in the viewed tab, close the new tab."""
        deadline = time.monotonic() + POPUP_GRACE_SECONDS
        url = ""
        while time.monotonic() < deadline and target in self._popups:
            candidate = self._popups.get(target, "")
            if candidate and not candidate.startswith("about:blank"):
                url = candidate
                break
            await asyncio.sleep(0.1)
        if url:
            try:
                check_navigation_url(url)
            except BlockedURL:
                log.info("browser: a new tab asked for by the viewer pointed at a refused address, closed")
            else:
                log.info("browser: a new tab was opened in the viewed tab: %s", url[:200])
                try:
                    await self._cmd("Page.navigate", {"url": url})
                except (BrowserError, asyncio.TimeoutError) as exc:
                    log.warning("browser: could not open the new tab's address: %s", exc)
        elif target in self._popups:  # no address: perhaps a download in that tab; let it begin, then close
            await asyncio.sleep(max(0.0, deadline - time.monotonic()))
        if target not in self._popup_closed:
            self._close_popup(target)

    def _download_begins(self, params: dict[str, Any]) -> None:
        """A download starts. It continues only when the viewer's user did something a moment ago (click, key press,
        opened address), it is an archive and not too many run: a page cannot push a file in by itself (H1)."""
        guid = str(params.get("guid", ""))
        suggested = str(params.get("suggestedFilename") or "download")
        name = safe_download_name(suggested, ARCHIVE_EXTS)
        running = sum(1 for d in self.downloads.values() if d["state"] == "running")
        reason = ""
        frame = str(params.get("frameId") or "")
        if time.monotonic() > self._gesture_until:
            reason = "blocked: it started without a click or key press in this browser"
        elif frame and self._main_frame and frame != self._main_frame and frame not in self._popups:  # (an older Chromium sends no frameId: the click alone counts)
            reason = "blocked: it was started by an embedded frame, not by the page itself"
        elif name is None:
            reason = "not an archive: only zip, rar, 7z and tar files are taken"
        elif running >= MAX_RUNNING_DOWNLOADS:
            reason = "too many downloads at once"
        self.downloads[guid] = {
            "id": guid, "name": name or suggested[:80], "state": "blocked" if reason else "running", "received": 0, "total": 0,
            "speed": 0.0, "t": time.time(), "r": 0, "note": reason,
        }
        if reason:
            self.downloads[guid]["ended"] = time.time()
            log.warning("browser: download %s refused (%s) from %s", suggested[:80], reason, str(params.get("url", ""))[:200])
            self._spawn(self._cancel_quietly(guid))
        else:
            self._gesture_until = 0.0  # one download per click: the next one needs the next click
            self._dl_names[guid] = name or ""
            log.info("browser: download %s started (%s)", name, params.get("url", ""))
        if len(self.downloads) > 50:  # the list of old rows must not grow for ever
            for old in list(self.downloads)[:-30]:
                self.downloads.pop(old, None)
        self._broadcast(self._status())

    async def _cancel_quietly(self, guid: str) -> None:
        try:
            await self._cmd("Browser.cancelDownload", {"guid": guid}, page=False)
        except (BrowserError, asyncio.TimeoutError) as exc:
            log.debug("browser: cancelling %s failed: %s", guid, exc)

    def _hand_over(self, guid: str, name: str) -> Path:
        """Blocking: copy the finished file from the browser user's private folder into the "browser" inbox as a root-owned
        file (opened without following links: the browser user owns the source folder and could swap the file for a link),
        under a temporary name, then rename it. The source is deleted."""
        if not re.fullmatch(r"[0-9A-Za-z-]{8,64}", guid) or not name:
            raise ValueError("bad download id")
        source = Path(self.cfg.browser_incoming_dir) / guid
        ready = Path(self.cfg.browser_ready_dir)
        limit = int(self.cfg.max_release_size_mb) * 1024 * 1024
        fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("the downloaded file is not a regular file")
            if info.st_nlink != 1:
                raise ValueError("the downloaded file has several hard links")
            if self._ids is not None and info.st_uid != self._ids[0]:
                raise ValueError("the downloaded file does not belong to the browser user")
            if info.st_size > limit:
                raise ValueError(f"larger than max_release_size_mb ({self.cfg.max_release_size_mb} MB)")
            ready.mkdir(parents=True, exist_ok=True)
            ext = next((x for x in sorted(ARCHIVE_EXTS, key=len, reverse=True) if name.lower().endswith(x)), "")
            stem = name[: len(name) - len(ext)]
            temp = ready / f".{guid}.part"  # a dot name is ignored by the inbox until the rename
            out = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(out, "wb") as dst, os.fdopen(fd, "rb", closefd=False) as src:
                    copied = 0
                    while chunk := src.read(1024 * 1024):
                        copied += len(chunk)
                        if copied > limit:
                            raise ValueError("the downloaded file grew beyond max_release_size_mb")
                        dst.write(chunk)
                final, n = ready / name, 1
                while True:  # os.link fails when the name is taken: never overwrites the file of a download that ended at the same time
                    try:
                        os.link(temp, final)
                        break
                    except FileExistsError:
                        n += 1
                        final = ready / f"{stem} ({n}){ext}"
                os.unlink(temp)
            except BaseException:
                try:
                    os.unlink(temp)
                except OSError:
                    pass
                raise
        finally:
            os.close(fd)
        try:
            os.unlink(source)
        except OSError:
            pass
        return final

    async def _finish_download(self, guid: str) -> None:
        item = self.downloads.get(guid)
        name = self._dl_names.pop(guid, "")
        if item is None:
            return
        try:
            dest = await asyncio.to_thread(self._hand_over, guid, name)
        except (OSError, ValueError) as exc:
            item["state"], item["note"] = "failed", str(exc) or type(exc).__name__
            log.warning("browser: download %s could not be handed over: %s", name, exc)
        else:
            item["state"], item["note"] = "done", ""
            log.info("browser: download %s finished (%d bytes), waiting in %s for your review", name, item["received"], dest.parent)
            self._item_done()
        item["ended"] = time.time()
        self._broadcast(self._status())

    def _item_done(self) -> None:
        """A file of the opened feed item arrived: the item is done."""
        if not self._item:
            return
        name, item_id = self._item
        source = next((s for s in self.controller.sources if getattr(s, "name", "") == name and hasattr(s, "mark_handled")), None)
        if source:
            source.mark_handled(item_id)

    # ---- actions of the viewer
    def _metrics(self) -> dict[str, Any]:
        width, height, ratio = self._size
        return {"width": width, "height": height, "deviceScaleFactor": ratio, "mobile": False}

    def _screencast(self) -> dict[str, Any]:
        width, height, ratio = self._size
        return {"format": "jpeg", "quality": JPEG_QUALITY, "maxWidth": int(width * ratio), "maxHeight": int(height * ratio), "everyNthFrame": 1}

    async def resize(self, width: int, height: int, ratio: float = 1.0) -> None:
        """Make the page as large as the viewer's window, at the viewer's pixel density (nothing is cut off or scaled)."""
        size = (max(320, min(MAX_PAGE[0], width)), max(240, min(MAX_PAGE[1], height)), max(1.0, min(2.0, ratio)))
        if size == self._size:
            return
        self._size = size
        async with self._cast_lock:
            if self._casting:
                await self._cmd("Page.stopScreencast")
            await self._cmd("Emulation.setDeviceMetricsOverride", self._metrics())
            if self._casting:
                await self._cmd("Page.startScreencast", self._screencast())

    async def _cast(self, on: bool) -> None:
        """Frames are rendered and encoded only while somebody looks: a hidden page leaves the CPU to the downloads."""
        async with self._cast_lock:
            if not self._alive() or on == self._casting:
                return
            try:
                if on:
                    await self._cmd("Page.startScreencast", self._screencast())
                    self._casting = True
                    self._frame_seq += 1
                    self._spawn(self._sharp(self._frame_seq))  # a page that does not change would show nothing otherwise
                else:
                    await self._cmd("Page.stopScreencast")
                    self._casting = False
                    if self._sharp_timer:
                        self._sharp_timer.cancel()
            except (BrowserError, asyncio.TimeoutError) as exc:
                log.debug("browser: screencast %s failed: %s", "start" if on else "stop", exc)

    async def navigate(self, url: str, gesture: bool = True) -> None:
        url = url.strip()
        if "://" not in url:
            url = "https://" + url
        try:
            check_navigation_url(url)  # http(s) only, no private / local / internal host
        except BlockedURL as exc:
            raise BrowserError(f"{exc}") from None
        log.info("browser: opening %s", url)
        self._user_action(gesture)
        try:
            await self._cmd("Page.stopLoading")  # whatever the previous page was still loading must not win
            await self._cmd("Network.clearBrowserCache")
        except (BrowserError, asyncio.TimeoutError):
            pass
        result = await self._cmd("Page.navigate", {"url": url})
        if result.get("errorText"):
            raise BrowserError(f"could not open {url}: {result['errorText']}")

    async def open_item(self, source: str, item_id: str, url: str, nonce: str = "") -> bool:
        """The viewer page was opened for a feed item; go to its page. Only a reconnect of the same viewer page (same
        ``nonce``, made at the click) keeps the page as it is, so a new click always shows the item's own page.
        Returns whether a new page is being loaded."""
        wanted = (source, item_id, url, nonce)
        if self._requested == wanted:
            return False
        if nonce.isdigit() and self._nonce and int(nonce) < self._nonce:
            log.info("browser: ignored a request that is older than the last click (%s)", url)
            return False  # an old viewer page reconnecting after a newer item was opened
        if nonce.isdigit():
            self._nonce = int(nonce)
        self._requested = wanted
        self._item = (source, item_id) if source and item_id else None
        if not url:
            return False
        self._begin_loading()
        try:
            await self.navigate(url, gesture=False)  # the page of a feed item is not a click on a download
        except BaseException:
            self._end_loading()
            raise
        return True

    async def handle(self, message: dict[str, Any]) -> None:
        kind = message.get("t")
        if (kind == "mouse" and message.get("k") == "down") or (kind == "key" and message.get("k") == "down"):
            self._user_action()
            self._open_until = time.monotonic() + POPUP_GESTURE_SECONDS
        elif kind == "reload":
            self._user_action(gesture=False)
        if kind == "mouse":
            action = {"move": "mouseMoved", "down": "mousePressed", "up": "mouseReleased"}.get(message.get("k"))
            if not action:
                return
            if action == "mouseMoved":
                self._cursor_xy = (float(message.get("x", 0)), float(message.get("y", 0)))
                now = time.monotonic()
                if now - self._cursor_t > 0.08:
                    self._cursor_t = now
                    self._spawn(self._cursor_at(*self._cursor_xy))
                else:
                    self._cursor_later(0.1)  # the last move of a series would otherwise never be asked about
            held = int(message.get("m", 0))
            if action == "mouseMoved":  # moving with a button held is a drag (scrollbar, text selection)
                button = "left" if held & 1 else "right" if held & 2 else "middle" if held & 4 else "none"
            else:
                button = _BUTTONS.get(int(message.get("b", 0)), "left")
            await self._fire("Input.dispatchMouseEvent", {
                "type": action, "x": float(message.get("x", 0)), "y": float(message.get("y", 0)), "button": button,
                "buttons": int(message.get("m", 0)), "clickCount": int(message.get("c", 1)) if action != "mouseMoved" else 0,
                "modifiers": int(message.get("mod", 0)),
            })
        elif kind == "wheel":
            self._cursor_xy = (float(message.get("x", 0)), float(message.get("y", 0)))
            self._cursor_later(0.2)  # what is under the mouse changes when the page scrolls
            await self._fire("Input.dispatchMouseEvent", {
                "type": "mouseWheel", "x": float(message.get("x", 0)), "y": float(message.get("y", 0)),
                "deltaX": float(message.get("dx", 0)), "deltaY": float(message.get("dy", 0)),
            })
        elif kind == "key":
            params: dict[str, Any] = {
                "modifiers": int(message.get("mod", 0)), "key": str(message.get("key", "")), "code": str(message.get("code", "")),
                "windowsVirtualKeyCode": int(message.get("vk", 0)), "nativeVirtualKeyCode": int(message.get("vk", 0)),
            }
            if message.get("k") == "up":
                params["type"] = "keyUp"
            else:
                text = str(message.get("text", ""))
                params["type"] = "keyDown" if text else "rawKeyDown"
                if text:
                    params["text"] = params["unmodifiedText"] = text
                if message.get("cmd") == "selectAll":
                    params["commands"] = ["selectAll"]
            await self._fire("Input.dispatchKeyEvent", params)
        elif kind == "text":
            await self._fire("Input.insertText", {"text": str(message.get("text", ""))[:20000]})
        elif kind == "resize":
            await self.resize(int(message.get("w", WIDTH)), int(message.get("h", HEIGHT)), float(message.get("dpr", 1)))
        elif kind == "nav":
            await self.navigate(str(message.get("url", "")))
        elif kind in ("back", "forward"):  # the history of the embedded browser; the host's Back button never touches it
            history = await self._cmd("Page.getNavigationHistory")
            index = int(history.get("currentIndex", 0)) + (-1 if kind == "back" else 1)
            entries = history.get("entries", [])
            if 0 <= index < len(entries):
                await self._cmd("Page.navigateToHistoryEntry", {"entryId": entries[index]["id"]})
        elif kind == "stop":
            await self._cmd("Page.stopLoading")
            self._end_loading()
        elif kind == "reload":
            await self._cmd("Page.reload")

    async def cancel_download(self, guid: str) -> bool:
        """Stop a running download (Chromium removes the unfinished file)."""
        item = self.downloads.get(guid)
        if not item or item["state"] != "running" or not self._alive():
            return False
        await self._cmd("Browser.cancelDownload", {"guid": guid}, page=False)
        return True

    def recent_downloads(self) -> list[dict[str, Any]]:
        """Running downloads and those that ended in the last ten minutes (for the Media page)."""
        now = time.time()
        rows = []
        for d in self.downloads.values():
            if d["state"] != "running" and now - d.get("ended", now) >= 600:
                continue
            row = dict(d)
            if d["state"] == "running":
                row["stalled"] = now - d["t"] > 15
                if d["speed"] > 0 and d["total"] > d["received"] and not row["stalled"]:
                    row["eta"] = int((d["total"] - d["received"]) / d["speed"])
            rows.append(row)
        return rows[-8:]

    # ---- the viewer WebSocket
    async def serve(self, request: web.Request) -> web.StreamResponse:
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=1_000_000)
        await ws.prepare(request)
        viewer = _Viewer()
        try:
            await self.ensure_started()
        except (BrowserError, OSError, asyncio.TimeoutError, aiohttp.ClientError) as exc:
            log.warning("browser: %s", exc)
            await ws.send_str(json.dumps({"t": "error", "message": str(exc) or type(exc).__name__}))
            await ws.close()
            return ws
        self._viewers.add(viewer)
        self._idle_since = time.time()
        viewer.push_text(json.dumps(self._status()))
        q = request.query
        try:
            await self.open_item(q.get("src", ""), q.get("item", ""), q.get("url", ""), q.get("n", ""))
        except (BrowserError, asyncio.TimeoutError) as exc:
            log.warning("browser: %s", exc)
            viewer.push_text(json.dumps({"t": "notice", "message": str(exc) or type(exc).__name__}))
        if self._loading:
            viewer.push_text(json.dumps({"t": "loading"}))
        else:
            viewer.push_text(json.dumps({"t": "loaded"}))
            if self._latest:
                viewer.push_frame(self._latest)
        await self._cast(True)
        pump = asyncio.ensure_future(_pump(ws, viewer))
        try:
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    await self.handle(json.loads(msg.data))
                except (BrowserError, ValueError, TypeError, asyncio.TimeoutError) as exc:
                    viewer.push_text(json.dumps({"t": "notice", "message": str(exc) or type(exc).__name__}))
        finally:
            pump.cancel()
            self._viewers.discard(viewer)
            self._idle_since = time.time()
            if not self._viewers:
                self._spawn(self._cast(False))
        return ws


async def _pump(ws: web.WebSocketResponse, viewer: _Viewer) -> None:
    try:
        while True:
            await viewer.event.wait()
            viewer.event.clear()
            texts, viewer.texts = viewer.texts, []
            for text in texts:
                await ws.send_str(text)
            if viewer.frame is not None:
                frame, viewer.frame = viewer.frame, None
                await ws.send_bytes(frame)
    except (ConnectionError, RuntimeError, asyncio.CancelledError):
        return


_PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Browser</title>
<style>
:root{color-scheme:light dark}
html,body{height:100%;overscroll-behavior:none} /* a swipe inside the page must not be taken as the browser's Back */
body{position:fixed;inset:0;margin:0;font:14px system-ui,sans-serif;background:#1b1b1b;color:#eee;overflow:hidden}
#bar{position:absolute;left:0;right:0;top:0;height:42px;box-sizing:border-box;display:flex;flex-wrap:nowrap;gap:6px;align-items:center;padding:0 8px;background:#202124;border-bottom:2px solid #2980b9;overflow:hidden;white-space:nowrap}
#bar a,#bar button{color:#8cf;background:none;border:1px solid #3a3a3a;border-radius:4px;padding:4px 9px;font:inherit;cursor:pointer;text-decoration:none}
#bar button:disabled{opacity:.4}
#url{flex:1 1 auto;min-width:60px;background:#111;color:#eee;border:1px solid #3a3a3a;border-radius:4px;padding:5px 8px;font:inherit}
#stage{position:absolute;left:0;right:0;top:42px;bottom:0;overflow:hidden;background:#fff;overscroll-behavior:none}
#screen{position:absolute;left:0;top:0;display:block;background:#fff;outline:none;cursor:default;touch-action:none;overscroll-behavior:none}
#kbd{position:fixed;left:-1000px;top:0;width:10px;height:10px;opacity:0}
#loading{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:14px;background:#fff;color:#222;z-index:3}
#loading[hidden]{display:none}
.spin{width:38px;height:38px;border:4px solid #ddd;border-top-color:#111;border-radius:50%;animation:spin 0.9s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
#over{position:absolute;left:0;right:0;bottom:0;z-index:4}
#msg{padding:8px 12px;background:rgba(32,33,36,.94);color:#ffb4a8}
#msg[hidden],#dl:empty{display:none}
#dl{max-height:20vh;overflow:auto;padding:4px 10px;font-size:13px;background:rgba(32,33,36,.92)}
#dl::-webkit-scrollbar{width:8px}#dl::-webkit-scrollbar-button{display:none}#dl::-webkit-scrollbar-thumb{background:#666;border-radius:4px}
#dl div{padding:2px 0}.warn{color:#ffcc80}.ok{color:#a5d6a7}
</style>@@GUARD@@</head><body>
<div id="bar">
<a href="./" id="back">&#9664; Media</a>
<button id="reload" title="Reload">&#8635;</button><button id="stop" title="Stop loading">&#10005;</button>
<input id="url" placeholder="https://" autocomplete="off" spellcheck="false">
<button id="go">Go</button>
</div>
<div id="stage"><canvas id="screen" width="1280" height="800" tabindex="0"></canvas><div id="loading"><div class="spin"></div><div>Loading&hellip;</div></div></div>
<textarea id="kbd" autocomplete="off" autocapitalize="off"></textarea>
<div id="over"><div id="msg" hidden></div><div id="dl"></div></div>
<script>
(function(){
var W = 1280, H = 800, DPR = 1;
var stage = document.getElementById('stage');
var q = new URLSearchParams(location.search);
var canvas = document.getElementById('screen'), ctx = canvas.getContext('2d');
var kbd = document.getElementById('kbd'), url = document.getElementById('url');
var loadingBox = document.getElementById('loading'), waitFrame = false;
function hideLoading(){ loadingBox.hidden = true; waitFrame = false; }
var msg = document.getElementById('msg'), dl = document.getElementById('dl');
var ws = null, closed = false, retryMs = 2000;
function show(text){ msg.textContent = text; msg.hidden = !text; }
function send(o){ if (ws && ws.readyState === 1) ws.send(JSON.stringify(o)); }
function measure(){ /* the page fills the free area of the window at the screen's pixel density: sharp, nothing cut off */
  var sw = Math.max(320, Math.floor(stage.clientWidth)), sh = Math.max(240, Math.floor(stage.clientHeight));
  var d = Math.min(2, window.devicePixelRatio || 1);
  if (Math.abs(sw - W) < 2 && Math.abs(sh - H) < 2 && d === DPR && canvas.width === Math.round(W * d)) return false;
  W = sw; H = sh; DPR = d; canvas.width = Math.round(sw * d); canvas.height = Math.round(sh * d);
  canvas.style.width = sw + 'px'; canvas.style.height = sh + 'px';
  return true;
}
/* "Media" steps back in the history when the Media page is where we came from: a plain link would add one more entry
   (Media, Viewer, Media) and Home Assistant's Back button would then return to this viewer instead of the HA page before */
document.getElementById('back').addEventListener('click', function(e){
  var cameFromReview = false;
  try { cameFromReview = history.length > 1 && !!document.referrer && new URL(document.referrer).origin === location.origin; } catch (err) {}
  if (cameFromReview){ e.preventDefault(); history.back(); }
  else { e.preventDefault(); location.replace('./'); }
});
function announce(){ send({t: 'resize', w: W, h: H, dpr: DPR}); }
var resizeTimer = null;
function onResize(){ if (measure()){ clearTimeout(resizeTimer); resizeTimer = setTimeout(announce, 150); } }
if (window.ResizeObserver) new ResizeObserver(onResize).observe(stage); else window.addEventListener('resize', onResize);
function connect(){
  var u = new URL('viewer/ws' + location.search, location.href);
  u.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(u.href);
  ws.binaryType = 'blob';
  ws.onopen = function(){ retryMs = 2000; measure(); announce(); };
  ws.onmessage = function(e){
    if (typeof e.data !== 'string'){
      draw(e.data);
      return;
    }
    var m = JSON.parse(e.data);
    if (m.t === 'cursor'){ canvas.style.cursor = m.c; }
    else if (m.t === 'loading'){ loadingBox.hidden = false; waitFrame = false; }
    else if (m.t === 'loaded'){ waitFrame = true; setTimeout(hideLoading, 2500); }
    else if (m.t === 'state'){
      if (document.activeElement !== url) url.value = m.url || '';
      document.title = m.title || 'Browser';
      renderDownloads(m.downloads || []); show('');
    } else if (m.t === 'error'){
      closed = true; loadingBox.hidden = true; show('Could not start the browser: ' + m.message + (q.get('url') ? '  Open the page in your own browser instead: ' + q.get('url') : ''));
    } else if (m.t === 'notice'){ show(m.message); }
    else if (m.t === 'closed'){ closed = true; show('The browser was closed. Open an item again from the Media page.'); }
  };
  /* a close can follow a 401 of Home Assistant's ingress layer (the handshake is refused, the page only sees code 1006):
     ask once with a plain request; after a 401 the banner is shown and no new connection is tried */
  ws.onclose = function(){
    if (closed || window.cmhSession.dead) return;
    window.cmhSession.probe('api/browser/downloads').then(function(ok){
      if (!ok || closed) return;
      setTimeout(connect, retryMs); retryMs = Math.min(15000, retryMs * 2);
    });
  };
}
var decoding = false, queued = null;
function draw(blob){ /* one frame at a time: a frame that arrives while one is decoded replaces the one waiting */
  if (decoding){ queued = blob; return; }
  decoding = true;
  createImageBitmap(blob).then(function(b){ ctx.drawImage(b, 0, 0, canvas.width, canvas.height); b.close(); if (waitFrame) hideLoading(); }).catch(function(){}).then(function(){
    decoding = false;
    if (queued){ var next = queued; queued = null; draw(next); }
  });
}
function renderDownloads(list){
  dl.textContent = '';
  list.forEach(function(d){
    var row = document.createElement('div');
    var pct = d.total ? ' ' + Math.round(100 * d.received / d.total) + '%' : '';
    row.textContent = d.name + ' - ' + (d.state === 'running' ? 'downloading' + pct : d.state === 'done' ? 'saved: waiting for your review' : d.state) + (d.note ? ' (' + d.note + ')' : '');
    row.className = d.note || d.state === 'blocked' ? 'warn' : (d.state === 'done' ? 'ok' : '');
    dl.appendChild(row);
  });
}
function pos(e){
  var r = canvas.getBoundingClientRect();
  return {x: Math.max(0, Math.min(W - 1, (e.clientX - r.left) * W / r.width)), y: Math.max(0, Math.min(H - 1, (e.clientY - r.top) * H / r.height))};
}
function mods(e){ return (e.altKey ? 1 : 0) | (e.ctrlKey ? 2 : 0) | (e.metaKey ? 4 : 0) | (e.shiftKey ? 8 : 0); }
var last = 0;
var dragging = false;
canvas.addEventListener('mouseup', function(e){
  if (e.button === 3 || e.button === 4){ e.preventDefault(); send({t: e.button === 3 ? 'back' : 'forward'}); }
});
canvas.addEventListener('auxclick', function(e){ e.preventDefault(); });
function moveMsg(e){ var p = pos(e); send({t: 'mouse', k: 'move', x: p.x, y: p.y, m: e.buttons, mod: mods(e)}); }
canvas.addEventListener('mousemove', function(e){
  if (dragging) return;
  var now = Date.now(); if (now - last < 50) return; last = now; moveMsg(e);
});
canvas.addEventListener('mousedown', function(e){
  e.preventDefault();
  if (e.button > 2) return; /* the side buttons act on mouse up */
  kbd.focus(); dragging = true; var p = pos(e);
  send({t: 'mouse', k: 'down', x: p.x, y: p.y, b: e.button, m: e.buttons, c: e.detail || 1, mod: mods(e)});
});
/* while a button is held the whole window follows the mouse, so a scrollbar or a selection can be dragged past the edge */
window.addEventListener('mousemove', function(e){
  if (!dragging) return;
  var now = Date.now(); if (now - last < 16) return; last = now; moveMsg(e);
});
window.addEventListener('mouseup', function(e){
  if (!dragging) return; dragging = false; var p = pos(e);
  send({t: 'mouse', k: 'up', x: p.x, y: p.y, b: e.button, m: e.buttons, c: e.detail || 1, mod: mods(e)});
});
canvas.addEventListener('contextmenu', function(e){ e.preventDefault(); });
var tx = 0, ty = 0;
canvas.addEventListener('touchstart', function(e){ var t = e.touches[0]; tx = t.clientX; ty = t.clientY; }, {passive: true});
canvas.addEventListener('touchmove', function(e){ /* a finger drag scrolls the page */
  e.preventDefault(); var t = e.touches[0], p = pos(t);
  send({t: 'wheel', x: p.x, y: p.y, dx: tx - t.clientX, dy: ty - t.clientY}); tx = t.clientX; ty = t.clientY;
}, {passive: false});
var reverse = /Mac/.test(navigator.platform); /* macOS scrolls the other way here */
var wheel = null;
canvas.addEventListener('wheel', function(e){ /* wheel ticks are summed for 30 ms and sent as one */
  e.preventDefault(); var p = pos(e);
  if (!wheel){
    wheel = {t: 'wheel', x: p.x, y: p.y, dx: 0, dy: 0};
    setTimeout(function(){ send(wheel); wheel = null; }, 30);
  }
  wheel.x = p.x; wheel.y = p.y; wheel.dx += reverse ? -e.deltaX : e.deltaX; wheel.dy += reverse ? -e.deltaY : e.deltaY;
}, {passive: false});
function keyMsg(e, k){
  var text = '';
  if (k === 'down'){
    if (e.key === 'Enter') text = '\r';
    else if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) text = e.key;
  }
  var m = {t: 'key', k: k, key: e.key, code: e.code, vk: e.keyCode, text: text, mod: mods(e)};
  if (k === 'down' && (e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'a') m.cmd = 'selectAll';
  return m;
}
kbd.addEventListener('keydown', function(e){
  /* Alt+Left / Alt+Right (Cmd+[ / Cmd+] on a Mac) move in the history of the embedded browser, not of Home Assistant */
  if ((e.altKey && (e.key === 'ArrowLeft' || e.key === 'ArrowRight')) || (e.metaKey && (e.key === '[' || e.key === ']'))){
    e.preventDefault(); send({t: (e.key === 'ArrowLeft' || e.key === '[') ? 'back' : 'forward'}); return;
  }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'v') return; /* the paste event carries the text */
  e.preventDefault(); send(keyMsg(e, 'down'));
});
kbd.addEventListener('keyup', function(e){
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'v') return;
  e.preventDefault(); send(keyMsg(e, 'up'));
});
kbd.addEventListener('paste', function(e){
  e.preventDefault(); var t = (e.clipboardData || window.clipboardData).getData('text'); if (t) send({t: 'text', text: t});
});
canvas.addEventListener('focus', function(){ kbd.focus(); });
document.getElementById('reload').onclick = function(){ send({t: 'reload'}); };
document.getElementById('stop').onclick = function(){ send({t: 'stop'}); hideLoading(); };
document.getElementById('go').onclick = function(){ if (url.value.trim()) send({t: 'nav', url: url.value}); };
url.addEventListener('keydown', function(e){ if (e.key === 'Enter') document.getElementById('go').click(); });
connect();
})();
</script></body></html>
"""


_PAGE = _PAGE.replace("@@GUARD@@", SESSION_GUARD_TAG)


def register_browser_pages(app: web.Application, controller: Any) -> EmbeddedBrowser | None:
    """Register the viewer routes. With ``browser_enabled`` off (the default) only an empty downloads list is
    served, the viewer, its WebSocket and everything that could start Chromium do not exist."""

    async def no_downloads(request: web.Request) -> web.Response:
        return web.json_response({"running": False, "downloads": []})

    if not getattr(controller.cfg, "browser_enabled", False):
        app.router.add_get("/api/browser/downloads", no_downloads)
        return None
    browser = EmbeddedBrowser(controller)
    app[BROWSER_KEY] = browser

    async def page(request: web.Request) -> web.Response:
        return web.Response(text=_PAGE, content_type="text/html", headers={"Cache-Control": "no-store"})

    async def socket(request: web.Request) -> web.StreamResponse:
        return await browser.serve(request)

    async def downloads(request: web.Request) -> web.Response:
        return web.json_response({"running": browser._alive(), "downloads": browser.recent_downloads()})

    async def cancel(request: web.Request) -> web.Response:
        try:
            guid = str((await request.json())["id"])
        except (ValueError, KeyError, TypeError):
            return web.json_response({"error": "id is required"}, status=400)
        try:
            stopped = await browser.cancel_download(guid)
        except (BrowserError, asyncio.TimeoutError) as exc:
            return web.json_response({"error": str(exc) or type(exc).__name__}, status=502)
        return web.json_response({"ok": stopped})

    async def cleanup(_: web.Application) -> None:
        await browser.stop()

    app.router.add_get("/viewer", page)
    app.router.add_get("/viewer/ws", socket)
    app.router.add_get("/api/browser/downloads", downloads)
    app.router.add_post("/api/browser/downloads/cancel", cancel)
    app.on_cleanup.append(cleanup)
    return browser
