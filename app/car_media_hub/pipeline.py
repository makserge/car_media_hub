"""Release processing pipeline and approval workflow."""
from __future__ import annotations

import asyncio
import base64
import errno
import hashlib
import json
import logging
import math
import os
import re
import shutil
import stat
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from car_media_hub.archive import (
    DISK_RESERVE_BYTES,
    ArchiveError,
    ExtractionBudget,
    ExtractionNoSpace,
    extract_nested,
    safe_extract,
)
from car_media_hub.cleaner import clean_release, display_text, map_renamed, repair_names
from car_media_hub.config import AUDIO_EXTS, library_blocks, path_relation
from car_media_hub.diskspace import (
    NoSpaceError,
    ensure_free,
    free_bytes,
    human_bytes,
    is_no_space_error,
    min_free_bytes,
)
from car_media_hub.hashing import hash_path, sha256_file
from car_media_hub.names import SYNC_AUDIO_EXTS, dedupe_name, name_key
from car_media_hub.sync import library_has_listable_file

if TYPE_CHECKING:
    from car_media_hub.config import Config
    from car_media_hub.db import Database, Release
    from car_media_hub.sources import Candidate, Outcome, Source

logger = logging.getLogger(__name__)

DEFAULT_REVIEW_URL = "/app/car_media_hub"
EVENT_MOBILE_ACTION = "mobile_app_notification_action"
EVENT_DECISION = "car_media_hub_decision"
_APPROVE_PREFIX = "MW_APPROVE_"
_DECLINE_PREFIX = "MW_DECLINE_"

STATE_LAST_DELETE = "sync:last_delete_at"  # read by the sync API's empty-library guard
STATE_LIBRARY_INDEXED = "library:indexed:v1"  # + ":<sha256 of the library path>" (see _indexed_key)
STATE_LAST_COUNT = "sync:last_count"  # kept by the sync API's empty-library guard
STATE_LAST_NONEMPTY = "sync:last_nonempty_at"
STATE_USED_NAMES = "library:used_names:v1"  # JSON list of hashes of top-level names ever published
STATE_LIBRARY_RETRY = "library:index_retry:v1"  # + ":<sha256 of the library path>": folders the index could not read
STATE_LIBRARY_ALERT = "library:alert_posted:v1"  # "1" while the "library unavailable" notification is showing
_USED_NAMES_CAP = 5000
LIBRARY_RELEASE_ID = "library"  # release_id of tracks found by index_library()
RSS_IMPORT_RELEASE_ID = "rss-import"  # release_id of tracks imported from rss_downloader's state
_INDEX_BATCH = 200
LIBRARY_UNAVAILABLE_NOTIFICATION = "car_media_hub_library_unavailable"
_ENCODED_PREFIX = "~mw64~"  # a library path that is not valid UTF-8 or starts with this prefix, as the UI sends it back
_FREE_PROBE_TIMEOUT = 3.0  # seconds a free-space probe may take before the status shows "unknown"

_REMOVAL_LABELS = (
    ("removed_hidden", "hidden file/folder"),
    ("removed_cyrillic_name", "Cyrillic file/folder name"),
    ("removed_cyrillic_tags", "Cyrillic tags"),
    ("removed_mp3_due_to_flac", "MP3 (FLAC present)"),
    ("removed_duplicate_tracks", "duplicate tracks"),
)
_UNSAFE_NAME_RE = re.compile(r'[/\\:*?"<>|\x00-\x1f\x7f]')


class HAClientLike(Protocol):
    """The subset of :class:`car_media_hub.ha.HAClient` used by the controller."""

    async def persistent_create(self, notification_id: str, title: str, message: str) -> None: ...
    async def persistent_dismiss(self, notification_id: str) -> None: ...
    async def notify(self, service: str, title: str, message: str, data: dict | None = None) -> None: ...


class InvalidState(ValueError):
    """The release is not in a state that allows the requested operation."""


def human_size(num: float) -> str:
    """Format a byte count as a short human-readable string (e.g. ``12.3 MB``)."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024 or unit == "TB":
            return f"{int(num)} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"  # pragma: no cover


_MAX_NAME_BYTES = 200  # leaves room for a " (n)" suffix within NAME_MAX (255 bytes)
_MD_SPECIAL_RE = re.compile(r"([\\`*_\[\]()!#<>|~])")


def _flat(text: Any) -> str:
    """Single-line, valid UTF-8 version of ``text`` (for notification titles)."""
    return " ".join(display_text(str(text)).split())


def md_escape(text: Any) -> str:
    """Escape markdown control characters and flatten newlines in untrusted text."""
    return _MD_SPECIAL_RE.sub(r"\\\1", _flat(text))


def sanitize_name(name: str, release_id: str) -> str:
    """Make ``name`` safe as a single directory name (at most 200 UTF-8 bytes)."""
    cleaned = _UNSAFE_NAME_RE.sub("", display_text(name)).strip(" .")
    if cleaned in ("", ".", ".."):
        return f"release-{release_id}"
    truncated = cleaned.encode("utf-8")[:_MAX_NAME_BYTES].decode("utf-8", "ignore")
    return truncated.strip(" .") or f"release-{release_id}"


def _unique_destination(parent: Path, name: str, retired: frozenset[str] | set[str] = frozenset(),
                        reserved: frozenset[str] | set[str] = frozenset()) -> Path:
    """``parent / name``, or with " (2)", " (3)"... when an entry with the same name_key exists.

    ``retired`` holds :func:`_name_token` values of names published before and deleted since:
    clients may still hold the old files under them, so a new release must not reuse them.
    ``reserved`` holds :func:`name_key` values of folders that an approval in progress (or an
    interrupted one) has already picked, though they may not exist yet.
    """
    try:
        taken = {name_key(entry) for entry in os.listdir(parent)}
    except FileNotFoundError:
        taken = set()
    taken |= reserved
    # A folder name has no extension, so " (n)" goes after the whole name ("Vol. 2 (2)"), and every
    # candidate comes from the original name: "Album", "Album (2)", "Album (3)" and so on.
    candidate = dedupe_name(name, taken, max_bytes=_MAX_NAME_BYTES + 16, split_ext=False)
    while _name_token(candidate) in retired:
        taken.add(name_key(candidate))
        candidate = dedupe_name(name, taken, max_bytes=_MAX_NAME_BYTES + 16, split_ext=False)
    return parent / candidate


def _name_token(name: str) -> str:
    return hashlib.sha1(name_key(name).encode("utf-8", "surrogatepass")).hexdigest()[:16]


def _approval_tmp(library_dir: Path, release_id: str) -> Path:
    return library_dir / f".mw-{release_id}.tmp"


def _rmtree(path: Path | str | None) -> None:
    if path:
        shutil.rmtree(path, ignore_errors=True)


def _drop_symlinks(root: Path) -> None:
    """Remove every symlink below ``root`` (copytree was told not to follow them)."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in [*dirnames, *filenames]:
            path = Path(dirpath) / name
            if path.is_symlink():
                logger.warning("Dropping symlink %s", path)
                path.unlink()
        dirnames[:] = [d for d in dirnames if not (Path(dirpath) / d).is_symlink()]


def _tree_size(root: Path) -> int:
    """Total size of the regular files below ``root`` (symlinks are not followed)."""
    total = 0
    for dirpath, _dirs, files in os.walk(root, followlinks=False):
        for name in files:
            try:
                info = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
    return total


def _populate_content(cand: Candidate, content: Path, min_free: int = 0) -> None:
    """Move/copy (directory) or safely extract (archive) the candidate into ``content``."""
    # leave room for the larger of 1 GiB and the configured reserve
    reserve = max(DISK_RESERVE_BYTES, min_free)
    copied = 0  # bytes of the folder copied into ``content`` (they are no longer free for the extraction)
    if cand.is_dir:
        moved = False
        if getattr(cand, "disposable", False):
            try:
                os.rename(cand.path, content)  # same file system: no copy, no extra space
                moved = True
            except OSError as exc:
                logger.info("Cannot move %s, copying instead: %s", cand.path, exc)
        if not moved:
            copied = _tree_size(Path(cand.path))
            ensure_free(content.parent, copied, min_free)
            shutil.copytree(cand.path, content, symlinks=True)
        _drop_symlinks(content)
        budget = ExtractionBudget.for_destination(content, reserve=reserve)
    else:
        content.mkdir(parents=True, exist_ok=True)
        # one budget (bounded by free disk space) for the archive and its nested archives
        budget = ExtractionBudget.for_destination(content, reserve=reserve)
        safe_extract(cand.path, content, budget)
    try:
        extract_nested(content, budget=budget)
    except ExtractionNoSpace as exc:
        if copied and exc.needed is not None:
            # The budget was made after the copy, so its need leaves the copy out. Waiting for that
            # much only would start (hash, copy, extract) the release again in vain, scan after scan.
            exc.needed += copied
            if budget.disk_total is not None and exc.needed > budget.disk_total:
                raise ArchiveError("the folder and the archives in it do not fit the disk together") from exc
        raise


def _pick_destination(library_dir: Path, name: str, retired: frozenset[str] | set[str] = frozenset(),
                      reserved: frozenset[str] | set[str] = frozenset()) -> Path:
    library_dir.mkdir(parents=True, exist_ok=True)
    return _unique_destination(library_dir, name, retired, reserved)


