"""Pull-sync API for the car app and cyp: the library over HTTP, per docs/SYNC_PROTOCOL.md.

``LibraryIndex`` walks ``library_dir`` and keeps the listing the protocol allows (hidden, type, size,
name hygiene, uniqueness and settle rules of section 4.2). ``create_sync_app`` serves it on its own
``web.Application`` (no ingress middlewares, never a 3xx). ``SyncServer`` owns the listener on the
fixed container port and never raises: its ``status()`` says why it is not listening.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import stat
import time
import urllib.parse
from dataclasses import dataclass
from email.utils import formatdate
from pathlib import Path
from typing import Any, Callable, Protocol

from aiohttp import web

from .config import Config
from .db import Database
from .ha import HAClient
from .names import SYNC_AUDIO_EXTS, name_key, segment_problem
from .regions_api import is_report_request, register_region_routes
from .fileserve import SYNC_DSCP, SendfileResponse, if_range_ok
from .probe import register_probe_routes
from .streams import MAX_STREAMS_HEADER, STREAMS, StreamBudget, busy_response
from .ev_settings import is_ev_settings_path, register_ev_settings_routes
from .trips import MAX_BODY_BYTES, TripPublisher, is_trips_path, register_trip_routes

log = logging.getLogger(__name__)

SYNC_PORT = 8100
API_PREFIX = "/api/v1/files"
MIN_TOKEN_LENGTH = 16
LIST_TTL = 30.0  # seconds a listing is reused; after that the next request starts a rebuild in the background
MAX_STALE = 120.0  # until the listing is this old it is still served while that rebuild runs; older: the request waits for it
DIR_CHECK_INTERVAL = 5.0  # a fresh listing is checked against the library folders' modification times this often
DIR_STAMP_MAX = 500  # folders (the root and its first-level folders, by name) the cheap change check looks at
SETTLE_SECONDS = 60.0  # a new name is hidden while its ctime is younger than this
MISS_REBUILD_INTERVAL = 5.0  # at most one rebuild per this many seconds when a name is not found
WRITE_TIMEOUT = 120.0  # abort a download when the client takes this long to accept one chunk
RETRY_AFTER_SECONDS = "300"
MAX_REJECTED_IN_STATUS = 50
STATE_LAST_COUNT = "sync:last_count"
STATE_LAST_NONEMPTY = "sync:last_nonempty_at"
STATE_LAST_DELETE = "sync:last_delete_at"
STATE_FOLDER_SPELLINGS = "sync:folder_spellings"  # JSON {name_key: spelling} of every listed folder
STATE_FOLDER_ALIASES = "sync:folder_aliases"  # JSON {name_key: on-disk spelling listed under the remembered one}
STATE_FOLDER_PRINTS = "sync:folder_prints"  # JSON {name_key: fingerprint of the files below the served folder}
REBUILD_DELAY = 2.0  # seconds after a library change before the listing is rebuilt in the background
_EMPTY_REASON = "library unexpectedly empty"

_INDEX = web.AppKey("index", object)
_TOKEN = web.AppKey("token", bytes)
_TRIPS_ON = web.AppKey("trips_on", bool)


class StateStore(Protocol):
    def get_state(self, key: str) -> str | None: ...
    def set_state(self, key: str, value: str) -> None: ...


class LibraryUnavailable(Exception):
    """The library cannot be listed reliably (missing, unreadable, walk error, unexpectedly empty)."""


@dataclass(frozen=True)
class Entry:
    """One listable file: ``name`` is the exact ``/``-separated path relative to the library."""

    name: str
    path: Path
    size: int
    mtime: int


@dataclass
class _Scan:
    entries: list[Entry]
    rejected: dict[str, str]


def _printable(text: str) -> str:
    """``text`` made safe for logs and JSON (lone surrogates and control characters escaped)."""
    safe = text.encode("utf-8", "backslashreplace").decode("utf-8")
    return "".join(ch if ch >= " " and ch != "\x7f" else f"\\x{ord(ch):02x}" for ch in safe)


def token_problem(token: str) -> str | None:
    """Why ``token`` cannot be the sync token (SYNC_PROTOCOL section 2), or None when it is fine."""
    if len(token) < MIN_TOKEN_LENGTH:
        return f"must be at least {MIN_TOKEN_LENGTH} characters"
    if any(not "!" <= ch <= "~" for ch in token):
        return "may only contain printable ASCII characters without spaces"
    return None


def _relative_dir(root: str, dirpath: str) -> str:
    """``dirpath`` (as produced by os.walk below ``root``) relative to ``root``, ``/``-separated."""
    if dirpath == root:
        return ""
    prefix = root if root.endswith((os.sep, os.altsep or os.sep)) else root + os.sep
    if dirpath.startswith(prefix):
        return dirpath[len(prefix):].replace(os.sep, "/")
    return os.path.relpath(dirpath, root).replace(os.sep, "/")  # pragma: no cover - defensive


def _scan_library(
    root: Path, now: float, settle: float, previous: frozenset[str], trust_old: bool = False
) -> _Scan:
    """Walk ``root`` (blocking). Raises LibraryUnavailable on any problem that could yield a partial list.

    ``trust_old`` (the first listing after a start, when ``previous`` is unknown): a file whose mtime is
    older than ``settle`` is listed even when its ctime is young (chmod/chown/xattr changes, not a copy).
    """
    root_str = os.fspath(root)
    try:
        if not stat.S_ISDIR(os.stat(root_str).st_mode):
            raise LibraryUnavailable(f"{root_str} is not a folder")
    except OSError as exc:
        raise LibraryUnavailable(f"cannot read {root_str}: {exc.strerror or exc}") from exc

    entries: list[Entry] = []
    rejected: dict[str, str] = {}

    def onerror(exc: OSError) -> None:
        # a sub-folder that vanished meanwhile is simply gone; anything else must not give a partial list
        if isinstance(exc, FileNotFoundError) and exc.filename != root_str:
            return
        raise exc

    try:
        for dirpath, dirnames, filenames in os.walk(root_str, onerror=onerror, followlinks=False):
            dirnames[:] = [
                d for d in dirnames
                if not d.startswith(".") and not os.path.islink(os.path.join(dirpath, d))
            ]
            rel_dir = _relative_dir(root_str, dirpath)
            dir_problem: tuple[str, str] | None = None
            for seg in rel_dir.split("/") if rel_dir else ():
                problem = segment_problem(seg)
                if problem:
                    dir_problem = (seg, problem)
                    break
            for fn in filenames:
                if fn.startswith(".") or os.path.splitext(fn)[1].lower() not in SYNC_AUDIO_EXTS:
                    continue
                full = os.path.join(dirpath, fn)
                try:
                    st = os.lstat(full)
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(st.st_mode) or st.st_size <= 0:
                    continue
                rel = f"{rel_dir}/{fn}" if rel_dir else fn
                bad = dir_problem
                if bad is None:
                    problem = segment_problem(fn)
                    bad = (fn, problem) if problem else None
                if bad is not None:
                    rejected[_printable(rel)] = f"{bad[1]} ({_printable(bad[0])})"
                    continue
                if settle > 0 and now - st.st_ctime < settle and rel not in previous:
                    if not (trust_old and now - st.st_mtime >= settle):
                        continue
                entries.append(Entry(rel, Path(full), st.st_size, int(st.st_mtime)))
    except LibraryUnavailable:
        raise
    except Exception as exc:  # OSError from the walk or lstat, anything unexpected: never a partial list
        raise LibraryUnavailable(f"cannot read {root_str}: {exc}") from exc
    return _Scan(entries, rejected)


def _dir_stamp(root: Path) -> tuple[tuple[str, int], ...] | None:
    """A cheap fingerprint of the library's folder structure (blocking): modification times of ``root`` and of its
    first-level folders (at most ``DIR_STAMP_MAX``, by name). A file added, renamed or deleted directly in one of these
    folders, or a folder added or removed below the root, changes it; changes deeper down do not (the TTL catches those).
    None when the root cannot be read."""
    root_str = os.fspath(root)
    try:
        items: list[tuple[str, int]] = [("", os.stat(root_str).st_mtime_ns)]
        with os.scandir(root_str) as it:
            names = sorted(e.name for e in it if not e.name.startswith("."))
        for name in names[:DIR_STAMP_MAX]:
            try:
                st = os.lstat(os.path.join(root_str, name))
            except OSError:
                continue
            if stat.S_ISDIR(st.st_mode):
                items.append((name, st.st_mtime_ns))
        return tuple(items)
    except OSError:
        return None


def library_has_listable_file(root: Path) -> bool:
    """Whether ``root`` holds at least one file the listing could serve (blocking; stops at the first).

    Same pruning and name rules as :func:`_scan_library`, without the settle rule (a file that is
    still settling will be listed soon). A missing or unreadable root counts as holding nothing;
    unreadable sub-folders are skipped.
    """
    root_str = os.fspath(root)
    for dirpath, dirnames, filenames in os.walk(root_str, followlinks=False):
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith(".") and not os.path.islink(os.path.join(dirpath, d))
            and segment_problem(d) is None
        ]
        for fn in filenames:
            if fn.startswith(".") or os.path.splitext(fn)[1].lower() not in SYNC_AUDIO_EXTS:
                continue
            if segment_problem(fn) is not None:
                continue
            try:
                st = os.lstat(os.path.join(dirpath, fn))
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode) and st.st_size > 0:
                return True
    return False


@dataclass
class _FolderPlan:
    kept: list[Entry]
    losers: list[tuple[str, str, str]]  # (folder, winning/remembered folder, "twin" | "respelled")
    spellings: dict[str, str]  # name_key -> spelling the phone holds, for every listed folder
    aliases: dict[str, str]  # name_key -> on-disk spelling that is listed under the remembered one
    prints: dict[str, str]  # name_key -> fingerprint of the files below the served folder


def _fingerprint(items: list[tuple[str, int, int]]) -> str:
    """A short digest of ``(path below the folder, size, mtime)`` triples (order does not matter)."""
    digest = hashlib.sha1(usedforsecurity=False)
    for rest, size, mtime in sorted(items):
        digest.update(f"{rest}\0{size}\0{mtime}\n".encode("utf-8", "backslashreplace"))
    return digest.hexdigest()[:16]


def plan_folders(
    entries: list[Entry],
    previous: frozenset[str] = frozenset(),
    preferred: dict[str, str] | None = None,
    aliases: dict[str, str] | None = None,
    prints: dict[str, str] | None = None,
) -> _FolderPlan:
    """Decide one spelling per folder ``name_key`` (SYNC_PROTOCOL 4.2 rule 5: ``Album/`` vs ``ALBUM/``).

    Decided top-down: depth-1 folders first, deeper folders only among the survivors. The winner is the
    spelling that holds files of the ``previous`` listing, then the ``preferred`` (persisted, every listed
    folder) one, then the one whose oldest file is oldest (first arrival), then the smallest spelling, so
    a spelling the phone already holds never flips. Every file below a losing folder is dropped.

    When the remembered spelling is gone from the disk (the served folder of a twin pair was deleted, or
    a folder was renamed only by case or Unicode form) the phone still holds the old spelling and would
    duplicate the folder under a new one. A folder whose files are exactly the files published before
    (same paths below it, sizes and mtimes: ``prints``) or that was already served that way (``aliases``)
    is then listed under the remembered spelling, its files keeping their on-disk path. Anything else
    is dropped as ``respelled``: rename it to the remembered spelling to get it listed.
    """
    preferred = preferred or {}
    aliases = aliases or {}
    prints = prints or {}
    previous_folders: set[str] = set()
    for name in previous:
        parts = name.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            previous_folders.add("/".join(parts[:i]))
    current = [(e, e.name.split("/")) for e in entries]
    losers: list[tuple[str, str, str]] = []
    seen: dict[str, str] = {}  # name_key -> spelling that survived, for every folder key met
    new_aliases: dict[str, str] = {}
    new_prints: dict[str, str] = {}
    frozen: set[str] = set()  # folders held back: what is remembered below them is kept
    depth = 1
    while True:
        groups: dict[str, dict[str, list[int]]] = {}  # name_key -> {spelling: indexes into current}
        for idx, (_, parts) in enumerate(current):
            if len(parts) > depth:
                prefix = "/".join(parts[:depth])
                groups.setdefault(name_key(prefix), {}).setdefault(prefix, []).append(idx)
        if not groups:
            break
        dropped: dict[str, str] = {}
        renamed: dict[str, str] = {}  # on-disk spelling -> spelling it is listed as

        def oldest(members: list[int]) -> int:
            return min(current[i][0].mtime for i in members)

        def print_of(members: list[int]) -> str:
            return _fingerprint([
                ("/".join(current[i][1][depth:]), current[i][0].size, current[i][0].mtime) for i in members
            ])

        for key, spellings in groups.items():
            remembered = preferred.get(key)
            if remembered is not None and (name_key(remembered) != key or remembered.count("/") != depth - 1):
                remembered = None  # a damaged stored value
            if remembered is not None and remembered not in spellings:
                # the spelling the phone holds is gone: list the same files under it, or nothing
                known = prints.get(key)
                fits = [s for s in spellings if aliases.get(key) == s or (known and print_of(spellings[s]) == known)]
                served = None
                if fits:
                    served = min(fits, key=lambda s, k=key: (aliases.get(k) != s, oldest(spellings[s]), s))
                    renamed[served] = remembered
                    new_aliases[key] = served
                    new_prints[key] = print_of(spellings[served])
                    if aliases.get(key) != served:
                        log.warning(
                            "Sync API: folder %s is listed as %s (the spelling the phone already holds)",
                            _printable(served), _printable(remembered),
                        )
                else:
                    frozen.add(key)
                    if known:
                        new_prints[key] = known
                seen[key] = remembered
                for spelling in spellings:
                    if spelling != served:
                        dropped[spelling] = remembered
                        losers.append((spelling, remembered, "respelled"))
                continue
            winner = min(
                spellings,
                key=lambda s, k=key, sp=spellings: (
                    s not in previous_folders, preferred.get(k) != s, oldest(sp[s]), s),
            )
            seen[key] = winner
            new_prints[key] = print_of(spellings[winner])
            for spelling in sorted(spellings):
                if spelling != winner:
                    dropped[spelling] = winner
                    losers.append((spelling, winner, "twin"))
        if dropped or renamed:
            nxt = []
            for entry, parts in current:
                if len(parts) > depth:
                    prefix = "/".join(parts[:depth])
                    if prefix in dropped:
                        continue
                    target = renamed.get(prefix)
                    if target is not None:
                        parts = target.split("/") + parts[depth:]
                        entry = Entry("/".join(parts), entry.path, entry.size, entry.mtime)
                nxt.append((entry, parts))
            current = nxt
        depth += 1
    spellings_now = dict(seen)
    if frozen:
        below = tuple(f + "/" for f in frozen)
        for old, new in ((preferred, spellings_now), (prints, new_prints)):
            for key, value in old.items():
                if key not in new and key.startswith(below):
                    new[key] = value
    return _FolderPlan([e for e, _ in current], losers, spellings_now, new_aliases, new_prints)


def drop_folder_twins(
    entries: list[Entry],
    previous: frozenset[str] = frozenset(),
    preferred: dict[str, str] | None = None,
) -> tuple[list[Entry], list[tuple[str, str]], dict[str, str]]:
    """:func:`plan_folders` without the alias memory.

    Returns ``(kept, [(losing_folder, winning_folder), ...], spellings)`` where ``spellings`` maps the
    ``name_key`` of every listed folder to the spelling that survived (vanished folders removed).
    """
    plan = plan_folders(entries, previous, preferred)
    return plan.kept, [(folder, winner) for folder, winner, _ in plan.losers], plan.spellings


def unique_by_key(entries: list[Entry]) -> tuple[list[Entry], list[tuple[Entry, Entry]]]:
    """Keep one entry per ``name_key`` (newest mtime, then the smallest name).

    Returns ``(kept, [(loser, winner), ...])``.
    """
    groups: dict[str, list[Entry]] = {}
    for entry in entries:
        groups.setdefault(name_key(entry.name), []).append(entry)
    kept: list[Entry] = []
    losers: list[tuple[Entry, Entry]] = []
    for group in groups.values():
        if len(group) == 1:
            kept.append(group[0])
            continue
        winner = min(group, key=lambda e: (-e.mtime, e.name))
        kept.append(winner)
        losers.extend((e, winner) for e in group if e is not winner)
    return kept, losers


def _bad_request_name(name: str) -> bool:
    """SYNC_PROTOCOL 5.2 step 1: names that are refused before any lookup."""
    if not name or "\0" in name or "\\" in name:
        return True
    return any(seg in ("", ".", "..") for seg in name.split("/"))


_FOLDER_STATE_KEYS = (STATE_FOLDER_SPELLINGS, STATE_FOLDER_ALIASES, STATE_FOLDER_PRINTS)


def _decide_names(
    entries: list[Entry], published: frozenset[str], stored: dict[str, dict[str, str]]
) -> tuple[_FolderPlan, list[Entry], list[tuple[Entry, Entry]]]:
    """Folder decisions, file collisions and the newest-first order (blocking, for a worker thread)."""
    plan = plan_folders(
        entries, published,
        stored[STATE_FOLDER_SPELLINGS], stored[STATE_FOLDER_ALIASES], stored[STATE_FOLDER_PRINTS],
    )
    kept, losers = unique_by_key(plan.kept)
    kept.sort(key=lambda e: (-e.mtime, e.name))
    return plan, kept, losers


class LibraryIndex:
    """Cached, validated listing of the library (SYNC_PROTOCOL section 4) and name resolution (5.2)."""

    def __init__(
        self,
        root: Path,
        state: StateStore,
        clock: Callable[[], float] = time.time,
        ttl: float = LIST_TTL,
        settle: float = SETTLE_SECONDS,
    ) -> None:
        self._root = Path(root)
        self._state = state
        self._clock = clock
        self._ttl = ttl
        self._settle = settle
        self._entries: list[Entry] = []
        self._by_name: dict[str, Entry] = {}
        self._built_at: float | None = None  # None: nothing usable cached
        self._last_attempt: float | None = None
        self._generation = 0
        self._lock: asyncio.Lock | None = None
        self._published: frozenset[str] = frozenset()  # names of the last successful listing
        self._rejected: dict[str, str] = {}
        self._logged: set[str] = set()
        self._paused_empty = False
        self._last_error: str | None = None
        self._failed_at: float | None = None  # when the last build failed (throttles retries)
        self._listed_once = False  # a build succeeded since this index was created
        self._refresh_task: asyncio.Task[None] | None = None  # a rebuild started by an expired TTL, running in the background
        self._stamp: tuple[tuple[str, int], ...] | None = None  # _dir_stamp() taken just before the last successful walk
        self._stamp_checked_at: float | None = None  # when a fresh listing was last compared with the folders

    # -- public API ------------------------------------------------------------------------------

    def invalidate(self) -> None:
        """Drop the cache (a release was approved or something was deleted)."""
        self._generation += 1
        self._built_at = None
        self._failed_at = None  # something changed: the next request may retry at once

    async def listing(self) -> list[Entry]:
        """The current listing, newest first. Raises LibraryUnavailable instead of a partial list."""
        if self._fresh() and not await self._folders_changed():
            return self._entries
        if self._usable_stale():
            # stale-while-revalidate: requests are answered from the cached listing while one rebuild runs in the background
            self._refresh_soon()
            return self._entries
        self._raise_if_failed_recently()
        async with self._get_lock():
            if self._fresh():
                return self._entries
            self._raise_if_failed_recently()
            return await self._build()

    async def aclose(self) -> None:
        """Stop a background rebuild (shutdown)."""
        task, self._refresh_task = self._refresh_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def lookup(self, name: str) -> Entry | None:
        """The listed file called exactly ``name``, checked on disk; None when it is not downloadable."""
        if _bad_request_name(name):
            return None
        await self.listing()
        entry = self._by_name.get(name)
        if entry is None:
            await self._rebuild_on_miss()
            entry = self._by_name.get(name)
        if entry is None:
            return None
        if not await asyncio.to_thread(self._verify, entry):
            self._invalidate_if_due()
            return None
        return entry

    def rejected(self) -> dict[str, str]:
        """Names that exist but are not listed because of name hygiene or collisions, with the reason."""
        return dict(self._rejected)

    def stats(self) -> dict[str, Any]:
        return {
            "files": len(self._entries),
            "built_at": self._built_at,
            "rejected": len(self._rejected),
            "paused_empty": self._paused_empty,
            "error": self._last_error,
        }

    # -- internals -------------------------------------------------------------------------------

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _fresh(self) -> bool:
        if self._built_at is None:
            return False
        return 0 <= self._clock() - self._built_at < self._ttl

    def _usable_stale(self) -> bool:
        """A listing whose TTL ran out but that is not too old: it may still be served while a rebuild runs.

        At most ``MAX_STALE`` seconds old, so a file changed directly in the library folder (Samba, NAS, HA media browser)
        shows up within the TTL plus the time of the rebuild, and a request never serves a listing older than that.
        """
        if self._built_at is None or not self._entries:
            return False
        return self._ttl <= self._clock() - self._built_at < max(MAX_STALE, self._ttl)

    async def _folders_changed(self) -> bool:
        """A fresh listing is compared with the folders' modification times (cheap, at most every ``DIR_CHECK_INTERVAL``).

        True drops the cache (``_built_at``), so the request waits for a rebuild: a change made behind the add-on's back
        is seen within the TTL. A failed or impossible check changes nothing.
        """
        now = self._clock()
        if self._stamp is None or self._stamp_checked_at is None:
            return False
        if 0 <= now - self._stamp_checked_at < DIR_CHECK_INTERVAL:
            return False
        self._stamp_checked_at = now
        current = await asyncio.to_thread(_dir_stamp, self._root)
        if current is None or current == self._stamp:
            return False
        self._built_at = None
        self._failed_at = None
        return True

    def _refresh_soon(self) -> None:
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.get_running_loop().create_task(self._refresh_quietly())

    async def _refresh_quietly(self) -> None:
        try:
            async with self._get_lock():
                if self._fresh():
                    return
                await self._build()
        except LibraryUnavailable:
            pass  # remembered by _fail(): the next request answers 503
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Sync API: background listing failed")

    def _raise_if_failed_recently(self) -> None:
        """A failed build is answered again for a few seconds instead of re-walking the whole tree."""
        if self._failed_at is not None and self._last_error is not None:
            if 0 <= self._clock() - self._failed_at < MISS_REBUILD_INTERVAL:
                raise LibraryUnavailable(self._last_error)

    def _invalidate_if_due(self) -> None:
        """Mark the cache stale after a failed check, but not more often than a miss may rebuild."""
        now = self._clock()
        if self._last_attempt is None or not 0 <= now - self._last_attempt < MISS_REBUILD_INTERVAL:
            self._built_at = None

    async def _rebuild_on_miss(self) -> None:
        async with self._get_lock():
            now = self._clock()
            if self._last_attempt is not None and 0 <= now - self._last_attempt < MISS_REBUILD_INTERVAL:
                return
            await self._build()

    def _verify(self, entry: Entry) -> bool:
        """5.2 step 3 (blocking): still a regular file, and its real path is inside the real root."""
        try:
            if not stat.S_ISREG(os.lstat(entry.path).st_mode):
                return False
            real_root = os.path.realpath(self._root)
            real = os.path.realpath(entry.path)
            return real != real_root and os.path.commonpath([real_root, real]) == real_root
        except (OSError, ValueError):
            return False

    async def _build(self) -> list[Entry]:
        """Rebuild the listing. The caller holds the lock."""
        generation = self._generation
        now = self._clock()
        self._last_attempt = now
        try:
            stamp = await asyncio.to_thread(_dir_stamp, self._root)  # before the walk: a change during it is seen next time
            scan = await asyncio.to_thread(
                _scan_library, self._root, now, self._settle, self._published, not self._listed_once
            )
            stored = {key: self._load_map(key) for key in _FOLDER_STATE_KEYS}
            plan, kept, losers = await asyncio.to_thread(
                _decide_names, scan.entries, self._published, stored
            )
            self._check_empty_guard(len(kept), now)
        except LibraryUnavailable as exc:
            self._fail(exc, generation)
            raise
        for key, value in zip(_FOLDER_STATE_KEYS, (plan.spellings, plan.aliases, plan.prints)):
            if value != stored[key]:
                self._state_set(key, json.dumps(value, sort_keys=True, ensure_ascii=True))
        rejected = dict(scan.rejected)
        for folder, winner, kind in plan.losers:
            if kind == "twin":
                rejected[_printable(folder)] = f"folder collides with {_printable(winner)}"
            else:
                rejected[_printable(folder)] = (
                    f"folder was published as {_printable(winner)}: rename it to {_printable(winner)} to list it"
                )
        for loser, winner in losers:
            rejected[_printable(loser.name)] = f"collides with {_printable(winner.name)}"
        self._log_rejected(rejected)
        self._rejected = rejected
        self._entries = kept
        self._by_name = {e.name: e for e in kept}
        self._published = frozenset(self._by_name)
        # an invalidate() during the walk means the result may already be outdated
        self._built_at = now if generation == self._generation else None
        self._stamp, self._stamp_checked_at = stamp, now
        self._paused_empty = False
        self._listed_once = True
        self._failed_at = None
        if self._last_error is not None:
            log.info("Sync API: the library can be listed again (%d files)", len(kept))
            self._last_error = None
        return kept

    def _fail(self, exc: LibraryUnavailable, generation: int) -> None:
        self._built_at = None
        if generation == self._generation:  # an invalidate() during the walk allows an immediate retry
            self._failed_at = self._clock()
        self._entries = []
        self._by_name = {}
        self._paused_empty = str(exc) == _EMPTY_REASON
        if self._last_error != str(exc):
            log.warning("Sync API: library unavailable, answering 503: %s", exc)
        self._last_error = str(exc)

    def _log_rejected(self, rejected: dict[str, str]) -> None:
        for name, reason in rejected.items():
            if name not in self._logged:
                log.warning("Sync API: not listing %s: %s", name, reason)
        self._logged = set(rejected)

    def _state_float(self, key: str) -> float | None:
        try:
            value = self._state.get_state(key)
            return float(value) if value is not None else None
        except Exception:  # a bad value or a database hiccup must not break listing
            log.debug("Sync API: cannot read state %s", key, exc_info=True)
            return None

    def _load_map(self, key: str) -> dict[str, str]:
        """A persisted folder map ({name_key: text}); empty when missing or damaged."""
        try:
            raw = self._state.get_state(key)
            data = json.loads(raw) if raw else {}
        except Exception:
            log.debug("Sync API: cannot read state %s", key, exc_info=True)
            return {}
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}

    def _state_set(self, key: str, value: str) -> None:
        try:
            self._state.set_state(key, value)
        except Exception:
            log.warning("Sync API: cannot store state %s", key, exc_info=True)

    def _check_empty_guard(self, count: int, now: float) -> None:
        """Empty-library guard (SYNC_PROTOCOL 4): refuse a sudden 0 files that no deletion explains."""
        stored = self._state_float(STATE_LAST_COUNT)
        last_count = int(stored) if stored is not None else 0
        nonempty_at = self._state_float(STATE_LAST_NONEMPTY)
        delete_at = self._state_float(STATE_LAST_DELETE)
        deleted = delete_at if delete_at is not None else float("-inf")
        if count == 0:
            if last_count > 0 and deleted < (nonempty_at or 0.0):
                raise LibraryUnavailable(_EMPTY_REASON)
            if stored != 0:
                self._state_set(STATE_LAST_COUNT, "0")
            return
        if stored is None or last_count != count:
            self._state_set(STATE_LAST_COUNT, str(count))
        # The guard is armed once a non-empty listing is newer than the last deletion. Both stamps are
        # wall-clock times of this host: after a clock step back a deletion may lie in the future (the
        # stamp must still end up after it) and a stamp may lie in the future (it is pulled back).
        if nonempty_at is None or nonempty_at <= deleted or nonempty_at > now:
            stamp = now if now > deleted else math.nextafter(deleted, math.inf)
            if stamp != nonempty_at:
                self._state_set(STATE_LAST_NONEMPTY, repr(stamp))


# -- HTTP ----------------------------------------------------------------------------------------


def _json(status: int, payload: Any, headers: dict[str, str] | None = None) -> web.Response:
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    resp = web.Response(status=status, body=body, content_type="application/json", charset="utf-8")
    resp.headers["Cache-Control"] = "no-store"
    for key, value in (headers or {}).items():
        resp.headers[key] = value
    return resp


def _not_found() -> web.Response:
    return _json(404, {"error": "not_found"})


def _unavailable() -> web.Response:
    return _json(503, {"error": "library_unavailable"}, {"Retry-After": RETRY_AFTER_SECONDS})


def _token_matches(header: str | None, expected: bytes) -> bool:
    """``Authorization: Bearer <token>`` check on bytes in constant time (SYNC_PROTOCOL section 2)."""
    if not header:
        return False
    scheme, _, rest = header.strip().partition(" ")
    if scheme.lower() != "bearer":
        return False
    try:
        got = rest.strip().encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:
        return False
    return hmac.compare_digest(got, expected)


@web.middleware
async def _error_middleware(request: web.Request, handler: Callable[..., Any]) -> web.StreamResponse:
    """JSON bodies for every error; never a redirect. Failures after prepare() propagate (abort)."""
    try:
        return await handler(request)
    except web.HTTPException as exc:
        if 300 <= exc.status < 400 or exc.status == 404:
            return _not_found()
        headers = {"Allow": exc.headers["Allow"]} if "Allow" in exc.headers else None
        return _json(exc.status, {"error": exc.reason.lower().replace(" ", "_")}, headers)
    except (asyncio.CancelledError, ConnectionError):
        raise
    except Exception:
        log.exception("Sync API: unexpected error for %s", request.method)
        return _json(500, {"error": "internal_error"})


@web.middleware
async def _auth_middleware(request: web.Request, handler: Callable[..., Any]) -> web.StreamResponse:
    """Authenticate before anything touches the file system, then allow GET and HEAD only.

    The trip API (``/api/trips``, ``/api/stats``) is authenticated the same way and brings its own methods; so does
    the one ``POST /api/regions/devices/report`` of the regions API.
    """
    if not _token_matches(request.headers.get("Authorization"), request.app[_TOKEN]):
        return _json(401, {"error": "unauthorized"}, {"WWW-Authenticate": 'Bearer realm="car_media_hub"'})
    if request.method not in ("GET", "HEAD") and not is_report_request(request) and not (_TRIPS_ON in request.app and (is_trips_path(request.rel_url.path) or is_ev_settings_path(request.rel_url.path))):
        return _json(405, {"error": "method_not_allowed"}, {"Allow": "GET, HEAD"})
    return await handler(request)


def _requested_name(request: web.Request) -> str | None:
    """The file name from the raw request target (query removed), percent-decoded once as UTF-8."""
    raw = request.raw_path
    if not raw.startswith("/"):  # absolute-form target
        raw = request.rel_url.raw_path
    path = raw.split("?", 1)[0]
    prefix = API_PREFIX + "/"
    if not path.startswith(prefix):
        return None
    try:
        return urllib.parse.unquote(path[len(prefix):], encoding="utf-8", errors="strict")
    except UnicodeDecodeError:
        return None


class _Unsatisfiable(Exception):
    pass


def _digits(text: str) -> int:
    """The value of an ASCII digit string; anything beyond 18 significant digits is simply huge.

    ``int()`` refuses strings of more than 4300 digits, which a header may well carry.
    """
    stripped = text.lstrip("0")
    if not stripped:
        return 0
    return int(stripped) if len(stripped) <= 18 else 1 << 63


def _byte_range(value: str, size: int) -> tuple[int, int] | None:
    """A single ``bytes=N-`` or ``bytes=N-M`` range as (start, end); None means ignore the header."""
    unit, _, spec = value.strip().partition("=")
    if unit.strip().lower() != "bytes" or "," in spec:
        return None
    first, dash, last = (part.strip() for part in spec.partition("-"))
    if not dash or not (first.isascii() and first.isdigit()):
        return None  # suffix ranges and garbage are ignored
    start = _digits(first)
    if last:
        if not (last.isascii() and last.isdigit()) or _digits(last) < start:
            return None
        end = _digits(last)
    else:
        end = size - 1
    if start >= size:
        raise _Unsatisfiable
    return start, min(end, size - 1)


def _open_regular(path: Path) -> Any:
    """Open ``path`` for binary reading without following a symlink (blocking); regular files only."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise IsADirectoryError(f"{path} is not a regular file")
        return os.fdopen(fd, "rb", buffering=0)
    except BaseException:
        os.close(fd)
        raise


