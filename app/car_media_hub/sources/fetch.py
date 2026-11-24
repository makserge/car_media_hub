"""Reusable helpers for sources that fetch releases over HTTP."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from pathlib import Path, PurePosixPath
from typing import Any, Awaitable, Callable, Protocol, TypeVar
from urllib.parse import unquote, urlparse

import aiohttp

from ..config import ARCHIVE_EXTS, AUDIO_EXTS
from ..diskspace import NoSpaceError, ensure_free, human_bytes, is_no_space_error
from ..names import dedupe_name, name_key, safe_component

log = logging.getLogger(__name__)

USER_AGENT = "CarMediaHub/0.0.1 (Home Assistant add-on)"
MAX_FAILURES = 3
RETRY_AFTER = 24 * 3600  # seconds before a release that failed MAX_FAILURES times is tried again
PROGRESS_INTERVAL = 0.5  # seconds between progress callbacks
MAX_TEXT_BYTES = 10 * 1024 * 1024
BOM = b"\xef\xbb\xbf"
_XML_DECL_RE = re.compile(rb"""^\s*<\?xml[^>]*?\sencoding\s*=\s*["']([A-Za-z0-9._:\-]+)["']""")
# JSON API calls are small: bound the whole request. Downloads can take far longer than a
# minute, so they only get connect/idle timeouts (no total limit).
API_TIMEOUT = aiohttp.ClientTimeout(total=60)
DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=None, connect=60, sock_connect=60, sock_read=60)
_WRITE_BUFFER = 1 << 20  # flush downloaded data to disk (in a worker thread) per MiB
SUPPORTED_ALGOS = ("sha1", "md5", "sha256")
_HEX_LEN_TO_ALGO = {32: "md5", 40: "sha1", 64: "sha256"}
_HTML_TYPES = ("text/html", "application/xhtml+xml")
_CONTENT_TYPE_EXT = {
    "audio/mpeg": ".mp3",
    "audio/flac": ".flac",
    "audio/x-flac": ".flac",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "application/zip": ".zip",
    "application/x-zip-compressed": ".zip",
    "application/vnd.rar": ".rar",
    "application/x-rar-compressed": ".rar",
    "application/x-7z-compressed": ".7z",
}

T = TypeVar("T")


class PermanentError(Exception):
    """A non-retryable failure (HTTP 4xx, checksum mismatch, size violation)."""


class DownloadCancelled(Exception):
    """The user stopped the download (deliberately not an ``OSError``: it must not be retried)."""


def check_url(url: str) -> None:
    """Raise :class:`PermanentError` unless ``url`` is an http(s) URL with a host."""
    try:
        parsed = urlparse(url)
        ok = parsed.scheme.lower() in ("http", "https") and bool(parsed.hostname)
    except ValueError:
        ok = False
    if not ok:
        raise PermanentError(f"only http(s) URLs are allowed, got {url!r}")


def has_wanted_extension(name: str) -> bool:
    """True when ``name`` ends in an audio or archive extension (case-insensitive)."""
    lower = name.lower()
    return lower.endswith(ARCHIVE_EXTS) or PurePosixPath(lower).suffix in AUDIO_EXTS


def normalise_relative_name(name: Any) -> str | None:
    """Return ``name`` if it is a safe relative posix path, else None."""
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        return None
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        return None
    parts = PurePosixPath(name).parts
    if not parts or any(p in ("..", ".", "") for p in parts):
        return None
    return name


def decode_text(data: bytes, charset: str | None) -> str:
    """Decode a fetched text body (with replacement) the way a feed reader must.

    The codec comes from, in this order: a byte order mark (UTF-8, UTF-16, UTF-32); the HTTP
    ``charset``; the encoding named in a leading ``<?xml ... encoding="..."?>``; else UTF-8. An
    unknown codec name falls back to UTF-8. (``application/rss+xml`` is often served without a charset.)
    """
    if data.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        codec = "utf-32"
    elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
        codec = "utf-16"
    elif data.startswith(BOM):
        codec = "utf-8-sig"
    elif charset:
        codec = charset
        if codec.lower().replace("_", "-") in ("utf-8", "utf8"):
            codec = "utf-8-sig"
    else:
        match = _XML_DECL_RE.match(data[:1024])
        codec = match.group(1).decode("ascii") if match else "utf-8"
        # a declared wide encoding without a BOM cannot be what an ASCII-readable header came in
        if codec.lower().replace("_", "-").startswith(("utf-16", "utf-32", "ucs")):
            codec = "utf-8"
    try:
        return data.decode(codec, errors="replace")
    except LookupError:
        return data.decode("utf-8-sig", errors="replace")


