"""Import of a region file from an https address (background task of the Regions page).

Only https; every address (and every redirect hop) goes through :mod:`car_media_hub.netguard` (no private, local or
internal hosts, ports of this add-on and of mail submission refused), the name is resolved once and the connection
goes to the checked address (``_PinnedResolver``: a DNS answer cannot change between the check and the connection).
The download is streamed into a hidden ``.import-<kind>.part`` file next to the target; after a cancel or a broken
connection the part stays and the next import of the same address resumes with ``Range`` / ``If-Range`` (only when
the first answer had a strong ETag or Last-Modified; the server must confirm it, else the download restarts at 0). At the end the
size limit, the file header, the optional SHA-256 and the free space are checked and the file is renamed atomically.

The state lives on disk (``.import-<kind>.part`` + ``.import-<kind>.json`` with the address, the validator, the optional
SHA-256 and the length that is known to be fsynced), so an add-on restart in the middle is picked up by
:meth:`car_media_hub.regions.RegionManager.resume_imports`; a part is cut back to its fsynced length before it is resumed.
The SHA-256 computed while streaming goes straight into ``files.json``, so the finished file is served without being read again.
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import os
import socket
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver

from . import netguard
from . import regions as regions_mod
from .diskspace import ensure_free, human_bytes
from .regions import (
    FILE_NAMES, HASH_CHUNK, RegionError, TransferInterrupted, head_bytes_needed, read_small_json, validate_head,
    write_json_atomic,
)

if TYPE_CHECKING:
    from .regions import ImportTask, RegionManager

log = logging.getLogger(__name__)

MAX_QUEUED_IMPORTS = 20  # running + waiting imports; they run one after the other
MAX_REDIRECTS = 5
MAX_URL_LENGTH = 2048
USER_AGENT = "CarMediaAndTripsHub/regions (Home Assistant add-on)"


async def _public_address(host: str, port: int) -> str:
    try:
        return await netguard.resolve_public(host, port)
    except netguard.BlockedURL as exc:
        raise RegionError(f"the address is refused: {exc}") from None


@dataclass
class ImportPolicy:
    """What an import may reach. The defaults are the production rules; tests relax them for a local server."""

    require_https: bool = True
    check_hosts: bool = True  # False: connect to the name as it is (tests only)
    resolve: Callable[[str, int], Awaitable[str]] = field(default=_public_address)
    connect_timeout: float = 30.0
    read_timeout: float = 60.0


def display_url(url: str) -> str:
    """The address without credentials and query, shortened: what the page shows and the log keeps."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{host}{port}{parts.path[:200]}"


def check_url_syntax(url: object, policy: ImportPolicy) -> str:
    """The cleaned address or :class:`RegionError` (https only, no credentials, no private or internal host)."""
    if not isinstance(url, str):
        raise RegionError("url must be text")
    text = url.strip()
    if not text or len(text) > MAX_URL_LENGTH:
        raise RegionError("the address is empty or too long")
    try:
        parts = urlsplit(text)
        _ = parts.port
    except ValueError:
        raise RegionError("the address is not valid") from None
    allowed = ("https",) if policy.require_https else ("https", "http")
    if parts.scheme.lower() not in allowed:
        raise RegionError("only https addresses can be imported")
    if parts.username is not None or parts.password is not None:
        raise RegionError("addresses with a user name or password are not imported")
    if policy.check_hosts:
        try:
            netguard.check_navigation_url(text)
        except netguard.BlockedURL as exc:
            raise RegionError(f"the address is refused: {exc}") from None
    elif not parts.hostname:
        raise RegionError("the address has no host")
    return text


class _PinnedResolver(AbstractResolver):
    """Resolves the one host of a hop to the address that was checked."""

    def __init__(self, ip: str) -> None:
        self._ip = ip

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET) -> list[dict[str, Any]]:
        fam = socket.AF_INET6 if ipaddress.ip_address(self._ip).version == 6 else socket.AF_INET
        return [{"hostname": host, "host": self._ip, "port": port, "family": fam, "proto": 0, "flags": socket.AI_NUMERICHOST}]

    async def close(self) -> None:
        return None