async def _list_files(request: web.Request) -> web.StreamResponse:
    try:
        entries = await request.app[_INDEX].listing()
    except LibraryUnavailable:
        return _unavailable()
    return _json(200, [{"name": e.name, "size": e.size, "mtime": e.mtime, "sha256": None} for e in entries])


def music_etag(size: int, mtime_ns: int) -> str:
    """The strong ETag of a music file: size and modification time in hex (``"<size>-<mtime_ns>"``, quoted)."""
    return f'"{size:x}-{mtime_ns:x}"'


async def _get_file(request: web.Request) -> web.StreamResponse:
    index: LibraryIndex = request.app[_INDEX]
    budget: StreamBudget = request.app[STREAMS]
    name = _requested_name(request)
    if name is None:
        return _not_found()
    try:
        entry = await index.lookup(name)
    except LibraryUnavailable:
        return _unavailable()
    if entry is None:
        return _not_found()
    try:
        f = await asyncio.to_thread(_open_regular, entry.path)
    except OSError as exc:
        if not isinstance(exc, (FileNotFoundError, PermissionError, NotADirectoryError, IsADirectoryError)):
            log.warning("Sync API: cannot open %s: %s", _printable(entry.name), exc)
        return _not_found()
    max_streams = {MAX_STREAMS_HEADER: str(budget.per_client)}
    try:
        st = os.fstat(f.fileno())
        size = st.st_size
        etag = music_etag(size, st.st_mtime_ns)
        last_modified = formatdate(int(st.st_mtime), usegmt=True)
        start, end, status = 0, size - 1, 200
        range_header = request.headers.get("Range")
        # a Range request is honoured when it carries no If-Range or an If-Range that names this very version
        if range_header and if_range_ok(request.headers.get("If-Range"), etag, last_modified, st.st_mtime_ns):
            try:
                selected = _byte_range(range_header, size)
            except _Unsatisfiable:
                return _json(416, {"error": "range_not_satisfiable"},
                             {"Content-Range": f"bytes */{size}", "ETag": etag, "Accept-Ranges": "bytes", **max_streams})
            if selected is not None:
                (start, end), status = selected, 206
        headers = {
            "Content-Type": "application/octet-stream",
            "Cache-Control": "no-store",
            "Accept-Ranges": "bytes",
            "ETag": etag,
            "Last-Modified": last_modified,
            **max_streams,
        }
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        stream = None
        if request.method != "HEAD":
            stream = budget.begin(request.remote or "", ("music", entry.name), request.transport)
            if stream is None:
                return busy_response(max_streams)
        try:
            resp = SendfileResponse(f, start, end - start + 1, status=status, headers=headers, stream=stream,
                                    timeout=WRITE_TIMEOUT, what=_printable(entry.name))
            await resp.send(request)
            return resp
        finally:
            if stream is not None:
                budget.end(stream)
    finally:
        f.close()


