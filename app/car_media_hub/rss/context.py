"""Fetching and link helpers of the RSS source: page fetching with timeouts and a watchdog, link
extraction and file selection. Everything here is synchronous (urllib): the RSS source runs it in a
worker thread."""
from __future__ import annotations

import http.client
import logging
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from html import unescape
from typing import Any, Callable, NamedTuple
from urllib.parse import unquote, urljoin, urlparse

log = logging.getLogger(__name__)

# Same string as ``sources.fetch.USER_AGENT`` (not imported: that would be a circular import).
USER_AGENT = "CarMediaHub/0.0.1 (Home Assistant add-on)"
FILE_EXTS = (".zip", ".rar", ".7z", ".mp3", ".flac", ".wav", ".ogg", ".m4a")
HTML_TYPES = ("text/html", "application/xhtml+xml")
MAX_PAGE_BYTES = 5 * 1024 * 1024
FOLLOW_DELAY = 1.0  # seconds to wait after each fetched item page (be polite to the server)
FETCH_TIMEOUT = 30.0  # seconds per socket operation of a request
FETCH_TOTAL_TIMEOUT = 90.0  # seconds for one whole page fetch, headers and body (a trickling server must not stall the add-on)


class FetchedPage(NamedTuple):
    """The result of fetching a URL: final URL (after redirects), content type and text body.

    ``body`` is empty for content types that are clearly not text (audio, archives, ...).
    """

    url: str
    content_type: str
    body: str


Fetcher = Callable[[str], FetchedPage]


def is_http_url(url: str) -> bool:
    """True for an http(s) URL with a host."""
    try:
        parsed = urlparse(url)
        return parsed.scheme.lower() in ("http", "https") and bool(parsed.hostname)
    except ValueError:
        return False


def is_file_url(url: str) -> bool:
    """True when the URL path ends in one of :data:`FILE_EXTS`."""
    return unquote(urlparse(url).path).lower().endswith(FILE_EXTS)


def extract_links(html: str, base: str, attrs: tuple[str, ...] = ("href",)) -> list[str]:
    """Absolute, de-duplicated values of the given attributes found in ``html``."""
    pattern = r"""\b(?:%s)\s*=\s*["']([^"']+)["']""" % "|".join(attrs)
    seen: set[str] = set()
    out: list[str] = []
    for raw in re.findall(pattern, html, re.I):
        try:
            url = urljoin(base, unescape(raw.strip()))
        except ValueError:  # e.g. "Invalid IPv6 URL": drop only this href
            log.debug("ignoring malformed link %r on %s", raw, base)
            continue
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


# ---------------------------------------------------------------- fetching
class _HttpRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow redirects to http(s) targets only (RSS-7)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        if urlparse(newurl).scheme.lower() not in ("http", "https"):
            raise urllib.error.HTTPError(newurl, code, "redirect to a non-http(s) URL refused", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _Watchdog:
    """Cuts the connections of one page fetch at its deadline.

    The timeout of ``http.client`` applies to every single socket operation, and its header
    parser accepts 100 header lines of 64 KB: a server that sends one byte every 29 s would keep
    ``open()`` blocked for days, long before the body (and its deadline) is reached. At the deadline
    the sockets are shut down, which wakes the reading thread with an error (``expired`` says why).
    """

    def __init__(self, seconds: float) -> None:
        self.expired = False
        self._lock = threading.Lock()
        self._socks: list[socket.socket] = []
        self._timer = threading.Timer(max(0.0, seconds), self._fire)
        self._timer.daemon = True
        self._timer.start()

    def track(self, sock: socket.socket) -> None:
        with self._lock:
            if not self.expired:
                self._socks.append(sock)
                return
        self._cut(sock)

    def _fire(self) -> None:
        with self._lock:
            self.expired = True
            socks = list(self._socks)
        for sock in socks:
            self._cut(sock)

    @staticmethod
    def _cut(sock: socket.socket) -> None:
        try:
            socket.socket.shutdown(sock, socket.SHUT_RDWR)  # not SSLSocket.shutdown: it drops the TLS state
        except (OSError, ValueError):
            pass

    def cancel(self) -> None:
        self._timer.cancel()


def _watched(base: type, dog: _Watchdog) -> type:
    class _WatchedConnection(base):  # type: ignore[misc, valid-type]
        def connect(self) -> None:
            super().connect()
            dog.track(self.sock)

    return _WatchedConnection


class _WatchedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, dog: _Watchdog) -> None:
        super().__init__()
        self._dog = dog

    def http_open(self, req):  # type: ignore[no-untyped-def]
        return self.do_open(_watched(http.client.HTTPConnection, self._dog), req)