def _part_paths(directory: Path, kind: str) -> tuple[Path, Path]:
    return directory / f".import-{kind}.part", directory / f".import-{kind}.json"


def _url_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _open_part(path: Path, truncate: bool) -> Any:
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= os.O_TRUNC if truncate else os.O_APPEND
    fd = os.open(path, flags, 0o644)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RegionError("the partial file is not a regular file", 500)
        return os.fdopen(fd, "ab" if not truncate else "wb")
    except BaseException:
        os.close(fd)
        raise


def _hash_prefix(path: Path, digest: Any) -> bytes:
    """Feed the existing part into ``digest`` (blocking); returns its first 512 bytes."""
    head = b""
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(HASH_CHUNK)
            if not chunk:
                return head
            if len(head) < 512:
                head += chunk[: 512 - len(head)]
            digest.update(chunk)


def _sync_part(handle: Any, meta_path: Path, meta: dict[str, Any], length: int) -> None:
    """Flush the part to disk and note that its first ``length`` bytes are safe (blocking)."""
    handle.flush()
    os.fsync(handle.fileno())
    meta["synced"] = length
    write_json_atomic(meta_path, meta)


def _close_part(handle: Any, meta_path: Path, meta: dict[str, Any]) -> None:
    """Close the part after a stop or a lost connection (blocking): what was written is fsynced and counts as safe, so
    the next try resumes from there; a part that was discarded meanwhile (closed handle) is left alone."""
    if handle.closed:
        return
    try:
        handle.flush()
        os.fsync(handle.fileno())
        _sync_part(handle, meta_path, meta, os.fstat(handle.fileno()).st_size)
    except OSError:
        pass
    finally:
        handle.close()


def _discard(*paths: Path) -> None:
    for path in paths:
        try:
            os.unlink(path)
        except OSError:
            pass


def discard_import_files(directory: Path, kind: str) -> None:
    """Delete the partial download of ``kind`` and its meta file (blocking)."""
    _discard(*_part_paths(directory, kind))


def _usable_validator(meta: dict[str, Any]) -> str:
    """The validator for ``If-Range`` kept from the first try: a strong ETag or a Last-Modified date, else ''."""
    etag = meta.get("etag") or ""
    if etag and not etag.startswith("W/"):
        return etag
    return meta.get("lastModified") or ""


def _resume_state(part: Path, meta_path: Path, key: str, max_bytes: int) -> tuple[int, dict[str, Any]]:
    """What an earlier try of the same address left behind (blocking): (offset to resume at, its meta data).

    Without a validator (neither a strong ETag nor Last-Modified) the part cannot be checked against the current file
    on the server, so it is dropped and the download starts again. Bytes behind the last fsynced length (``synced``)
    may be lost or garbled after a power cut: the part is cut back to that length first.
    """
    try:
        st = os.lstat(part)
        meta = read_small_json(meta_path)
    except (OSError, ValueError):
        return 0, {}
    if not (stat.S_ISREG(st.st_mode) and isinstance(meta, dict) and meta.get("url") == key and _usable_validator(meta)):
        return 0, {}
    size = st.st_size
    synced = meta.get("synced")
    if isinstance(synced, int) and not isinstance(synced, bool) and 0 <= synced < size:
        try:
            os.truncate(part, synced)
        except OSError:
            return 0, {}
        size = synced
    return (size, meta) if 0 < size < max_bytes else (0, {})


def set_auto_resume(directory: Path, kind: str, on: bool) -> None:
    """Switch the restart pick-up of a part on or off (blocking); without a meta file there is nothing to do."""
    _, meta_path = _part_paths(directory, kind)
    try:
        meta = read_small_json(meta_path)
        if isinstance(meta, dict) and bool(meta.get("auto")) != on:
            meta["auto"] = on
            write_json_atomic(meta_path, meta)
    except (OSError, ValueError):
        pass