def create_sync_app(
    index: LibraryIndex, token: str, trips: Database | None = None, publisher: TripPublisher | None = None,
    regions: Any = None, dscp: bool = True,
) -> web.Application:
    """The sync API as its own application: only the auth and error middlewares of this module.

    With ``trips`` (the database) the trip upload API of :mod:`car_media_hub.trips` is served too, with
    ``regions`` (a ``RegionManager``) the offline map region API of :mod:`car_media_hub.regions_api`.
    """
    problem = token_problem(token)
    if problem:
        raise ValueError(f"invalid sync token: {problem}")
    app = web.Application(middlewares=[_error_middleware, _auth_middleware], client_max_size=MAX_BODY_BYTES)
    app[_INDEX] = index
    app[_TOKEN] = token.encode("ascii")
    app[SYNC_DSCP] = dscp  # option sync_dscp
    app[STREAMS] = getattr(regions, "streams", None) or StreamBudget()  # one budget for music, regions, resources and probe
    register_probe_routes(app)
    app.router.add_get(API_PREFIX, _list_files)
    app.router.add_get(API_PREFIX + "/{name:.+}", _get_file)
    if trips is not None:
        app[_TRIPS_ON] = True
        register_trip_routes(app, trips, publisher)
        register_ev_settings_routes(app)
    if regions is not None:
        register_region_routes(app, regions)
    return app