def _touch_files(root: Path) -> None:
    """Set every regular file's mtime to now: the library time is the arrival time (SYNC_PROTOCOL 4.3)."""
    for dirpath, _dirs, files in os.walk(root, followlinks=False):
        for name in files:
            path = os.path.join(dirpath, name)
            if stat.S_ISREG(os.lstat(path).st_mode):
                os.utime(path, None)


def _copy_into_library(content: Path, tmp: Path, reserve: int) -> None:
    """Copy ``content`` to ``tmp`` (blocking), refusing to eat into the free-space ``reserve``.

    The whole tree is checked first and every file again before it is copied, because other
    writers share the disk. :class:`NoSpaceError` is not an ``OSError``, so ``copytree`` stops at
    once instead of carrying on with the remaining files.
    """
    ensure_free(tmp.parent, _tree_size(content), reserve)

    def copy(src: str, dst: str, *, follow_symlinks: bool = True) -> str:
        ensure_free(tmp.parent, os.lstat(src).st_size, reserve)
        return shutil.copy2(src, dst, follow_symlinks=follow_symlinks)

    shutil.copytree(content, tmp, symlinks=True, copy_function=copy)


def _place_in_library(content: Path, tmp: Path, dest: Path, reserve: int = 0) -> None:
    """Put ``content`` at ``dest`` atomically: build it in ``tmp`` (same dir), then rename.

    Across devices the content is copied and the source is left for the caller to remove
    once the approval is recorded, so an interruption never loses data and never leaves a
    partial folder under the final name. The files' times are reset to now just before the
    final rename. Only a move across mounts (EXDEV) falls back to the copy; any other rename error is
    raised. The copy needs the release's size free again (plus ``reserve``); otherwise
    :class:`NoSpaceError` is raised and the release stays as it was.
    """
    _rmtree(tmp)  # leftover from an interrupted attempt
    moved = False
    try:
        os.rename(content, tmp)
        moved = True
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise  # only a move across mounts falls back to a copy
        try:
            _copy_into_library(content, tmp, reserve)
        except BaseException:
            _rmtree(tmp)
            raise
    try:
        _touch_files(tmp)
        os.rename(tmp, dest)
    except BaseException:
        if moved:
            try:
                os.rename(tmp, content)  # the release stays pending with its content intact
            except OSError:
                logger.exception("Could not move %s back to %s", tmp, content)
        else:
            _rmtree(tmp)
        raise


def encode_library_path(raw_rel: str) -> str:
    """The id the UI uses for a library path: the path itself, unless that would be ambiguous.

    A name that is not valid UTF-8 (surrogate escapes) cannot round-trip through JSON, and a real
    folder whose name starts with the token prefix would be taken for a token, so such paths are
    sent as a reversible token; :func:`decode_library_path` restores them.
    """
    if display_text(raw_rel) == raw_rel and not raw_rel.startswith(_ENCODED_PREFIX):
        return raw_rel
    token = base64.urlsafe_b64encode(raw_rel.encode("utf-8", "surrogatepass")).decode("ascii")
    return _ENCODED_PREFIX + token


def decode_library_path(rel: str) -> str:
    """Inverse of :func:`encode_library_path`; plain paths are returned unchanged.

    Strict: only the exact token :func:`encode_library_path` issues for a path is accepted, so an id
    maps to one path and a hand-made token (or a plain path that starts with the prefix, such as a
    stale page might send) never reaches another folder; it raises ``ValueError`` instead.
    """
    if isinstance(rel, str) and rel.startswith(_ENCODED_PREFIX):
        try:
            raw = base64.b64decode(
                rel[len(_ENCODED_PREFIX):].encode("ascii"), altchars=b"-_", validate=True
            ).decode("utf-8", "surrogatepass")
        except (ValueError, UnicodeError):
            raise ValueError("invalid path") from None
        if encode_library_path(raw) != rel:
            raise ValueError("invalid path")
        return raw
    return rel


def _validate_rel(rel: str, allow_empty: bool) -> list[str]:
    """Split a library-relative posix path from the UI into segments, or raise ValueError."""
    if not isinstance(rel, str):
        raise ValueError("path must be a string")
    if rel == "" and allow_empty:
        return []
    if not rel or "\\" in rel or "\x00" in rel:
        raise ValueError("invalid path")
    parts = rel.split("/")
    for part in parts:
        if part in ("", ".", "..") or part.startswith("."):
            raise ValueError("invalid or hidden path segment")
    return parts


def _resolve_library_path(library_dir: Path, rel: str, allow_empty: bool) -> Path:
    """Absolute path of ``rel`` below ``library_dir`` (blocking), refusing symlinks and escapes.

    Raises ValueError for a bad path, FileNotFoundError when it does not exist.
    """
    parts = _validate_rel(rel, allow_empty)
    root = os.fspath(library_dir)
    real_root = os.path.realpath(root)
    current = root
    for part in parts:
        current = os.path.join(current, part)
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            raise FileNotFoundError(rel) from None
        except OSError as exc:
            raise ValueError(f"cannot access {rel!r}: {exc.strerror or exc}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("symbolic links are not followed")
    real = os.path.realpath(current)
    try:
        inside = os.path.commonpath([real_root, real]) == real_root
    except ValueError:  # different drives
        inside = False
    if not inside or (parts and real == real_root):
        raise ValueError("path is outside the library")
    if not parts and not os.path.isdir(current):
        raise FileNotFoundError(rel)
    return Path(current)


def _walk_stats(path: str) -> tuple[int, int]:
    """(total bytes, audio file count) of the visible regular files below ``path``."""
    size = files = 0
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            try:
                info = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            size += info.st_size
            if os.path.splitext(name)[1].lower() in AUDIO_EXTS:
                files += 1
    return size, files


def _list_directory(target: Path, rel: str, shown_rel: str | None = None) -> dict[str, Any]:
    """One directory level of the library for the UI (blocking).

    ``rel`` is the raw relative path of ``target``; ``shown_rel`` (default ``rel``) is what the
    caller asked for and is echoed back as ``path``.
    """
    entries: list[dict[str, Any]] = []
    with os.scandir(target) as it:
        found = list(it)
    for entry in found:
        name = entry.name
        if name.startswith("."):
            continue
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISDIR(info.st_mode):
            size, files = _walk_stats(entry.path)
            is_dir = True
        elif stat.S_ISREG(info.st_mode):
            size = info.st_size
            files = 1 if os.path.splitext(name)[1].lower() in AUDIO_EXTS else 0
            is_dir = False
        else:
            continue  # symlinks and special files are not shown
        entries.append({
            "name": display_text(name),
            "path": encode_library_path(f"{rel}/{name}" if rel else name),
            "is_dir": is_dir,
            "size": size,
            "files": files,
            "mtime": int(info.st_mtime),
        })
    entries.sort(key=lambda e: (-e["mtime"], e["name"]))
    crumbs: list[dict[str, str]] = []
    prefix = ""
    for part in rel.split("/") if rel else ():
        prefix = f"{prefix}/{part}" if prefix else part
        crumbs.append({"name": display_text(part), "path": encode_library_path(prefix)})
    return {"path": rel if shown_rel is None else shown_rel, "crumbs": crumbs, "entries": entries}


def _delete_path(target: Path) -> None:
    """Delete a library file or folder (blocking); symlinks inside are removed, never followed."""
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)
    else:
        target.unlink()


def _iter_library_tracks(library_dir: Path, problems: list[str] | None = None,
                        subdirs: list[str] | None = None) -> Iterator[list[tuple[str, str]]]:
    """Hash the audio files below ``library_dir`` and yield (sha256, relpath) batches (blocking).

    A sub-folder that vanished during the walk is skipped, like the sync listing does. Anything
    else that could not be read is skipped too but appended to ``problems`` (when given), so the
    caller knows the index is incomplete. Trouble with ``library_dir`` itself is raised.
    ``subdirs`` (library-relative, ``/``-separated) limits the walk to those folders.
    """
    batch: list[tuple[str, str]] = []
    root = os.fspath(library_dir)
    starts = [root]
    if subdirs is not None:
        starts = []
        for folder in subdirs:
            parts = folder.split("/")
            if any(part in ("", ".", "..") or part.startswith(".") for part in parts):
                continue
            start = os.path.join(root, *parts)
            if not os.path.islink(start):
                starts.append(start)

    def onerror(exc: OSError) -> None:
        if exc.filename is None or os.path.abspath(os.fsdecode(exc.filename)) == os.path.abspath(root):
            raise exc
        if isinstance(exc, FileNotFoundError):
            logger.info("Not indexing %s: it vanished", exc.filename)
            return
        logger.warning("Cannot index %s: %s", exc.filename, exc)
        if problems is not None:
            problems.append(str(exc.filename))

    for start in starts:
        for dirpath, dirnames, filenames in os.walk(start, followlinks=False, onerror=onerror):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith(".")
                                 and not os.path.islink(os.path.join(dirpath, d)))
            for name in sorted(filenames):
                path = Path(dirpath) / name
                if name.startswith(".") or path.suffix.lower() not in AUDIO_EXTS or path.is_symlink():
                    continue
                try:
                    digest = sha256_file(path)
                except OSError as exc:
                    logger.warning("Cannot index %s: %s", path, exc)
                    continue
                batch.append((digest, path.relative_to(library_dir).as_posix()))
                if len(batch) >= _INDEX_BATCH:
                    yield batch
                    batch = []
    if batch:
        yield batch