class HttpClient:
    """Polite aiohttp wrapper: User-Agent, retries with backoff on 5xx and network errors."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        user_agent: str = USER_AGENT,
        retries: int = 3,
        retry_delay: float = 2.0,
    ) -> None:
        self.session = session
        self.user_agent = user_agent
        self.retries = retries
        self.retry_delay = retry_delay  # seconds, multiplied by attempt number

    async def retrying(self, op: Callable[[], Awaitable[T]], what: str) -> T:
        """Run ``op`` with retries on 5xx / network errors.

        ``PermanentError``, ``NoSpaceError`` and ``DownloadCancelled`` are re-raised at once.
        """
        last: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                return await op()
            except (PermanentError, NoSpaceError, DownloadCancelled):
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                last = exc
                log.warning("%s failed (attempt %d/%d): %s", what, attempt, self.retries, exc)
                if attempt < self.retries:
                    await asyncio.sleep(self.retry_delay * attempt)
        raise RuntimeError(f"{what} failed after {self.retries} attempts: {last}")

    def get(self, url: str, timeout: aiohttp.ClientTimeout = API_TIMEOUT, **kwargs: Any):
        headers = {"User-Agent": self.user_agent, **(kwargs.pop("headers", None) or {})}
        return self.session.get(url, headers=headers, timeout=timeout, **kwargs)

    @staticmethod
    def check_status(resp: aiohttp.ClientResponse, url: str) -> None:
        if resp.status >= 500:
            raise aiohttp.ClientResponseError(resp.request_info, resp.history, status=resp.status)
        if resp.status >= 400:
            raise PermanentError(f"HTTP {resp.status} for {url}")

    async def get_json(
        self, url: str, what: str, params: Any = None, headers: dict[str, str] | None = None
    ) -> Any:
        async def op() -> Any:
            async with self.get(url, params=params, headers=headers) as resp:
                self.check_status(resp, url)
                return await resp.json(content_type=None)

        return await self.retrying(op, what)

    async def get_text(self, url: str, what: str, max_bytes: int = MAX_TEXT_BYTES) -> tuple[str, str]:
        """GET ``url`` and return ``(text, final URL after redirects)``.

        The text is decoded with replacement: byte order mark, else the response charset, else the
        encoding declared by a leading ``<?xml?>``, else UTF-8 (see :func:`decode_text`).
        ``PermanentError`` for a non-http(s) URL, HTTP 4xx or a body over ``max_bytes``.
        """
        check_url(url)

        async def op() -> tuple[str, str]:
            async with self.get(url) as resp:
                self.check_status(resp, url)
                if (resp.content_length or 0) > max_bytes:
                    raise PermanentError(f"{url} is larger than {max_bytes} bytes")
                body = bytearray()
                async for chunk in resp.content.iter_chunked(1 << 16):
                    body += chunk
                    if len(body) > max_bytes:
                        raise PermanentError(f"{url} is larger than {max_bytes} bytes")
                charset = resp.charset
                final_url = str(resp.url)
            return decode_text(bytes(body), charset), final_url

        return await self.retrying(op, what)


def _resolve_algo(checksum: str | None, algo: str | None) -> str | None:
    if not checksum:
        return None
    chosen = (algo or _HEX_LEN_TO_ALGO.get(len(checksum)) or "").lower()
    if chosen not in SUPPORTED_ALGOS:
        raise PermanentError(f"unsupported checksum algorithm {algo!r} for a {len(checksum)}-char checksum")
    return chosen


def _response_name(resp: aiohttp.ClientResponse, hint: str) -> str:
    """Pick the file name for a response: Content-Disposition, final URL segment, else ``hint``."""
    candidate = ""
    disposition = resp.content_disposition
    if disposition is not None and disposition.filename:
        candidate = re.split(r"[\\/]", disposition.filename)[-1].strip()
    if not candidate:
        candidate = unquote(urlparse(str(resp.url)).path.rsplit("/", 1)[-1], errors="replace").strip()
    if not candidate:
        candidate = hint
    if not has_wanted_extension(candidate):
        candidate += _CONTENT_TYPE_EXT.get(resp.content_type, "")
    return safe_component(candidate, fallback=hint or "file")


def _write_and_hash(out: Any, data: bytes, digest: Any) -> None:
    """Write ``data`` and feed it to ``digest`` (blocking: for a worker thread; calls are sequential, so in order)."""
    if digest is not None:
        digest.update(data)
    out.write(data)


def _unique_name(folder: Path, name: str) -> str:
    """``name`` made unique among the (case/normalisation-insensitive) entries of ``folder``."""
    try:
        taken = {name_key(p.name) for p in folder.iterdir()}
    except OSError:
        taken = set()
    return dedupe_name(name, taken)


async def download_file(
    client: HttpClient,
    url: str,
    folder: Path,
    name: str,
    max_bytes: int,
    checksum: str | None = None,
    checksum_algo: str | None = None,
    headers: dict[str, str] | None = None,
    *,
    progress: Callable[[int, int | None], None] | None = None,
    cancel: asyncio.Event | None = None,
    min_free: int = 0,
    name_from_response: bool = False,
) -> int:
    """Stream ``url`` to ``folder/name`` (via ``name.part``, atomic rename); return bytes written.

    The body may not exceed ``max_bytes``; the checksum (sha1/md5/sha256) is verified when
    given. File I/O runs in worker threads, off the event loop. Raises on failure.

    ``progress(received, total_or_None)`` is called at most every 0.5 s and once at the end;
    ``cancel`` (checked per chunk) raises :class:`DownloadCancelled`; the free space of
    ``folder`` is kept above ``min_free`` (:class:`NoSpaceError`, also raised when a write hits a full disk
    or quota: never retried, never a failure). A body shorter than its
    Content-Length is retried; an HTML page where a file was expected is permanent. With
    ``name_from_response`` ``name`` is only a hint (its folder part is kept): the final file name
    comes from Content-Disposition, the final URL or the Content-Type (see ``_response_name``).
    """
    check_url(url)
    if normalise_relative_name(name) is None:
        raise RuntimeError(f"unsafe path {name!r}")
    dest = folder / name

    def prepare() -> bool:
        if not dest.resolve().is_relative_to(folder.resolve()):
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        return True

    try:
        prepared = await asyncio.to_thread(prepare)
    except OSError as exc:
        if is_no_space_error(exc):  # creating the folder on a full disk: wait for space, no failure
            raise NoSpaceError(f"the disk is full, cannot create {dest.parent}: {exc}") from exc
        raise
    if not prepared:
        raise RuntimeError(f"unsafe path {name!r}")
    part = dest.with_name(dest.name + ".part")
    algo = _resolve_algo(checksum, checksum_algo)

    async def op() -> int:
        digest = hashlib.new(algo) if algo else None
        received = 0
        final = dest
        if cancel is not None and cancel.is_set():
            raise DownloadCancelled(name)
        await asyncio.to_thread(ensure_free, folder, 0, min_free)
        async with client.get(url, timeout=DOWNLOAD_TIMEOUT, headers=headers) as resp:
            client.check_status(resp, url)
            if name_from_response:
                final = dest.with_name(_response_name(resp, dest.name))
            if resp.content_type in _HTML_TYPES and (name_from_response or has_wanted_extension(name)):
                raise PermanentError(f"got an HTML page instead of a file from {url}")
            encoded = resp.headers.get("Content-Encoding", "identity").lower() not in ("", "identity")
            expected = None if encoded else resp.content_length  # decoded size is unknown otherwise
            if expected is not None and expected > max_bytes:
                raise PermanentError(f"{name} is larger than expected ({max_bytes} bytes)")
            try:
                out = await asyncio.to_thread(part.open, "wb")
                try:
                    buf = bytearray()
                    last_report = time.monotonic()
                    async for chunk in resp.content.iter_chunked(1 << 16):
                        if cancel is not None and cancel.is_set():
                            raise DownloadCancelled(name)
                        received += len(chunk)
                        if received > max_bytes:
                            raise PermanentError(f"{name} is larger than expected ({max_bytes} bytes)")
                        buf += chunk
                        if len(buf) >= _WRITE_BUFFER:
                            await asyncio.to_thread(ensure_free, folder, 0, min_free)
                            await asyncio.to_thread(_write_and_hash, out, bytes(buf), digest)  # hashed off the loop too
                            buf.clear()
                        if progress is not None and time.monotonic() - last_report >= PROGRESS_INTERVAL:
                            last_report = time.monotonic()
                            progress(received, expected)
                    if buf:
                        await asyncio.to_thread(ensure_free, folder, 0, min_free)
                        await asyncio.to_thread(_write_and_hash, out, bytes(buf), digest)
                finally:
                    await asyncio.to_thread(out.close)
            except OSError as exc:
                # a full disk or quota is a wait for space, not a network error: retrying would fill the
                # disk again and, after three tries, count the release as failed (min_free 0 = no check)
                if is_no_space_error(exc):
                    raise NoSpaceError(
                        f"the disk is full while writing {name} ({human_bytes(received)} received): {exc}"
                    ) from exc
                raise
        if expected is not None and received != expected:
            raise aiohttp.ClientPayloadError(f"{name}: got {received} of {expected} bytes")
        if digest and checksum and digest.hexdigest().lower() != checksum.lower():
            raise PermanentError(f"{algo} mismatch for {name}")
        if progress is not None:
            progress(received, expected if expected is not None else received)
        if name_from_response:
            final = final.with_name(await asyncio.to_thread(_unique_name, final.parent, final.name))
        await asyncio.to_thread(part.replace, final)
        return received

    try:
        return await client.retrying(op, f"download {name}")
    finally:
        await asyncio.to_thread(part.unlink, missing_ok=True)


class StateStore(Protocol):
    def get_state(self, key: str) -> str | None: ...
    def set_state(self, key: str, value: str) -> None: ...


class SeenStore:
    """Per-release processed signature and failure counter, kept in ``db`` state.

    Keys: ``<prefix>:sig:<id>``, ``<prefix>:fail:<id>``, ``<prefix>:failsig:<id>`` and
    ``<prefix>:failat:<id>`` (time of the last failure; a release that failed ``MAX_FAILURES``
    times is parked until its files change or ``RETRY_AFTER`` seconds passed).
    """

    def __init__(self, db: StateStore, prefix: str, clock: Callable[[], float] = time.time) -> None:
        self._db = db
        self.prefix = prefix
        self._clock = clock

    def _key(self, kind: str, release_id: str) -> str:
        return f"{self.prefix}:{kind}:{release_id}"

    def _int(self, key: str) -> int:
        try:
            return int(self._db.get_state(key) or 0)
        except ValueError:
            return 0

    def is_unchanged(self, release_id: str, signature: str) -> bool:
        return self._db.get_state(self._key("sig", release_id)) == signature

    def failures(self, release_id: str) -> int:
        return self._int(self._key("fail", release_id))

    def track_signature(self, release_id: str, signature: str) -> int:
        """Reset the failure counter when the signature changed; return the current failures."""
        failsig, fail = self._key("failsig", release_id), self._key("fail", release_id)
        if self._db.get_state(failsig) != signature:
            self._db.set_state(failsig, signature)
            self._db.set_state(fail, "0")
        return self._int(fail)

    def retry_due(self, release_id: str) -> bool:
        """True when a parked release (``MAX_FAILURES`` failures) may be tried again."""
        try:
            failed_at = float(self._db.get_state(self._key("failat", release_id)) or 0)
        except ValueError:
            failed_at = 0.0
        return self._clock() - failed_at >= RETRY_AFTER

    def record_failure(self, release_id: str) -> None:
        key = self._key("fail", release_id)
        count = self._int(key) + 1
        self._db.set_state(key, str(count))
        self._db.set_state(self._key("failat", release_id), repr(self._clock()))
        if count >= MAX_FAILURES:
            log.warning(
                "%s %s failed %d times; parking it for %d h (or until its files change)",
                self.prefix, release_id, count, RETRY_AFTER // 3600,
            )

    def record_success(self, release_id: str, signature: str | None) -> None:
        if signature:
            self._db.set_state(self._key("sig", release_id), signature)
        self._db.set_state(self._key("fail", release_id), "0")