class SyncServer:
    """Owns the sync listener. Never raises; ``status()`` explains why it is not listening."""

    def __init__(
        self,
        index: LibraryIndex | None = None,
        token: str = "",
        *,
        enabled: bool = True,
        reason: str = "",
        trips: Database | None = None,
        publisher: TripPublisher | None = None,
        regions: Any = None,
        dscp: bool = True,
    ) -> None:
        self._dscp = dscp
        self._regions = regions
        self._trips = trips
        self._publisher = publisher
        self._index = index
        self._token = token
        self._enabled = enabled
        self._reason = reason
        self._runner: web.AppRunner | None = None
        self._port: int | None = None
        self._warm_task: asyncio.Task[None] | None = None
        self._rebuild_task: asyncio.Task[None] | None = None
        self._rebuild_sleeping = False  # the rebuild task has not started walking yet
        self._rebuild_dirty = False  # the library changed again while the rebuild task was walking

    async def start(self, host: str = "0.0.0.0", port: int = SYNC_PORT) -> None:
        if self._runner is not None or self._index is None:
            return
        runner: web.AppRunner | None = None
        try:
            runner = web.AppRunner(
                create_sync_app(self._index, self._token, self._trips, self._publisher, self._regions, self._dscp), access_log=None, shutdown_timeout=5.0)
            await runner.setup()
            await web.TCPSite(runner, host, port).start()
            bound = runner.addresses[0][1] if runner.addresses else port
        except Exception as exc:
            self._reason = f"cannot listen on port {port}: {exc}"
            log.warning("Sync API not started: %s", self._reason)
            if runner is not None:
                try:
                    await runner.cleanup()
                except Exception:
                    log.debug("Sync API: cleanup after a failed start failed", exc_info=True)
            return
        self._runner = runner
        self._port = int(bound)
        self._reason = ""
        log.info("Sync API listening on port %d", self._port)
        self._warm_task = asyncio.create_task(self._warm())

    async def _warm(self) -> None:
        """Build the first listing at start so the status and the empty-library guard are current."""
        try:
            assert self._index is not None
            await self._index.listing()
        except LibraryUnavailable:
            pass  # already logged by the index and visible in the status
        except Exception:
            log.exception("Sync API: first listing failed")

    async def _rebuild_later(self) -> None:
        """Re-list shortly after a library change so the empty-library guard re-arms and the status is current."""
        assert self._index is not None
        while True:
            self._rebuild_sleeping = True
            await asyncio.sleep(REBUILD_DELAY)
            # from here on invalidate() never cancels this task: a walk in a worker thread cannot be
            # stopped, so a cancelled rebuild would only pile a second walk on top of the running one
            self._rebuild_sleeping = False
            self._rebuild_dirty = False
            try:
                await self._index.listing()
            except LibraryUnavailable:
                pass  # already logged by the index and visible in the status
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Sync API: listing after a library change failed")
            if not self._rebuild_dirty:
                return

    async def stop(self) -> None:
        for name in ("_warm_task", "_rebuild_task"):
            task = getattr(self, name)
            setattr(self, name, None)
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if self._index is not None:
            await self._index.aclose()
        runner, self._runner = self._runner, None
        self._port = None
        if runner is not None:
            try:
                await runner.cleanup()
            except Exception:
                log.warning("Sync API: error while stopping", exc_info=True)
            self._reason = "stopped"

    def invalidate(self) -> None:
        """Library changed (approval, deletion, confirmed empty): drop the cached listing."""
        if self._index is None:
            return
        self._index.invalidate()
        if self._runner is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # called outside the event loop: the next request rebuilds
            return
        task = self._rebuild_task
        if task is not None and not task.done():
            if not self._rebuild_sleeping:
                self._rebuild_dirty = True  # walking: it goes around once more when it is done
                return
            task.cancel()  # still waiting: restart the delay so a burst of changes costs one walk
        self._rebuild_sleeping = True
        self._rebuild_task = loop.create_task(self._rebuild_later())

    def status(self) -> dict[str, Any]:
        listening = self._runner is not None
        stats = self._index.stats() if self._index is not None else {"files": 0, "paused_empty": False, "error": None}
        rejected = self._index.rejected() if self._index is not None else {}
        return {
            "enabled": self._enabled,
            "listening": listening,
            "port": self._port if listening else None,
            "reason": "" if listening else self._reason,
            "files": stats["files"],
            "rejected": [
                {"name": name, "reason": reason} for name, reason in sorted(rejected.items())
            ][:MAX_REJECTED_IN_STATUS],
            "rejected_total": len(rejected),
            "paused_empty": stats["paused_empty"],
            "error": stats.get("error"),
        }