def _present_tracks(library_dir: Path, batch: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """The (sha256, relpath) pairs of ``batch`` whose file still exists in the library (blocking)."""
    present: list[tuple[str, str]] = []
    for digest, rel in batch:
        try:
            info = os.lstat(os.path.join(os.fspath(library_dir), *rel.split("/")))
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode):
            present.append((digest, rel))
    return present


def _unread_folders(library_dir: Path, problems: list[str]) -> list[str] | None:
    """Library-relative, ``/``-separated paths of the folders in ``problems`` (sorted, no duplicates).

    None when one of them cannot be expressed that way (it does not lie below the library).
    """
    found: set[str] = set()
    root = os.fspath(library_dir)
    for path in problems:
        try:
            rel = os.path.relpath(path, root).replace(os.sep, "/")
        except ValueError:  # another drive
            return None
        if rel in (".", "") or rel == ".." or rel.startswith("../"):
            return None
        found.add(rel)
    return sorted(found)


def _indexed_key(library_dir: Path) -> str:
    """State key of the "this library was indexed" marker: one per library path."""
    real = os.path.realpath(os.fspath(library_dir))
    return f"{STATE_LIBRARY_INDEXED}:{hashlib.sha256(real.encode('utf-8', 'surrogatepass')).hexdigest()}"


def _is_under(path: str, root: str) -> bool:
    """``path`` is ``root`` or lies below it (plain string rule: no realpath, no case folding)."""
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _has_listable_file(library_dir: Path) -> bool:
    """Whether the library holds at least one file the sync API could list (blocking)."""
    return library_has_listable_file(library_dir)


def _probe_dir(path: Path) -> bool | OSError:
    """Whether ``path`` is a directory, or the OSError that says it cannot be read (blocking).

    ``Path.is_dir`` only swallows "missing"-type errors; a network share that is down answers
    ENODEV/EIO/ETIMEDOUT/EHOSTDOWN, which must not crash the caller.
    """
    try:
        return path.is_dir()
    except OSError as exc:
        return exc


def _restore_orphan_tmp(staging_path: str | None, tmp: Path) -> bool:
    """Deal with the hidden ``.mw-<id>.tmp`` folder of an interrupted approval (blocking).

    When the release's staged content is gone, ``tmp`` holds the only copy (it got there by one
    atomic rename), so it is moved back instead of deleted. When the staged content still exists,
    ``tmp`` is a partial copy and is removed. Returns False only if the content could not be moved
    back: ``tmp`` is then left alone.
    """
    if not tmp.is_dir():
        return True
    content = Path(staging_path) / "content" if staging_path else None
    if content is not None and content.is_dir():
        _rmtree(tmp)
        return True
    if content is None:
        logger.error("Keeping %s: its release has no staging directory", tmp)
        return False
    try:
        content.parent.mkdir(parents=True, exist_ok=True)
        os.rename(tmp, content)
    except OSError:
        logger.exception("Could not move %s back to %s; leaving it where it is", tmp, content)
        return False
    logger.warning("Moved the content of an interrupted approval back to %s", content)
    return True


def _repair_legacy(content: Path, report: dict[str, Any]) -> dict[str, Any]:
    """Bring a release cleaned by 0.1 up to the 0.2 name rules; returns the updated report (blocking)."""
    removed_hidden, renamed = repair_names(content)
    kept: list[list[str]] = []
    for rel, digest in report.get("kept_tracks", []):
        if any(name.startswith(".") or name == "__MACOSX" for name in rel.split("/")):
            continue  # removed with the hidden entries
        kept.append([map_renamed(rel, renamed), digest])
    return {
        **report,
        "kept_tracks": kept,
        "removed_hidden": [display_text(item) for item in removed_hidden],
        "renamed": [[display_text(old), display_text(new)] for old, new in renamed],
        "unsyncable": [rel for rel, _digest in kept if os.path.splitext(rel)[1].lower() not in SYNC_AUDIO_EXTS],
    }