def pending_imports(root: Path) -> list[tuple[str, str, str, str]]:
    """The imports an add-on restart interrupted (blocking): (region id, kind, address, expected sha256).

    A part counts when its meta file names the address and says ``auto`` (set while an import runs, cleared when the
    owner cancels it or it fails for another reason than the connection).
    """
    out: list[tuple[str, str, str, str]] = []
    try:
        entries = sorted(os.scandir(root), key=lambda e: e.name)
    except OSError:
        return out
    for entry in entries:
        if not regions_mod.valid_region_id(entry.name):
            continue
        directory = Path(entry.path)
        for kind in FILE_NAMES:
            part, meta_path = _part_paths(directory, kind)
            try:
                meta = read_small_json(meta_path)
                if not stat.S_ISREG(os.lstat(part).st_mode) or not isinstance(meta, dict):
                    continue
            except (OSError, ValueError):
                continue
            address, sha = meta.get("address"), meta.get("sha256", "")
            if isinstance(address, str) and address and meta.get("auto") is True:
                out.append((entry.name, kind, address, sha if isinstance(sha, str) else ""))
    return out


def _head_of(path: Path) -> tuple[int, bytes]:
    """Size and first 512 bytes of a finished part (blocking)."""
    size = os.stat(path).st_size
    with open(path, "rb") as handle:
        return size, handle.read(512)


async def _hop(session_factory: Callable[[str], aiohttp.ClientSession], policy: ImportPolicy, url: str,
               headers: dict[str, str]) -> tuple[aiohttp.ClientSession, aiohttp.ClientResponse, str]:
    """GET ``url`` following up to MAX_REDIRECTS redirects by hand; every hop is checked again."""
    for _ in range(MAX_REDIRECTS + 1):
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
        ip = await policy.resolve(host, port) if policy.check_hosts else ""
        session = session_factory(ip)
        try:
            response = await session.get(url, headers=headers, allow_redirects=False)
        except BaseException:
            await session.close()
            raise
        if response.status in (301, 302, 303, 307, 308):
            location = response.headers.get("Location", "")
            response.release()
            await session.close()
            if not location:
                raise RegionError("the server redirected without an address")
            url = check_url_syntax(urljoin(url, location), policy)
            continue
        return session, response, url
    raise RegionError("too many redirects")