async def start_sync_server(
    cfg: Config, state: StateStore, *, host: str = "0.0.0.0", port: int = SYNC_PORT,
    trips: Database | None = None, ha: HAClient | None = None, regions: Any = None,
) -> SyncServer:
    """Create and start the sync server from the options. Never raises."""
    try:
        if not cfg.sync_enabled:
            log.info("Sync API not started: disabled")
            return SyncServer(enabled=False, reason="disabled")
        token = cfg.sync_token.strip()
        problem = token_problem(token)
        if problem:
            reason = f"sync_token is invalid: {problem}"
            log.warning("Sync API not started: %s", reason)
            return SyncServer(enabled=True, reason=reason)
        publisher = TripPublisher(trips, ha) if trips is not None and ha is not None else None
        server = SyncServer(LibraryIndex(cfg.library_dir, state), token, trips=trips, publisher=publisher, regions=regions,
                            dscp=cfg.sync_dscp)
        await server.start(host, port)
        if publisher is not None and server.status()["listening"]:
            publisher.publish_soon()  # Home Assistant forgets REST states when it restarts
        return server
    except Exception as exc:  # pragma: no cover - defensive: the add-on must start regardless
        log.exception("Sync API not started")
        return SyncServer(enabled=True, reason=f"internal error: {exc}")