class _WatchedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, dog: _Watchdog) -> None:
        super().__init__()
        self._dog = dog

    def https_open(self, req):  # type: ignore[no-untyped-def]
        return self.do_open(
            _watched(http.client.HTTPSConnection, self._dog), req, context=getattr(self, "_context", None)
        )


def build_opener(watchdog: _Watchdog | None = None) -> urllib.request.OpenerDirector:
    """An opener that speaks http and https only: no file:, ftp: or data: handlers, no proxies.

    With a ``watchdog`` every connection it opens is cut when the watchdog expires.
    """
    opener = urllib.request.OpenerDirector()
    for handler in (
        _WatchedHTTPHandler(watchdog) if watchdog else urllib.request.HTTPHandler(),
        _WatchedHTTPSHandler(watchdog) if watchdog else urllib.request.HTTPSHandler(),
        urllib.request.HTTPDefaultErrorHandler(),
        _HttpRedirectHandler(),
        urllib.request.HTTPErrorProcessor(),
        urllib.request.UnknownHandler(),
    ):
        opener.add_handler(handler)
    return opener


def _is_textual(content_type: str) -> bool:
    return (
        content_type in HTML_TYPES
        or content_type.startswith("text/")
        or content_type.endswith(("json", "xml", "javascript"))
    )


def _decode(raw: bytes, charset: str | None) -> str:
    try:
        return raw.decode(charset or "utf-8", "replace")
    except LookupError:
        return raw.decode("utf-8", "replace")


def _read_limited(resp: Any, max_bytes: int, deadline: float) -> bytes:
    """Read up to ``max_bytes`` in chunks, raising ``TimeoutError`` once ``deadline`` (monotonic) passed.

    ``timeout`` of urlopen limits every single socket operation only, so a server that sends one
    byte every few seconds would block a plain ``read`` for as long as it likes.
    """
    chunks: list[bytes] = []
    total = 0
    while total < max_bytes:
        if time.monotonic() > deadline:
            raise TimeoutError("the server is too slow: page not received in time")
        chunk = resp.read1(min(65536, max_bytes - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def fetch_page(
    url: str,
    timeout: float = FETCH_TIMEOUT,
    max_bytes: int = MAX_PAGE_BYTES,
    *,
    any_type: bool = False,
    total_timeout: float = FETCH_TOTAL_TIMEOUT,
) -> FetchedPage:
    """GET ``url`` (http/https only, also after redirects) and read at most ``max_bytes`` of text.

    The body is read for text-like content types only (``body`` is empty otherwise), unless
    ``any_type`` is set. ``timeout`` applies to each socket
    operation, ``total_timeout`` to the whole fetch: connecting, redirects, headers and body
    (``TimeoutError``; a watchdog cuts the connection, since the headers are read inside urllib).
    Raises ``ValueError`` for another scheme and the urllib errors for network / HTTP failures.
    """
    if not is_http_url(url):
        raise ValueError(f"only http(s) URLs are allowed, got {url!r}")
    deadline = time.monotonic() + total_timeout
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    dog = _Watchdog(total_timeout)
    try:
        with build_opener(dog).open(request, timeout=timeout) as resp:
            content_type = resp.headers.get_content_type()
            body = ""
            if any_type or _is_textual(content_type):
                body = _decode(_read_limited(resp, max_bytes, deadline), resp.headers.get_content_charset())
            return FetchedPage(resp.geturl(), content_type, body)
    except TimeoutError:
        raise
    except Exception as exc:
        if dog.expired:  # the watchdog cut the connection: say what happened, not the socket error
            raise TimeoutError("the server is too slow: page not received in time") from exc
        raise
    finally:
        dog.cancel()


# ---------------------------------------------------------------- file selection
def matches_format(url: str, formats: list[str] | tuple[str, ...]) -> bool:
    """True when no formats are set, or the file name (without extension) ends with one of them
    (``Album - MP3.zip``) or the extension is one (``track.mp3``), ignoring case."""
    if not formats:
        return True
    name = os.path.basename(unquote(urlparse(url).path))
    stem, ext = os.path.splitext(name)
    stem, ext = stem.upper(), ext[1:].upper()
    return any(stem.endswith(f.upper()) or (ext and ext == f.upper().lstrip(".")) for f in formats)