async def run_import(manager: "RegionManager", task: "ImportTask", directory: Path, url: str, policy: ImportPolicy) -> None:
    kind = task.kind
    final = directory / FILE_NAMES[kind]
    part, meta_path = _part_paths(directory, kind)
    key = _url_key(url)
    max_bytes = manager.max_file_bytes

    def make_session(ip: str) -> aiohttp.ClientSession:
        connector = aiohttp.TCPConnector(resolver=_PinnedResolver(ip), limit=1, force_close=True) if ip else aiohttp.TCPConnector(limit=1, force_close=True)
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=policy.connect_timeout, sock_read=policy.read_timeout)
        return aiohttp.ClientSession(connector=connector, timeout=timeout, auto_decompress=False, headers={"User-Agent": USER_AGENT})

    # what an earlier try left behind
    offset, meta = await asyncio.to_thread(_resume_state, part, meta_path, key, max_bytes)
    if offset == 0:
        await asyncio.to_thread(_discard, part, meta_path)

    streamed: Any = None  # sha256 of the whole part when this run saw all of it
    for attempt in range(2):
        headers = {"Accept-Encoding": "identity"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
            headers["If-Range"] = _usable_validator(meta)  # _resume_state guarantees there is one
        session, response, final_url = await _hop(make_session, policy, url, headers)
        try:
            status = response.status
            if status == 416 and offset:
                total_text = response.headers.get("Content-Range", "").rpartition("/")[2]
                if total_text.isdigit() and int(total_text) == offset:
                    break  # the part is complete already
                await asyncio.to_thread(_discard, part, meta_path)
                offset, meta = 0, {}
                continue
            if status not in (200, 206):
                raise RegionError(f"the server answered {status}")
            if status == 200 and offset:
                offset, meta = 0, {}  # the server ignores or refuses the range: start again
            if status == 206:
                content_range = response.headers.get("Content-Range", "")
                if not content_range.startswith(f"bytes {offset}-"):
                    raise RegionError("the server answered with a different range")
                # the server must confirm that it is still the same file (a 206 answers If-Range only when it matches)
                new_etag, new_modified = response.headers.get("ETag", ""), response.headers.get("Last-Modified", "")
                if (meta.get("etag") and new_etag != meta["etag"]) or (
                    not meta.get("etag") and meta.get("lastModified") and new_modified != meta["lastModified"]
                ):
                    await asyncio.to_thread(_discard, part, meta_path)
                    offset, meta = 0, {}
                    continue  # the file changed on the server: start again from 0
            length = response.content_length
            total = (length + offset) if length is not None else None
            if total is not None and total > max_bytes:
                raise RegionError(f"the file ({human_bytes(total)}) is larger than the limit of {human_bytes(max_bytes)}", 413)
            task.total, task.downloaded, task.resumed_from = total, offset, offset
            # with or without a stated length the reserve (min_free_space_gb) must stay free
            await asyncio.to_thread(ensure_free, directory, (total - offset) if total is not None else 0, manager.min_free)
            next_check = task.downloaded + regions_mod.FREE_CHECK_BYTES
            meta_doc = {
                "url": key, "etag": response.headers.get("ETag", ""), "lastModified": response.headers.get("Last-Modified", ""),
                "total": total, "address": url, "sha256": task.expected_sha256, "auto": True, "synced": offset,
            }
            await asyncio.to_thread(write_json_atomic, meta_path, meta_doc)
            digest = hashlib.sha256()
            head = b""
            if offset:
                head = await asyncio.to_thread(_hash_prefix, part, digest)
            need = head_bytes_needed(kind)
            handle = await asyncio.to_thread(_open_part, part, offset == 0)
            next_sync = task.downloaded + regions_mod.SYNC_BYTES
            try:
                async for chunk in response.content.iter_chunked(HASH_CHUNK):
                    if len(head) < need:
                        head += chunk[: need - len(head)]
                        if len(head) >= need:
                            try:
                                validate_head(kind, head)
                            except RegionError:
                                await asyncio.to_thread(handle.close)
                                await asyncio.to_thread(_discard, part, meta_path)
                                raise
                    digest.update(chunk)
                    task.downloaded += len(chunk)
                    if task.downloaded > max_bytes:
                        await asyncio.to_thread(handle.close)
                        await asyncio.to_thread(_discard, part, meta_path)
                        raise RegionError(f"the download exceeds the limit of {human_bytes(max_bytes)}", 413)
                    await asyncio.to_thread(handle.write, chunk)
                    if task.downloaded >= next_sync:
                        next_sync = task.downloaded + regions_mod.SYNC_BYTES
                        await asyncio.to_thread(_sync_part, handle, meta_path, meta_doc, task.downloaded)
                    if task.downloaded >= next_check:
                        next_check = task.downloaded + regions_mod.FREE_CHECK_BYTES
                        await asyncio.to_thread(
                            ensure_free, directory, max(0, (total - task.downloaded)) if total is not None else 0, manager.min_free)
                await asyncio.to_thread(handle.flush)
                await asyncio.to_thread(os.fsync, handle.fileno())
                streamed = digest
            finally:
                await asyncio.to_thread(_close_part, handle, meta_path, meta_doc)
        finally:
            response.release()
            await session.close()
        if total is not None and task.downloaded != total:
            raise TransferInterrupted(f"the download ended early ({human_bytes(task.downloaded)} of {human_bytes(total)}); import it again to resume")
        break
    else:
        raise RegionError("the server did not deliver the file")

    # complete: verify, then publish atomically
    size, head = await asyncio.to_thread(_head_of, part)
    digest = streamed
    if digest is None and task.expected_sha256:
        digest = hashlib.sha256()
        await asyncio.to_thread(_hash_prefix, part, digest)
    try:
        validate_head(kind, head, size)
        if size > max_bytes:
            raise RegionError(f"the file is larger than the limit of {human_bytes(max_bytes)}", 413)
        if task.expected_sha256 and digest.hexdigest() != task.expected_sha256:
            raise RegionError("the SHA-256 of the download does not match the expected value")
    except RegionError:
        await asyncio.to_thread(_discard, part, meta_path)
        raise
    part_stat = await asyncio.to_thread(os.stat, part)  # size and mtime survive the rename: the identity of the hashed bytes
    await asyncio.to_thread(os.replace, part, final)
    await asyncio.to_thread(_discard, meta_path)
    if digest is not None:
        task.sha256, task.published = digest.hexdigest(), (part_stat.st_size, part_stat.st_mtime_ns)