class Controller:
    """Coordinates sources, the cleaner, the database and Home Assistant notifications."""

    def __init__(
        self,
        cfg: Config,
        db: Database,
        ha: HAClientLike,
        sources: list[Source],
        clock: Callable[[], float] = time.time,
        *,
        review_url: str = DEFAULT_REVIEW_URL,
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.ha = ha
        self.sources = list(sources)
        self.clock = clock
        self.review_url = review_url
        self._locks: dict[str, asyncio.Lock] = {}
        self._library_lock = asyncio.Lock()
        self.on_library_changed: list[Callable[[], None]] = []
        self.library_rev = 0  # counts changes of the library (approval, deletion); the web page lists the library again when it changes
        # called (loop thread) whenever an approval is refused because the library looks missing or
        # empty: the sync API re-lists then, so its status shows the state without a client request
        self.on_library_problem: list[Callable[[], None]] = []
        self.sync_status: Callable[[], dict] | None = None
        self.waiting_for_space = False
        self._space_needed: dict[str, tuple[int, float]] = {}  # candidate key -> (free bytes needed, since)
        self._need_owner: dict[str, str] = {}  # candidate key -> name of the source that offered it
        self._need_name: dict[str, str] = {}  # candidate key -> release name (shown in the status bar)
        self._space_warned: set[str] = set()  # releases whose approval already logged a space warning
        # The "library unavailable" notification is (possibly) showing. Kept in the state table, so a
        # restart still dismisses a notification the earlier process posted.
        self._library_alert = self._stored_library_alert()
        self._library_problem: str | None = None  # why approvals are refused right now (None: they are not)
        self._library_problem_logged: str | None = None  # the refusal the log already carries
        self._library_probe: asyncio.Future | None = None
        self._staging_in_library: bool | None = None  # staging_dir lies inside library_dir (worked out once)
        self._expiry_logged: set[str] = set()  # expired releases whose expiry was already logged
        self._tasks: set[asyncio.Task] = set()
        self._free: int | None = None  # last free-space probe of the staging disk
        self._free_probe: asyncio.Future | None = None
        self.activity: dict[str, Any] = {}
        self._set_activity("idle")
        self._current_source: Source | None = None
        self._wake = asyncio.Event()
        for source in self.sources:
            for attr, callback in (("set_progress", self._on_progress), ("set_space_check", self.space_needed)):
                setter = getattr(source, attr, None)
                if callable(setter):
                    try:
                        setter(callback)
                    except Exception:  # noqa: BLE001
                        logger.exception("Source %s rejected the %s callback", getattr(source, "name", source), attr)

    # ------------------------------------------------------------------ activity and status
    def _set_activity(self, state: str, **fields: Any) -> None:
        """Replace the activity snapshot; ``since`` only moves when the state changes."""
        previous = self.activity.get("state")
        since = self.activity.get("since") if previous == state else None
        self.activity = {
            "state": state, "source": None, "release": None, "file": None,
            "received": None, "total": None,
            "since": since if since is not None else self.clock(),
        }
        self.activity.update(fields)

    def _on_progress(self, info: dict[str, Any]) -> None:
        """Download progress reported by a source (runs on the event loop)."""
        self._set_activity(
            "downloading",
            source=info.get("source"), release=info.get("release"), file=info.get("file"),
            received=info.get("received"), total=info.get("total"),
        )

    def status(self) -> dict[str, Any]:
        """Everything the status bar of the UI shows."""
        sources: list[dict[str, Any]] = []
        for source in self.sources:
            try:
                sources.append(source.status())
            except Exception as exc:  # noqa: BLE001
                logger.exception("Source %s failed to report its status", getattr(source, "name", source))
                sources.append({"name": getattr(source, "name", "?"), "type": "unknown", "error": str(exc)})
        free = self._free  # never probe the disk here: this runs on the event loop
        waiting = self.waiting_for_space or any(item.get("waiting_for_space") for item in sources)
        horizon = self.clock() - 2 * 86400  # expired needs are only pruned by the scans
        need = max(
            ((needed, self._need_name.get(key)) for key, (needed, since) in self._space_needed.items()
             if since >= horizon),
            key=lambda item: item[0], default=None,
        )
        sync: dict[str, Any] = {"enabled": False, "listening": False, "reason": "disabled"}
        if self.sync_status is not None:
            try:
                sync = self.sync_status()
            except Exception:  # noqa: BLE001
                logger.exception("Sync status failed")
        return {
            "activity": dict(self.activity),
            "sources": sources,
            "disk": {
                "path": str(self.cfg.staging_dir),
                "free_bytes": free,
                "min_free_bytes": min_free_bytes(self.cfg),
                "waiting_for_space": waiting,
                # the biggest recorded need (it includes min_free) and the release it belongs to
                "needed_bytes": need[0] if need else None,
                "waiting_release": need[1] if need else None,
            },
            "pending_count": self.db.count(["pending"]),
            "library_rev": self.library_rev,
            "sync": sync,
            # why approvals are refused (library missing or unexpectedly empty), whatever the sync API
            # does; the UI offers "Confirm" for it
            "library_problem": self._library_problem,
        }

    async def refresh_free_space(self) -> int | None:
        """Probe the staging disk off the loop and remember the result (None: unknown or too slow).

        A probe that hangs (staging on network storage that went away) is waited for at most
        ``_FREE_PROBE_TIMEOUT`` seconds; no second probe starts while the first one still runs.
        """
        probe = self._free_probe
        if probe is None or probe.done():
            probe = asyncio.ensure_future(asyncio.to_thread(free_bytes, self.cfg.staging_dir))
            probe.add_done_callback(self._free_probed)
            self._free_probe = probe
        try:
            await asyncio.wait_for(asyncio.shield(probe), _FREE_PROBE_TIMEOUT)
        except (asyncio.TimeoutError, OSError):
            self._free = None
        return self._free

    def _free_probed(self, probe: asyncio.Future) -> None:
        if probe.cancelled():
            return
        self._free = probe.result() if probe.exception() is None else None

    async def status_async(self) -> dict[str, Any]:
        """:meth:`status` with a fresh free-space figure that cannot block the event loop."""
        await asyncio.gather(self.refresh_free_space(), self.refresh_library_problem())
        return self.status()

    async def refresh_library_problem(self) -> None:
        """While approvals are refused, look at the library again (off the loop, at most 3 s).

        A share that came back (or a folder that was filled) clears the problem without an approval
        attempt. The probe is cheap: ``is_dir`` and a walk that stops at the first listable file. A
        probe that hangs (storage gone) is waited for briefly and no second one starts meanwhile.
        """
        if self._library_problem is None:
            return
        probe = self._library_probe
        if probe is None or probe.done():
            probe = asyncio.ensure_future(self._probe_library())
            self._library_probe = probe
        try:
            await asyncio.wait_for(asyncio.shield(probe), _FREE_PROBE_TIMEOUT)
        except asyncio.TimeoutError:
            pass

    async def _probe_library(self) -> None:
        try:
            problem = await self._find_library_problem()
        except Exception:  # noqa: BLE001 - the next probe tries again
            logger.debug("Library probe failed", exc_info=True)
            return
        if problem is None:
            await self._library_ok()
        elif self._library_problem is not None:
            self._library_problem = problem

    def stop_current(self) -> bool:
        """Stop the download in progress (if any); returns whether a source was signalled."""
        source = self._current_source
        if source is None:
            return False
        try:
            return bool(source.cancel())
        except Exception:  # noqa: BLE001
            logger.exception("Source %s failed to cancel", getattr(source, "name", source))
            return False

    def request_poll(self, name: str | None = None) -> list[str]:
        """Make the named source (or every source) poll at the next scan, and start one now."""
        chosen = [s for s in self.sources if name is None or s.name == name]
        if name is not None and not chosen:
            raise KeyError(name)
        for source in chosen:
            source.force_poll()
        self._wake.set()
        return [s.name for s in chosen]

    def _library_changed(self) -> None:
        self.library_rev += 1
        for hook in list(self.on_library_changed):
            try:
                hook()
            except Exception:  # noqa: BLE001
                logger.exception("on_library_changed hook failed")

    def _library_refused(self) -> None:
        for hook in list(self.on_library_problem):
            try:
                hook()
            except Exception:  # noqa: BLE001
                logger.exception("on_library_problem hook failed")

    def _note_delete(self) -> None:
        """Record "the library was emptied on purpose" (a delete-all or Confirm).

        The guard compares it with ``sync:last_nonempty_at``, which the sync API stamps with its own
        clock: after a backward clock step that stamp can lie in the future, and a plain ``now`` would
        then leave the guard armed (Confirm doing nothing). The record is always newer than it.
        """
        now = float(self.clock())
        nonempty_at = self._state_float(STATE_LAST_NONEMPTY)
        if nonempty_at is not None and math.isfinite(nonempty_at):
            now = max(now, math.nextafter(nonempty_at, math.inf))
        self.db.set_state(STATE_LAST_DELETE, repr(now))

    def _stored_library_alert(self) -> bool:
        try:
            return self.db.get_state(STATE_LIBRARY_ALERT) == "1"
        except Exception:  # noqa: BLE001
            return False

    def _set_library_alert(self, showing: bool) -> None:
        self._library_alert = showing
        try:
            self.db.set_state(STATE_LIBRARY_ALERT, "1" if showing else "0")
        except Exception:  # noqa: BLE001 - only the dismissal after a restart depends on it
            logger.debug("Could not store the library alert flag", exc_info=True)

    async def _library_ok(self) -> None:
        """Approvals are possible: forget the problem and dismiss the notification (if it shows)."""
        self._library_problem = None
        self._library_problem_logged = None
        if self._library_alert:
            self._set_library_alert(False)
            await self._safe("persistent_dismiss", self.ha.persistent_dismiss(LIBRARY_UNAVAILABLE_NOTIFICATION))

    def confirm_empty_library(self) -> dict[str, Any]:
        """The user confirms that an empty library is intended (lifts the sync API's guard).

        Also lifts the approval guard: the problem is cleared at once and its notification dismissed.
        """
        self._note_delete()
        self._library_problem = None
        self._library_problem_logged = None
        if self._library_alert:
            try:
                task = asyncio.get_running_loop().create_task(
                    self._safe("persistent_dismiss", self.ha.persistent_dismiss(LIBRARY_UNAVAILABLE_NOTIFICATION))
                )
            except RuntimeError:  # no loop: the next passing check dismisses it
                pass
            else:
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
                self._set_library_alert(False)
        self._library_changed()
        return {"ok": True}

    async def library_listing(self, rel: str = "") -> dict[str, Any]:
        """One directory level of the library: ``{"path", "entries": [...]}``, newest first."""
        raw = decode_library_path(rel)  # the UI sends a token for names that are not valid UTF-8
        try:
            target = await asyncio.to_thread(_resolve_library_path, self.cfg.library_dir, raw, True)
        except FileNotFoundError:
            if raw == "":  # the library folder does not exist yet: nothing to show
                return {"path": "", "entries": []}
            raise
        if not await asyncio.to_thread(target.is_dir):
            raise ValueError("not a directory")
        return await asyncio.to_thread(_list_directory, target, raw, rel)

    async def delete_library_path(self, rel: str) -> dict[str, str]:
        """Delete one file or folder of the library (never the library itself).

        The release it came from (or just the deleted tracks of it) is forgotten too, so the same
        archive can be imported again and its tracks are no longer treated as known.
        """
        raw = decode_library_path(rel)
        async with self._library_lock:
            target = await asyncio.to_thread(_resolve_library_path, self.cfg.library_dir, raw, False)
            await asyncio.to_thread(_delete_path, target)
            # Lift the sync API's empty-library guard only when this deletion left nothing to list:
            # after a partial deletion an empty library still means "storage gone" (503), not "emptied".
            if not await asyncio.to_thread(_has_listable_file, self.cfg.library_dir):
                self._note_delete()
            try:
                await self._forget_library_path(_validate_rel(raw, False))
            except Exception:  # noqa: BLE001 - the files are gone; the bookkeeping must not fail the call
                logger.exception("Could not forget the releases of the deleted path %s", display_text(raw))
        logger.info("Deleted %s from the library", display_text(raw))
        self._library_changed()
        return {"deleted": rel}

    async def _forget_library_path(self, parts: list[str]) -> None:
        """Drop what the database knows about the deleted library path ``parts`` (loop thread)."""
        library = os.fspath(self.cfg.library_dir)
        target = os.path.join(library, *parts)
        rel = "/".join(parts)
        for release in self.db.list(statuses=["approved"], limit=100_000):
            if not release.library_path:
                continue
            if _is_under(release.library_path, target):
                # the release folder (or a folder holding it) is gone
                self.db.delete(release.id)
                logger.info("Forgot release %s (%s): it was deleted from the library", release.id, release.name)
            elif _is_under(target, release.library_path):
                # one file or sub-folder of the release is gone: forget those tracks only, and let
                # the same archive bring them back (the other tracks are still known)
                inner = os.path.relpath(target, release.library_path).replace(os.sep, "/")
                self.db.delete_tracks_under(release.id, inner)
                self.db.forget_source_hash(release.id)
                logger.info("Forgot the deleted tracks %s of release %s (%s)", inner, release.id, release.name)
        for owner in (LIBRARY_RELEASE_ID, RSS_IMPORT_RELEASE_ID):
            self.db.delete_tracks_under(owner, rel)
        # An archive that brought nothing new ('empty') was judged by the tracks known back then, and the
        # deleted ones may have been those: let the same archive be looked at once more.
        for release in self.db.list(statuses=["empty"], limit=100_000):
            self.db.forget_source_hash(release.id)
        self._retire_name(parts[0])

    def _used_names(self) -> list[str]:
        try:
            value = json.loads(self.db.get_state(STATE_USED_NAMES) or "[]")
        except ValueError:
            return []
        return [str(item) for item in value] if isinstance(value, list) else []

    def _retire_name(self, name: str) -> None:
        """Remember a top-level library name that was published, so it is not handed out again.

        Sync clients mirror the library but never replace a file that already exists under the
        same name (SYNC_PROTOCOL 7), so a new release must get a new folder name even after the old
        one was deleted. Bounded: only hashes of the most recent names are kept.
        """
        token = _name_token(name)
        tokens = [item for item in self._used_names() if item != token]
        tokens.append(token)
        self.db.set_state(STATE_USED_NAMES, json.dumps(tokens[-_USED_NAMES_CAP:]))

    async def index_library(self) -> None:
        """Remember the hash of every audio file already in the library (once per library path)."""
        library = self.cfg.library_dir
        key = _indexed_key(library)
        retry_key = f"{STATE_LIBRARY_RETRY}:{key.rsplit(':', 1)[1]}"
        indexed = self.db.get_state(key) is not None
        retry = self._folders_to_retry(retry_key) if indexed else []
        if indexed and not retry:
            return
        if not await asyncio.to_thread(library.is_dir):
            logger.info("Library %s does not exist yet; not indexing it", library)
            return
        self._set_activity("indexing")
        count = 0
        problems: list[str] = []
        try:
            # a library that was indexed before is not hashed again: only the folders that could not
            # be read then are walked once more
            batches = _iter_library_tracks(library, problems, retry) if indexed else _iter_library_tracks(library, problems)
            while True:
                batch = await asyncio.to_thread(next, batches, None)
                if batch is None:
                    break
                # The hashing above runs without the library lock (it can take hours); a deletion made
                # meanwhile must not be undone, so each batch is added under the lock, minus the files
                # that are gone by now.
                async with self._library_lock:
                    present = await asyncio.to_thread(_present_tracks, library, batch)
                    self.db.add_tracks(LIBRARY_RELEASE_ID, present)
                count += len(batch)
            unread = await asyncio.to_thread(_unread_folders, library, problems)
            if indexed:
                if unread is None:  # cannot happen (every folder of the walk lies below the library)
                    unread = retry
                self.db.set_state(retry_key, json.dumps(unread))
                if unread:
                    logger.warning("Indexed %d audio file(s) of %d folder(s) that could not be read before; "
                                   "%d still cannot be read (tried again at the next start)",
                                   count, len(retry), len(unread))
                else:
                    logger.info("Indexed %d audio file(s) of the folders that could not be read before", count)
            elif count == 0:
                logger.info("Library %s holds no audio yet; it is looked at again at the next start", library)
            elif unread is None:
                logger.warning("Indexed %d audio file(s) of the library, but %d folder(s) could not be read; "
                               "indexing runs again at the next start", count, len(problems))
            else:
                self.db.set_state(key, repr(float(self.clock())))
                if unread:
                    # Not hashing the whole library again at every start: only these folders are retried.
                    self.db.set_state(retry_key, json.dumps(unread))
                    logger.warning("Indexed %d audio file(s) of the library, but %d folder(s) could not be read; "
                                   "they are tried again at the next start", count, len(unread))
                else:
                    logger.info("Indexed %d audio file(s) of the library", count)
        finally:
            self._set_activity("idle")

    def _folders_to_retry(self, retry_key: str) -> list[str]:
        try:
            value = json.loads(self.db.get_state(retry_key) or "[]")
        except ValueError:
            return []
        return [str(item) for item in value] if isinstance(value, list) else []

    # ------------------------------------------------------------------ helpers
    def _lock(self, release_id: str) -> asyncio.Lock:
        lock = self._locks.get(release_id)
        if lock is None:
            lock = self._locks[release_id] = asyncio.Lock()
        return lock

    @staticmethod
    def _notification_id(release_id: str) -> str:
        return f"car_media_hub_{release_id}"

    async def _safe(self, what: str, coro: Any) -> None:
        """Await a notification coroutine; failures are logged, never raised."""
        try:
            await coro
        except Exception:  # noqa: BLE001
            logger.exception("Home Assistant call failed (%s)", what)

    async def _finalize(self, source: Source, cand: Candidate, outcome: Outcome) -> None:
        try:
            await source.finalize(cand, outcome)
        except Exception:  # noqa: BLE001
            logger.exception("Source %s failed to finalize %s as %s", source.name, cand.name, outcome)

    def _known_track_checker(self) -> Callable[[str], bool]:
        """Return a thread-safe ``is_known_track`` that queries the DB on the loop thread."""
        loop = asyncio.get_running_loop()

        async def _has(digest: str) -> bool:
            return self.db.has_track(digest)

        def check(digest: str) -> bool:
            return asyncio.run_coroutine_threadsafe(_has(digest), loop).result()

        return check

    def _pending_message(self, release: Release, note: str | None = None) -> str:
        report = release.report
        kept = len(report.get("kept_tracks", []))
        lines = [
            f"**{md_escape(release.name)}**",
            "",
            f"- Tracks kept: {kept}",
            f"- Total size: {human_size(report.get('total_bytes', 0))}",
        ]
        for key, label in _REMOVAL_LABELS:
            count = len(report.get(key, []))
            if count:
                lines.append(f"- Removed ({label}): {count}")
        if report.get("renamed"):
            lines.append(f"- Renamed: {len(report['renamed'])}")
        if report.get("unsyncable"):
            lines.append(f"- Not synced (format): {len(report['unsyncable'])}")
        if report.get("other_files"):
            lines.append(f"- Other files: {len(report['other_files'])}")
        # creator/license/url come from (untrusted) feed or site metadata: escape everything
        if report.get("creator"):
            lines.append(f"- Creator: {md_escape(report['creator'])}")
        if report.get("license"):
            lines.append(f"- License: {md_escape(report['license'])}")
        if report.get("url"):
            lines.append(f"- Source: {md_escape(report['url'])}")
        if note:
            lines.append(f"- Not approved automatically: {md_escape(note)}")
        lines += ["", f"[Review in Car Media and Trips]({self.review_url})"]
        return "\n".join(lines)

    async def _notify_pending(self, release: Release, note: str | None = None) -> None:
        nid = self._notification_id(release.id)
        await self._safe(
            "persistent_create",
            self.ha.persistent_create(
                nid, f"New music: {_flat(release.name)}", self._pending_message(release, note)
            ),
        )
        if self.cfg.notify_service:
            kept = len(release.report.get("kept_tracks", []))
            data = {
                "actions": [
                    {"action": f"{_APPROVE_PREFIX}{release.id}", "title": "Approve"},
                    {"action": f"{_DECLINE_PREFIX}{release.id}", "title": "Decline"},
                ],
                "tag": nid,
            }
            await self._safe(
                "notify",
                self.ha.notify(
                    self.cfg.notify_service,
                    f"New music: {_flat(release.name)}",
                    f"{kept} track(s), {human_size(release.report.get('total_bytes', 0))}",
                    data,
                ),
            )

    async def _clear_notifications(self, release_id: str) -> None:
        nid = self._notification_id(release_id)
        await self._safe("persistent_dismiss", self.ha.persistent_dismiss(nid))
        if self.cfg.notify_service:
            await self._safe(
                "notify clear",
                self.ha.notify(self.cfg.notify_service, "", "clear_notification", {"tag": nid}),
            )

    # ------------------------------------------------------------------ scanning
    async def _refresh_space_flag(self) -> None:
        """Clear ``waiting_for_space`` once the free space is back (nothing else would reset it)."""
        if not self.waiting_for_space:
            return
        try:
            free = await asyncio.to_thread(free_bytes, self.cfg.staging_dir)
        except OSError:
            return
        if free >= max(min_free_bytes(self.cfg), self._largest_space_need()):
            logger.info("Free space is back above the limit")
            self.waiting_for_space = False

    def _largest_space_need(self) -> int:
        """Free space the biggest deferred release is waiting for (needs older than 2 days expire)."""
        horizon = self.clock() - 2 * 86400
        for key in [k for k, (_need, since) in self._space_needed.items() if since < horizon]:
            del self._space_needed[key]
            self._need_owner.pop(key, None)
            self._need_name.pop(key, None)
        return max((need for need, _since in self._space_needed.values()), default=0)

    def space_needed(self, key: str) -> int | None:
        """Free bytes the deferred candidate ``key`` waits for, or None (sources may skip it until then)."""
        entry = self._space_needed.get(key)
        return entry[0] if entry else None

    async def scan_once(self) -> None:
        """Poll every source and process each candidate sequentially."""
        await asyncio.gather(self.refresh_free_space(), self.refresh_library_problem())
        self._largest_space_need()  # needs expire after 2 days whether or not the flag is set
        await self._refresh_space_flag()
        if await self._staging_blocked():
            return  # sources are polled again (they are not consumed) once the library exists or is confirmed
        try:
            for source in self.sources:
                self._current_source = source
                self._set_activity("polling", source=getattr(source, "name", None))
                try:
                    candidates = await source.poll()
                except Exception:  # noqa: BLE001
                    logger.exception("Polling source %s failed", getattr(source, "name", source))
                    continue
                if getattr(source, "offers_deferred_again", False):
                    self._forget_needs_not_offered(source, candidates)
                for cand in candidates:
                    try:
                        await self.process(source, cand)
                    except Exception:  # noqa: BLE001 - process already handles its own errors
                        logger.exception("Unexpected error processing %s", cand.name)
        finally:
            self._current_source = None
            self._set_activity("idle")

    def _forget_needs_not_offered(self, source: Source, candidates: list[Candidate]) -> None:
        """A source that offers a deferred entry at every poll did not offer it: it was removed."""
        offered = {cand.key or str(cand.path) for cand in candidates}
        name = getattr(source, "name", None)
        for key in [k for k, owner in self._need_owner.items() if owner == name and k not in offered]:
            del self._need_owner[key]
            self._need_name.pop(key, None)
            self._space_needed.pop(key, None)

    def _space_is_back(self) -> None:
        if self.waiting_for_space and not self._space_needed:
            logger.info("Enough free space again; processing resumes")
            self.waiting_for_space = False

    async def _defer(self, source: Source, cand: Candidate, release_id: str | None,
                     staging: Path | None, reason: object) -> None:
        """Not enough space: drop the partial work and let the source offer the release again later."""
        await asyncio.to_thread(_rmtree, staging)
        if release_id is not None:
            try:
                if self.db.get(release_id) is not None:
                    self.db.delete(release_id)
            except Exception:  # noqa: BLE001 - e.g. the disk is full: the next scan drops the stale row
                logger.exception("Could not drop release %s (%s); it is dropped when %s is offered again",
                                 release_id, cand.name, cand.name)
        if not self.waiting_for_space:
            logger.warning("Waiting for free space before importing %s: %s", cand.name, reason)
        self.waiting_for_space = True
        await self._finalize(source, cand, "deferred")

    async def process(self, source: Source, cand: Candidate) -> Release | None:
        """Import one candidate. Returns the release (pending/empty/failed) or None for duplicates."""
        release_id: str | None = None
        staging: Path | None = None
        name = display_text(cand.name)  # file names may carry undecodable bytes
        min_free = min_free_bytes(self.cfg)
        self._set_activity("processing", source=getattr(source, "name", None), release=name)
        key = cand.key or str(cand.path)
        need_since: float | None = None  # when the need this candidate is retried after was first recorded
        if await self._staging_blocked():
            # the library went away during this scan: creating staging would create it (see scan_once)
            await self._finalize(source, cand, "deferred")
            self._set_activity("idle")
            return None
        try:
            await asyncio.to_thread(ensure_free, self.cfg.staging_dir, 0, min_free)
            wanted = self.space_needed(key)
            if wanted is not None:
                # an earlier attempt found the release too big for the free space: stay put (no
                # hashing, no extraction) until the space it needs is there
                free = await asyncio.to_thread(free_bytes, self.cfg.staging_dir)
                if free < wanted:
                    raise NoSpaceError(f"{human_bytes(wanted)} must be free, {human_bytes(free)} are",
                                       needed=wanted)
                popped = self._space_needed.pop(key, None)
                need_since = popped[1] if popped else None
                self._need_owner.pop(key, None)
                self._need_name.pop(key, None)
            if cand.is_dir and not getattr(cand, "disposable", False):
                # cheap (lstat only), so a folder that cannot be copied is not hashed first
                size = await asyncio.to_thread(_tree_size, Path(cand.path))
                await asyncio.to_thread(ensure_free, self.cfg.staging_dir, size, min_free)
            source_hash = await asyncio.to_thread(hash_path, cand.path)
            existing = self.db.get_by_source_hash(source_hash)
            if existing is not None and existing.status == "failed":
                # Nothing was imported: retry instead of treating (and deleting) it as a
                # duplicate. Sources bound retries (inbox: on change/restart; RSS:
                # MAX_FAILURES).
                logger.info("Retrying %s: previous attempt (release %s) failed: %s",
                            cand.name, existing.id, existing.error)
                self.db.delete(existing.id)
                existing = None
            elif existing is not None and existing.status == "processing":
                # Releases are processed one at a time, so this one was left behind by an attempt that
                # died before it could record its outcome (a database error while handling a full
                # disk, say). Nothing was imported: handle it like recover() does and start afresh,
                # instead of deleting the untouched source as a "duplicate".
                logger.warning("Reprocessing %s: release %s was left in 'processing' by an earlier attempt",
                               cand.name, existing.id)
                await asyncio.to_thread(_rmtree, existing.staging_path)
                self.db.delete(existing.id)
                existing = None
            if existing is not None:
                logger.info("Skipping %s: already seen as release %s (%s)",
                            cand.name, existing.id, existing.status)
                if existing.status != "pending":  # a pending one already has its review notification
                    # never let a re-added entry disappear without a trace (one note per earlier release)
                    await self._safe(
                        "persistent_create",
                        self.ha.persistent_create(
                            f"car_media_hub_duplicate_{existing.id}",
                            "Car Media and Trips: already imported",
                            f"**{md_escape(name)}** was skipped: it is the same as the earlier release "
                            f"**{md_escape(existing.name)}** ({md_escape(existing.status)})."
                            + (" That release brought nothing new at the time; delete something in the "
                               "Library section and drop the file again to have it looked at once more."
                               if existing.status == "empty" else ""),
                        ),
                    )
                await self._finalize(source, cand, "duplicate")
                return None

            release_id = uuid.uuid4().hex[:12]
            staging = self.cfg.staging_dir / release_id
            async with self._lock(release_id):
                self.db.create_release(release_id, name, source_hash, str(staging))
                logger.info("Processing %s as release %s", cand.name, release_id)
                content = staging / "content"
                await asyncio.to_thread(staging.mkdir, parents=True, exist_ok=True)
                await asyncio.to_thread(_populate_content, cand, content, min_free)
                report_obj = await asyncio.to_thread(
                    clean_release, content, self.cfg, self._known_track_checker()
                )
                report = {**report_obj.to_dict(), "source": source.name, **cand.meta}
                if getattr(source, "manual_only", False):
                    report["manual_only"] = True  # kept with the release: stays true when the source is later removed

                if not report_obj.kept_tracks:
                    await asyncio.to_thread(_rmtree, staging)
                    self.db.update(release_id, status="empty", report=report,
                                   staging_path=None, decided_at=self.clock())
                    logger.info("Release %s (%s) has no new audio", release_id, cand.name)
                    if getattr(source, "notify_empty", True):
                        await self._safe(
                            "persistent_create",
                            self.ha.persistent_create(
                                self._notification_id(release_id),
                                "Car Media and Trips: nothing new",
                                f"nothing new in {md_escape(name)}",
                            ),
                        )
                    self._space_is_back()
                    await self._finalize(source, cand, "empty")
                    return self.db.get(release_id)

                self.db.update(release_id, status="pending", report=report)
                release = self.db.get(release_id)
                assert release is not None
            logger.info("Release %s (%s) is pending review: %d track(s)",
                        release_id, cand.name, len(report_obj.kept_tracks))
            self._space_is_back()
            if getattr(source, "auto_approve", False) and not getattr(source, "manual_only", False):
                try:
                    release = await self.approve(release_id)
                except (InvalidState, NoSpaceError) as exc:  # fall back to asking the user
                    logger.warning("Auto-approval of release %s did not happen: %s", release_id, exc)
                    await self._notify_pending(release, note=str(exc))
                except Exception:  # noqa: BLE001 - fall back to asking the user
                    logger.exception("Auto-approval of release %s failed; asking for review", release_id)
                    await self._notify_pending(release)
            else:
                await self._notify_pending(release)
            await self._finalize(source, cand, "done")
            return release
        except (NoSpaceError, ExtractionNoSpace) as exc:
            needed = getattr(exc, "needed", None)
            if needed:  # remember what it takes, so the next scans do not redo the work in vain
                # A dip below min_free (an approval copy, a backup) raises NoSpaceError(min_free) before
                # the recorded need is looked at: keep the bigger need, and when it was first recorded.
                old = self._space_needed.get(key)
                if old is not None:
                    self._space_needed[key] = (max(needed, old[0]), old[1])
                else:
                    # deferred again after a retry: keep when the wait began, so a release that is
                    # offered at every poll (RSS, inbox) still has its need expire after 2 days
                    self._space_needed[key] = (needed, need_since if need_since is not None else self.clock())
                self._need_owner[key] = getattr(source, "name", "")
                self._need_name[key] = name
            await self._defer(source, cand, release_id, staging, exc)
            return None
        except Exception as exc:  # noqa: BLE001
            if is_no_space_error(exc):
                await self._defer(source, cand, release_id, staging, exc)
                return None
            logger.exception("Processing %s failed", cand.name)
            await asyncio.to_thread(_rmtree, staging)
            try:
                if release_id is not None and self.db.get(release_id) is not None:
                    self.db.update(release_id, status="failed", error=display_text(str(exc)) or type(exc).__name__,
                                   staging_path=None, decided_at=self.clock())
            except Exception:  # noqa: BLE001 - the source must still hear that it failed
                logger.exception("Could not record the failure of release %s; it is dropped when %s is "
                                 "offered again", release_id, cand.name)
            await self._safe(
                "persistent_create",
                self.ha.persistent_create(
                    self._notification_id(release_id or "error"),
                    "Car Media and Trips: import failed",
                    f"Importing **{md_escape(name)}** failed: {md_escape(exc)}",
                ),
            )
            await self._finalize(source, cand, "failed")
            return self.db.get(release_id) if release_id else None
        finally:
            if self.activity.get("state") == "processing":
                self._set_activity("idle")

    # ------------------------------------------------------------------ decisions
    def _require_pending(self, release_id: str) -> Release:
        release = self.db.get(release_id)
        if release is None:
            raise KeyError(release_id)
        if release.status != "pending":
            raise InvalidState(f"release {release_id} is {release.status}, not pending")
        return release

    def _record_approval(self, release: Release, dest: Path) -> int:
        """Record an approval once ``dest`` holds the content; returns the track count.

        Runs on the loop thread without awaiting, and the caller holds ``_library_lock``: a Library
        deletion can then not slip in between the placement and this record (it would find the
        release still pending, forget nothing, and leave its tracks known). The staged copy is only
        removed afterwards (:meth:`_drop_staging`), so a crash in between leaves nothing worse
        than a leftover folder that :meth:`recover` removes.
        """
        tracks = [(digest, rel) for rel, digest in release.report.get("kept_tracks", [])]
        self.db.add_tracks(release.id, tracks)
        self.db.update(release.id, status="approved", decided_at=self.clock(), library_path=str(dest))
        logger.info("Approved release %s -> %s", release.id, dest)
        self._retire_name(dest.name)
        self._library_changed()
        return len(tracks)

    async def _drop_staging(self, release: Release) -> None:
        """Remove the staged copy of an approved release; a failure only leaves it behind (logged)."""
        if not release.staging_path:
            return
        try:
            await asyncio.to_thread(_rmtree, release.staging_path)
            self.db.update(release.id, staging_path=None)
        except Exception:  # noqa: BLE001 - the approval is recorded; recover() retries at the next start
            logger.exception("Could not remove the staged copy %s of approved release %s",
                             release.staging_path, release.id)

    async def _complete_approval(self, release: Release, dest: Path) -> int:
        """Final step of an approval once ``dest`` holds the content; returns the track count."""
        async with self._library_lock:
            count = self._record_approval(release, dest)
        await self._drop_staging(release)
        return count

    def _state_float(self, key: str) -> float | None:
        try:
            value = self.db.get_state(key)
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    async def _find_library_problem(self) -> str | None:
        """Why approving into the library is refused right now, or None (no side effects; loop thread).

        The sync API answers 503 while the library is missing or unexpectedly empty, so the car app
        keeps its files. Approving into such a place would fill it with one release, the listing
        would turn non-empty again and the car app would delete everything else. Same rule as the
        sync API's empty-library guard; the user lifts it with "Confirm" (a recorded deletion).
        A fresh install (nothing published yet) is not affected: the first approval creates the
        folder, except while the sync API is enabled (then a missing library_dir is refused until it
        is created or the user confirms it). The rule does not depend on ``sync_enabled`` otherwise:
        an approval into an empty mount point would turn into a short list as soon as sync is on again.
        """
        library = Path(self.cfg.library_dir)
        last_count = int(self._state_float(STATE_LAST_COUNT) or 0)
        nonempty_at = self._state_float(STATE_LAST_NONEMPTY) or 0.0
        deleted = self._state_float(STATE_LAST_DELETE)
        confirmed = deleted is not None and deleted >= nonempty_at  # "empty is intended" came last
        problem: str | None = None
        if not confirmed:
            is_dir = await asyncio.to_thread(_probe_dir, library)
            if isinstance(is_dir, OSError):
                # a network share that is down (ENODEV/EIO/ETIMEDOUT/EHOSTDOWN), a permission error:
                # nothing can be approved into it, and confirming would not help ("cannot be read"
                # keeps the UI from offering Confirm)
                problem = f"the library folder {library} cannot be read ({is_dir.strerror or is_dir})"
            elif not is_dir:
                if last_count > 0 or self._has_release_under(library):
                    problem = f"the library folder {library} is missing or not a folder"
                elif self.cfg.sync_enabled:
                    # Sync clients mirror the list: creating a mistyped or unmounted library_dir and
                    # publishing one release there would make them delete everything else they hold.
                    problem = (f"library_dir {library} does not exist; create it or fix the option "
                               "(or confirm in Car Media and Trips that it is right)")
            elif last_count > 0 and not await asyncio.to_thread(_has_listable_file, library):
                problem = f"the library folder {library} is unexpectedly empty"
        return problem

    async def _check_library_available(self) -> None:
        """Refuse to approve into a library that looks missing or unmounted (loop thread).

        See :meth:`_find_library_problem` for the rule. A refusal is remembered (``status()`` shows it
        and the UI offers "Confirm" whatever the sync API does), posted as a persistent notification,
        and passed to ``on_library_problem`` so the sync API re-lists and shows it too.
        """
        problem = await self._find_library_problem()
        if problem is None:
            await self._library_ok()
            return
        await self._library_refusal(problem, "Not approving")
        raise InvalidState(f"library unavailable: {problem}")

    async def _library_refusal(self, problem: str, what: str) -> None:
        """Remember, log (once per problem) and announce that ``problem`` holds work back (loop thread)."""
        self._library_problem = problem
        if problem != self._library_problem_logged:
            # an unchanged refusal is not logged again: expire_pending and the scans retry it
            self._library_problem_logged = problem
            logger.warning("%s: %s", what, problem)
        else:
            logger.debug("Still held back (%s): %s", what, problem)
        if not self._library_alert:
            self._set_library_alert(True)
            await self._safe(
                "persistent_create",
                self.ha.persistent_create(
                    LIBRARY_UNAVAILABLE_NOTIFICATION,
                    "Car Media and Trips: library unavailable",
                    f"Releases wait because {md_escape(problem)}. Check the library_dir setting "
                    "and the storage behind it. If the library is really meant to be empty or moved, "
                    "press Confirm in the Car Media and Trips status bar.",
                ),
            )
        self._library_refused()

    async def _staging_blocked(self) -> bool:
        """True while staging_dir lies inside a missing library_dir that must not be created (loop thread).

        Creating the staging folder (an import, a download) would create the library as its parent,
        and the sync API would serve the empty folder as ``200 []`` instead of answering 503. So nothing
        is polled, downloaded or imported until the library exists or the user confirms it (the same
        rule as the approval refusal, see :func:`car_media_hub.config.library_blocks`).
        """
        if not self.cfg.sync_enabled:
            return False
        try:
            if self._staging_in_library is None:  # the layout does not change: work it out once
                relation = await asyncio.to_thread(path_relation, self.cfg.staging_dir, self.cfg.library_dir)
                self._staging_in_library = relation in ("same", "inside")
            if not self._staging_in_library:
                return False  # the usual layout: no stat of a library that may be a dead mount
            blocked = await asyncio.to_thread(library_blocks, self.cfg, self.cfg.staging_dir, self.db.get_state)
        except Exception:  # noqa: BLE001 - a broken check must not stop imports for good
            logger.debug("Checking whether staging is blocked failed", exc_info=True)
            return False
        if blocked:
            # the plain problem text: the off-loop probe (refresh_library_problem) finds the same one
            problem = await self._find_library_problem() or f"library_dir {self.cfg.library_dir} does not exist"
            await self._library_refusal(
                problem, f"Not downloading or importing anything (staging_dir {self.cfg.staging_dir} lies "
                "inside library_dir and would create it)")
        return blocked

    def _reserved_names(self, release_id: str) -> set[str]:
        """name_keys of the top-level library folders that other pending releases have picked.

        An interrupted approval keeps its ``library_path`` until recover() or the next approve() resets
        it, though its folder may not exist yet (the copy was cut short): another release must not take
        that name, or the first one would later be completed into the wrong folder.
        """
        root = os.path.normpath(os.fspath(self.cfg.library_dir))
        keys: set[str] = set()
        for other in self.db.list(statuses=["pending"], limit=100_000):
            if other.id == release_id or not other.library_path:
                continue
            parent, leaf = os.path.split(other.library_path)
            if os.path.normpath(parent) == root:
                keys.add(name_key(leaf))
        return keys

    def _has_release_under(self, library: Path) -> bool:
        root = os.fspath(library)
        return any(
            release.library_path and _is_under(release.library_path, root)
            for release in self.db.list(statuses=["approved"], limit=100_000)
        )

    async def approve(self, release_id: str) -> Release:
        """Move a pending release into the library and remember its tracks."""
        async with self._lock(release_id):
            release = self._require_pending(release_id)
            placed = await asyncio.to_thread(_probe_dir, Path(release.library_path)) if release.library_path else False
            if isinstance(placed, OSError):
                # the library cannot be read (a share that is down): leave the interrupted approval as
                # it is (library_path kept) and try again later
                raise InvalidState(f"library unavailable: the library folder {self.cfg.library_dir} cannot be "
                                   f"read ({placed.strerror or placed})")
            if placed:
                # An earlier approval was interrupted after the content reached the library.
                dest = Path(release.library_path)
                count = await self._complete_approval(release, dest)
            else:
                if release.library_path:
                    # ... or before: the content may sit in the hidden tmp folder (same-mount rename)
                    restored = await asyncio.to_thread(
                        _restore_orphan_tmp, release.staging_path,
                        _approval_tmp(Path(release.library_path).parent, release.id),
                    )
                    if restored:
                        # The content is in staging again and the recorded folder does not exist:
                        # forget it, so it is neither completed into whatever folder gets that name
                        # later nor reserved against the next release.
                        self.db.update(release.id, library_path=None)
                        release.library_path = None
                if not release.staging_path:
                    raise InvalidState(f"release {release_id} has no staging directory")
                content = Path(release.staging_path) / "content"
                if not await asyncio.to_thread(content.is_dir):
                    raise InvalidState(f"staging content of release {release_id} is missing")
                name = sanitize_name(release.name, release.id)
                async with self._library_lock:  # no two approvals pick the same folder name
                    await self._check_library_available()
                    dest = await asyncio.to_thread(
                        _pick_destination, self.cfg.library_dir, name, frozenset(self._used_names()),
                        frozenset(self._reserved_names(release.id)),
                    )
                    # Record the target first so recover() can finish an interrupted approval.
                    self.db.update(release.id, library_path=str(dest))
                    # The copy is temporary (staging is freed right after it), so it only has to keep
                    # the disk from running full (the last 1 GiB, as extraction does), not the whole
                    # min_free reserve, which downloads may already have used up to: otherwise nearly
                    # every approval would be refused.
                    reserve = DISK_RESERVE_BYTES
                    worker = asyncio.ensure_future(asyncio.to_thread(
                        _place_in_library, content, _approval_tmp(self.cfg.library_dir, release.id), dest, reserve
                    ))
                    try:
                        await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        # The thread cannot be stopped. Wait for it while the locks are still held, so
                        # no second approval races it; the file system then says what happened and
                        # recover() / the next approve() go by that (library_path stays as it is).
                        await self._wait_for_worker(worker)
                        failed = worker.done() and not worker.cancelled() and worker.exception() is not None
                        if failed and await asyncio.to_thread(content.is_dir):
                            self.db.update(release.id, library_path=None)
                        raise
                    except Exception:
                        # the worker is done and put the content back; keep library_path only when
                        # that failed too, so recover() can find the content in the tmp folder
                        if await asyncio.to_thread(content.is_dir):
                            self.db.update(release.id, library_path=None)
                        raise
                    # recorded before the lock is released (see _record_approval)
                    count = self._record_approval(release, dest)
                await self._drop_staging(release)
        await self._clear_notifications(release.id)
        await self._safe(
            "persistent_create",
            self.ha.persistent_create(
                f"car_media_hub_approved_{release.id}",
                f"Approved: {_flat(release.name)}",
                f"Approved: {md_escape(release.name)} ({count} track(s)) moved to {md_escape(dest)}.",
            ),
        )
        # a Library deletion may have forgotten the release since it was recorded: still report the approval
        result = self.db.get(release.id) or replace(
            release, status="approved", library_path=str(dest), staging_path=None
        )
        return result

    @staticmethod
    async def _wait_for_worker(worker: asyncio.Future) -> None:
        """Wait until a worker thread has finished, even though this task was cancelled."""
        try:
            await asyncio.wait({worker})
        except asyncio.CancelledError:  # cancelled again: stop waiting
            pass

    async def decline(self, release_id: str) -> Release:
        """Delete a pending release's staging data; the row is kept to block re-imports."""
        async with self._lock(release_id):
            release = self._require_pending(release_id)
            await asyncio.to_thread(_rmtree, release.staging_path)
            self.db.update(release.id, status="declined", decided_at=self.clock(), staging_path=None)
            logger.info("Declined release %s (%s)", release.id, release.name)
        await self._clear_notifications(release.id)
        result = self.db.get(release.id)
        assert result is not None
        return result

    async def handle_event(self, event_type: str, data: dict) -> None:
        """Handle HA events carrying an approve/decline decision; never raises."""
        release_id: str | None = None
        decision: str | None = None
        if event_type == EVENT_MOBILE_ACTION:
            action = str((data or {}).get("action", ""))
            if action.startswith(_APPROVE_PREFIX):
                release_id, decision = action[len(_APPROVE_PREFIX):], "approve"
            elif action.startswith(_DECLINE_PREFIX):
                release_id, decision = action[len(_DECLINE_PREFIX):], "decline"
        elif event_type == EVENT_DECISION:
            release_id = str((data or {}).get("id") or "") or None
            decision = str((data or {}).get("decision") or "").lower() or None
        if not release_id or decision not in ("approve", "decline"):
            logger.debug("Ignoring event %s: %s", event_type, data)
            return
        try:
            if decision == "approve":
                await self.approve(release_id)
            else:
                await self.decline(release_id)
        except KeyError:
            logger.warning("Event %s refers to unknown release %s", event_type, release_id)
        except (InvalidState, NoSpaceError) as exc:
            logger.warning("Cannot %s release %s: %s", decision, release_id, exc)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to %s release %s", decision, release_id)

    def _manual_only(self, release: Release) -> bool:
        """True for a release that must never be approved without a person (the browser's downloads). Decided by the
        flag stored with the release, not by the current source list: the source may have been switched off or removed
        since. Releases made before the flag existed are recognised by the reserved source name "browser"."""
        report = release.report or {}
        origin = report.get("source")
        return bool(report.get("manual_only") is True or origin == "browser" or (origin and any(
            s.name == origin and getattr(s, "manual_only", False) for s in self.sources)))

    async def expire_pending(self) -> None:
        """Approve or decline (``pending_timeout_action``) pending releases older than
        ``pending_timeout_hours`` (0 = never)."""
        hours = self.cfg.pending_timeout_hours
        if hours <= 0:
            return
        action = self.cfg.pending_timeout_action
        cutoff = self.clock() - hours * 3600
        for release in self.db.list(statuses=["pending"], limit=100_000):
            if release.created_at < cutoff:
                if release.id not in self._expiry_logged:  # a refused approval is retried at every scan
                    self._expiry_logged.add(release.id)
                    logger.info("Release %s (%s) expired after %d h: %s", release.id, release.name, hours, action)
                try:
                    if action == "approve":
                        if self._manual_only(release):
                            continue  # e.g. files saved by the embedded browser: a person decides, or it stays pending
                        await self.approve(release.id)
                    else:
                        await self.decline(release.id)
                    self._space_warned.discard(release.id)
                    self._expiry_logged.discard(release.id)
                except (KeyError, InvalidState) as exc:
                    logger.debug("Release %s stays pending: %s", release.id, exc)
                except Exception as exc:  # noqa: BLE001
                    if isinstance(exc, NoSpaceError) or is_no_space_error(exc):
                        # the free-space check ran before anything was copied: retrying every scan
                        # does no harm, but one warning per release is enough
                        if release.id not in self._space_warned:
                            self._space_warned.add(release.id)
                            logger.warning("Cannot approve expired release %s (%s) yet: %s",
                                           release.id, release.name, exc)
                    else:
                        logger.exception("Failed to expire release %s", release.id)

    async def recover(self) -> None:
        """Clean up after a crash.

        Releases interrupted mid-processing are dropped so their source is reprocessed.
        Pending releases with an interrupted approval are completed when their content
        reached the library, otherwise the partial copy is removed and they stay pending.
        """
        for release in self.db.list(statuses=["processing"], limit=100_000):
            logger.warning("Recovering interrupted release %s (%s)", release.id, release.name)
            await asyncio.to_thread(_rmtree, release.staging_path)
            self.db.delete(release.id)
        for listed in self.db.list(statuses=["pending"], limit=100_000):
            if not listed.library_path:
                continue
            async with self._lock(listed.id):
                # an approval may have settled this release while the loop waited for its lock
                release = self.db.get(listed.id)
                if release is None or release.status != "pending" or release.library_path != listed.library_path:
                    continue
                dest = Path(release.library_path)
                placed = await asyncio.to_thread(_probe_dir, dest)
                if isinstance(placed, OSError):
                    # a share that is down: neither complete nor roll back; the next start (or an
                    # approval) looks again
                    logger.warning("Leaving the interrupted approval of %s (%s) as it is: %s cannot be read (%s)",
                                   release.id, release.name, dest, placed.strerror or placed)
                    continue
                if placed:
                    logger.warning("Completing interrupted approval of %s (%s)", release.id, release.name)
                    await self._complete_approval(release, dest)
                    await self._clear_notifications(release.id)
                else:
                    logger.warning("Rolling back interrupted approval of %s (%s)", release.id, release.name)
                    # Never delete content that may be the only copy: with staging and library on
                    # one file system the content was renamed into the hidden tmp folder, and only
                    # that folder holds it when the process died before the final rename.
                    restored = await asyncio.to_thread(
                        _restore_orphan_tmp, release.staging_path, _approval_tmp(dest.parent, release.id)
                    )
                    if restored:
                        self.db.update(release.id, library_path=None)
        for release in self.db.list(statuses=["approved"], limit=100_000):
            if release.staging_path:  # the staged copy of an approval that was cut short after it was recorded
                logger.info("Removing the leftover staged copy of approved release %s", release.id)
                await self._drop_staging(release)
        await self._upgrade_legacy_pending()

    async def _upgrade_legacy_pending(self) -> None:
        """Give releases that 0.1 left pending the 0.2 name hygiene (hidden entries, listable names).

        0.1 did not clean such names, and approval does not either, so the files would reach the
        library under names the sync API refuses to list. Their report has no ``renamed`` key.
        """
        for release in self.db.list(statuses=["pending"], limit=100_000):
            if "renamed" in release.report or release.library_path or not release.staging_path:
                continue
            content = Path(release.staging_path) / "content"
            if not await asyncio.to_thread(content.is_dir):
                continue
            async with self._lock(release.id):
                try:
                    report = await asyncio.to_thread(_repair_legacy, content, release.report)
                except Exception:  # noqa: BLE001
                    logger.exception("Could not bring pending release %s up to date", release.id)
                    continue
                self.db.update(release.id, report=report)
                logger.info("Brought pending release %s (%s) up to the current name rules", release.id, release.name)

    async def run(self, stop: asyncio.Event) -> None:
        """Main loop: recover and index, then scan and expire every ``scan_interval`` seconds
        (or as soon as :meth:`request_poll` asks) until stopped."""
        await self.recover()
        await self.refresh_free_space()  # the status shows the free space from the start
        try:
            await self.index_library()
        except Exception:  # noqa: BLE001 - retried at the next start
            logger.exception("Indexing the library failed")
        while not stop.is_set():
            try:
                await self.scan_once()
                await self.expire_pending()
            except Exception:  # noqa: BLE001
                logger.exception("Scan iteration failed")
            waiters = {asyncio.ensure_future(stop.wait()), asyncio.ensure_future(self._wake.wait())}
            try:
                await asyncio.wait(waiters, timeout=self.cfg.scan_interval, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for waiter in waiters:
                    waiter.cancel()
                self._wake.clear()
