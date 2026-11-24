"""Template base class for website sources.

A plugin subclasses :class:`RemoteSource` and implements ``list_releases`` and
``list_files``; polling, change detection, size limits, downloading, checksum verification,
failure bookkeeping and finalisation are handled here.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, Callable, ClassVar

from ..config import Config, library_blocks, parse_hhmm
from ..diskspace import NoSpaceError, ensure_free, is_no_space_error, min_free_bytes
from ..names import is_hidden_entry
from .base import Candidate, Outcome, Source
from .fetch import (
    MAX_FAILURES,
    RETRY_AFTER,
    DownloadCancelled,
    HttpClient,
    SeenStore,
    StateStore,
    download_file,
    has_wanted_extension,
    normalise_relative_name,
)

if TYPE_CHECKING:
    import aiohttp

log = logging.getLogger(__name__)

DOWNLOADS_DIR = "_downloads"
_SAFE_DIR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")
_NAME_RE = _SAFE_DIR_RE


@dataclass
class RemoteRelease:
    """A release on a website. ``list_files`` may enrich ``title``/``meta`` (they are read afterwards).

    ``meta`` is copied into ``Candidate.meta``; the keys ``url``, ``license`` and ``creator``
    are shown in the review UI and notification.
    """

    id: str
    title: str
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class RemoteFile:
    """A downloadable file. ``name`` is a relative path inside the release folder."""

    name: str
    url: str
    size: int | None = None
    checksum: str | None = None
    checksum_algo: str | None = None  # "sha1" | "md5" | "sha256"; inferred from length if omitted
    name_from_response: bool = False  # ``name`` is only a hint: use Content-Disposition / URL / type


def is_wanted_file(name: str) -> bool:
    """True for audio files and archives (by extension, case-insensitive)."""
    return has_wanted_extension(name)


def safe_dir_name(release_id: str) -> str:
    """A filesystem-safe folder name for a release id (stable and collision-free)."""
    if _SAFE_DIR_RE.match(release_id):
        return release_id
    cleaned = re.sub(r"[^A-Za-z0-9_.\-]", "_", release_id).lstrip("._-") or "release"
    return f"{cleaned[:60]}-{hashlib.sha1(release_id.encode('utf-8')).hexdigest()[:8]}"


def _positive_number(settings: dict[str, Any], key: str, default: float, integer: bool) -> float:
    value = settings.get(key, default)
    ok = isinstance(value, int) if integer else isinstance(value, (int, float))
    if isinstance(value, bool) or not ok or value <= 0:
        kind = "integer" if integer else "number"
        raise ValueError(f"{key} must be a positive {kind}, got {value!r}")
    return value


def _occurrence(ts: float, hour: int, minute: int, day_offset: int = 0) -> float:
    """Timestamp of ``hour:minute`` local time on the local day of ``ts`` (+ ``day_offset`` days)."""
    lt = time.localtime(ts)
    day = date(lt.tm_year, lt.tm_mon, lt.tm_mday) + timedelta(days=day_offset)
    return time.mktime((day.year, day.month, day.day, hour, minute, 0, 0, 0, -1))


class RemoteSource(Source):
    """Base class for sources that list releases and files on a remote website.

    Settings: ``name`` (state prefix and download folder), ``poll_minutes``, ``check_time``
    (``HH:MM`` local time, daily; replaces the interval), ``run_on_start`` (poll right after
    start-up, default true), ``max_items_per_scan``, ``auto_approve``.
    """

    type_name: ClassVar[str] = ""  # set by @register_source
    name: str = ""  # defaults to the registered type name
    state_prefix: ClassVar[str] = ""  # prefix of db state keys and candidate keys; defaults to name
    default_api_base: ClassVar[str] = ""
    default_poll_minutes: ClassVar[int] = 60
    default_max_items_per_scan: ClassVar[int] = 3
    notify_empty = False  # a remote release with nothing new is not worth a notification

    def __init__(
        self,
        cfg: Config,
        db: StateStore,
        session: aiohttp.ClientSession,
        settings: dict[str, Any] | None = None,
        clock: Callable[[], float] = time.time,
        api_base: str | None = None,
    ) -> None:
        self.cfg = cfg
        self.settings = dict(settings or {})
        self.clock = clock
        self.api_base = (api_base or self.default_api_base).rstrip("/")
        self.http = HttpClient(session)
        custom = self.settings.get("name")
        if custom is not None:
            if not isinstance(custom, str) or not _NAME_RE.match(custom):
                raise ValueError(f"name must match {_NAME_RE.pattern}, got {custom!r}")
            self.name = custom
        elif not self.name:
            self.name = self.type_name or type(self).__name__.lower()
        self._prefix = self.state_prefix or self.name
        self._db = db
        self.seen = SeenStore(db, self._prefix, lambda: self.clock())
        self.poll_minutes = _positive_number(self.settings, "poll_minutes", self.default_poll_minutes, False)
        self.max_items_per_scan = int(
            _positive_number(self.settings, "max_items_per_scan", self.default_max_items_per_scan, True)
        )
        check_time = self.settings.get("check_time") or ""
        if not isinstance(check_time, str):
            raise ValueError(f"check_time must be HH:MM or empty, got {check_time!r}")
        self._check_time = parse_hhmm(check_time, "check_time")
        self.run_on_start = self._flag("run_on_start", True)
        self.auto_approve = self._flag("auto_approve", False)
        self._start = self.clock()
        self._last_poll: float | None = self._load_last_poll()
        self._first_poll = True  # no poll yet since start-up
        self._force = False
        self._cancel = asyncio.Event()
        self._downloading = False
        self._progress: Callable[[dict[str, Any]], None] | None = None
        self._space_check: Callable[[str], int | None] | None = None
        self.waiting_for_space = False
        self._download_sizes: dict[str, int] = {}  # release id -> bytes of its last download (deferred ones)
        self._library_wait_logged = False
        self._space_reason = ""
        self._last_result: str | None = None

    def _flag(self, key: str, default: bool) -> bool:
        value = self.settings.get(key, default)
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be true or false, got {value!r}")
        return value

    @property
    def _lastpoll_key(self) -> str:
        return f"{self._prefix}:lastpoll"

    def _load_last_poll(self) -> float | None:
        try:
            return float(self._db.get_state(self._lastpoll_key))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    @property
    def retry_delay(self) -> float:
        return self.http.retry_delay

    @retry_delay.setter
    def retry_delay(self, value: float) -> None:
        self.http.retry_delay = value

    # ---- to implement / override
    async def list_releases(self) -> list[RemoteRelease]:
        raise NotImplementedError

    async def list_files(self, release: RemoteRelease) -> list[RemoteFile]:
        raise NotImplementedError

    def select_file(self, f: RemoteFile) -> bool:
        """Whether to download ``f``. Default: audio files and archives."""
        return is_wanted_file(f.name)

    # ---- schedule, status, control
    def next_poll_at(self) -> float | None:
        """When the next poll is due (the current time when it is due already)."""
        now = self.clock()
        if self._force or (self._first_poll and self.run_on_start):
            return now
        base = self._last_poll if self._last_poll is not None else self._start
        base = min(base, now)  # a clock that jumped back must not stall the schedule
        if self._check_time is not None:
            hour, minute = self._check_time
            due = _occurrence(base, hour, minute)
            return due if due > base else _occurrence(base, hour, minute, 1)
        return base + self.poll_minutes * 60

    def force_poll(self) -> None:
        """Make the next ``poll()`` run once, whatever the schedule says."""
        self._force = True

    def cancel(self) -> bool:
        """Stop the running download (only while one runs); return whether it was signalled."""
        if not self._downloading:
            return False
        self._cancel.set()
        return True

    def set_progress(self, callback: Callable[[dict[str, Any]], None] | None) -> None:
        self._progress = callback

    def set_space_check(self, callback: Callable[[str], int | None] | None) -> None:
        self._space_check = callback

    def _space_wanted(self, rid: str) -> int:
        """Free bytes the pipeline found this release needs on top of its download (0 if unknown)."""
        if self._space_check is None:
            return 0
        try:
            return max(0, int(self._space_check(f"{self._prefix}:{rid}") or 0))
        except Exception:  # noqa: BLE001 - a broken callback must not stop the source
            log.debug("%s: space check failed", self.name, exc_info=True)
            return 0

    def status(self) -> dict[str, Any]:
        base = super().status()
        base.update(
            last_poll_at=self._last_poll,
            next_poll_at=self.next_poll_at(),
            last_result=self._last_result,
            waiting_for_space=self.waiting_for_space,
            auto_approve=self.auto_approve,
        )
        return base

    def _due(self) -> bool:
        if self._force or (self._first_poll and self.run_on_start):
            return True
        due = self.next_poll_at()
        return due is not None and self.clock() >= due

    def _report(self, release: RemoteRelease, name: str, received: int, total: int | None) -> None:
        if self._progress is None:
            return
        try:
            self._progress(
                {
                    "source": self.name,
                    "release": release.title or release.id,
                    "file": name,
                    "received": received,
                    "total": total,
                }
            )
        except Exception:  # noqa: BLE001 - a broken listener must not break the download
            log.debug("%s: progress callback failed", self.name, exc_info=True)

    # ---- template
    def _folder(self, release: RemoteRelease):
        return self.cfg.staging_dir / DOWNLOADS_DIR / self.name / safe_dir_name(release.id)

    def _waits_for_library(self) -> bool:
        """True while downloading would create a missing library_dir (staging lies inside it).

        The download folder (and the import after it) would create the folder, and the sync API would
        then serve an empty library as 200 [] instead of answering 503. See config.library_blocks.
        """
        try:
            return library_blocks(self.cfg, self.cfg.staging_dir, self._db.get_state)
        except Exception:  # noqa: BLE001 - a broken check must not stop the source
            log.debug("%s: library check failed", self.name, exc_info=True)
            return False

    async def poll(self) -> list[Candidate]:
        if not self._due():
            return []
        # off the loop: it stats library_dir, which may be a network mount that is down
        if await asyncio.to_thread(self._waits_for_library):
            # nothing is consumed (the poll stays due): it runs as soon as library_dir exists or the
            # user confirmed it
            if not self._library_wait_logged:
                self._library_wait_logged = True
                log.warning(
                    "%s: not polling: library_dir %s does not exist and staging_dir %s lies inside it; "
                    "create the library folder or confirm it in Car Media and Trips",
                    self.name, self.cfg.library_dir, self.cfg.staging_dir,
                )
            self._last_result = "waiting for library_dir to exist"
            return []
        self._library_wait_logged = False
        now = self.clock()
        self._force = False
        self._first_poll = False
        self._last_poll = now
        self._cancel.clear()  # a stop request of an earlier poll must not stop this one
        try:
            self._db.set_state(self._lastpoll_key, repr(now))
        except Exception:  # noqa: BLE001
            log.warning("%s: could not persist the poll time", self.name, exc_info=True)
        stats = {"new": 0, "unchanged": 0, "failed": 0, "skipped": 0, "waiting": 0}
        suffix = ""
        self._space_reason = ""  # why a release (or the whole poll) waits for free space
        try:
            releases = await self.list_releases()
        except Exception as exc:  # noqa: BLE001
            log.warning("%s: listing releases failed: %s", self.name, exc)
            self._last_result = f"listing failed: {exc}"
            return []
        candidates: list[Candidate] = []
        seen_ids: set[str] = set()
        for release in releases:
            if len(candidates) >= self.max_items_per_scan:
                break
            if release.id in seen_ids:
                continue
            seen_ids.add(release.id)
            try:
                cand = await self._fetch_release(release, stats)
            except NoSpaceError as exc:
                self._space_reason = str(exc)
                stats["waiting"] += 1
                break
            except DownloadCancelled:
                log.info("%s: download stopped by the user", self.name)
                suffix = ", stopped"
                break
            except Exception as exc:  # noqa: BLE001
                log.warning("%s: release %s skipped: %s", self.name, release.id, exc)
                stats["failed"] += 1
                continue
            if cand is not None:
                candidates.append(cand)
        waiting = stats["waiting"] > 0
        if waiting:
            suffix = ", waiting for free space" + suffix
            if not self.waiting_for_space:
                log.warning("%s: waiting for free space: %s", self.name, self._space_reason)
        # decided afresh at every poll: one release that waits must not stop the others, and one
        # that downloaded must not hide that another still waits
        self.waiting_for_space = waiting
        parts = [f"{stats['new']} new", f"{stats['unchanged']} unchanged", f"{stats['failed']} failed"]
        if stats["skipped"]:
            parts.append(f"{stats['skipped']} skipped")
        self._last_result = ", ".join(parts) + suffix
        return candidates

    async def _fetch_release(self, release: RemoteRelease, stats: dict[str, int]) -> Candidate | None:
        rid = release.id
        wanted_files: list[RemoteFile] = []  # every selected file (the signature covers all of them)
        selected: list[RemoteFile] = []  # ... minus hidden entries: what is downloaded
        for f in await self.list_files(release):
            if normalise_relative_name(f.name) is None:
                log.warning("%s %s: rejecting unsafe file name %r", self.name, rid, f.name)
                continue
            if not self.select_file(f):
                continue
            wanted_files.append(f)
            if not f.name_from_response and any(is_hidden_entry(part) for part in f.name.split("/")):
                log.debug("%s %s: skipping hidden entry %r", self.name, rid, f.name)  # e.g. __MACOSX/._x.zip
                continue
            selected.append(f)
        if not selected:
            log.debug("%s %s: no matching files", self.name, rid)
            stats["unchanged"] += 1
            return None
        # The signature is the 0.1 one (over every selected file, hidden entries included), so the
        # sig:<id> values stored by 0.1 stay valid and an unchanged item is not downloaded again.
        wanted_files.sort(key=lambda f: f.name)
        lines = [f"{f.name}:{f.checksum or ''}:{max(0, f.size or 0)}" for f in wanted_files]
        signature = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
        # exact names first: a file named from its response (Content-Disposition) dedupes against what
        # is in the folder, so it must come after, or a later exact name would silently replace it
        selected.sort(key=lambda f: (f.name_from_response, f.name))

        if self.seen.is_unchanged(rid, signature):
            log.debug("%s %s unchanged", self.name, rid)
            stats["unchanged"] += 1
            return None
        if self.seen.track_signature(rid, signature) >= MAX_FAILURES:
            if not self.seen.retry_due(rid):
                log.debug("%s %s: parked until its files change or %d h passed", self.name, rid, RETRY_AFTER // 3600)
                stats["skipped"] += 1
                return None
            log.info("%s %s: trying again after %d failures", self.name, rid, MAX_FAILURES)
        total = sum(max(0, f.size or 0) for f in selected)
        limit = self.cfg.max_release_size_mb * 1024 * 1024
        if total > limit:
            log.warning(
                "%s %s is %d bytes, over the %d MB limit; skipping",
                self.name, rid, total, self.cfg.max_release_size_mb,
            )
            stats["skipped"] += 1
            return None

        min_free = min_free_bytes(self.cfg)
        # an earlier attempt was deferred because extracting it did not fit: do not download it
        # again until that much is free on top of the download itself. The need was measured with
        # the download on disk, so the download is part of the price (its real size when the source
        # declares none: RSS). Only this release waits; the others are still fetched.
        wanted = self._space_wanted(rid)
        if wanted:
            download = max(total, self._download_sizes.get(rid, 0))
            try:
                await asyncio.to_thread(ensure_free, self.cfg.staging_dir, download + wanted, 0)
            except NoSpaceError as exc:
                log.debug("%s %s: waiting for free space: %s", self.name, rid, exc)
                self._space_reason = self._space_reason or f"{rid}: {exc}"
                stats["waiting"] += 1
                return None
        await asyncio.to_thread(ensure_free, self.cfg.staging_dir, total, min_free)
        folder = self._folder(release)
        await asyncio.to_thread(shutil.rmtree, folder, True)
        self._cancel.clear()
        self._downloading = True
        try:
            remaining = limit
            for f in selected:
                declared = max(0, f.size or 0)
                max_bytes = min(declared, remaining) if declared > 0 else remaining
                remaining -= await download_file(
                    self.http, f.url, folder, f.name, max_bytes, f.checksum, f.checksum_algo,
                    progress=lambda received, size, _n=f.name: self._report(release, _n, received, size),
                    cancel=self._cancel, min_free=min_free, name_from_response=f.name_from_response,
                )
        except (NoSpaceError, DownloadCancelled):
            await asyncio.to_thread(shutil.rmtree, folder, True)
            raise
        except Exception as exc:  # noqa: BLE001
            if is_no_space_error(exc):  # a full disk is a wait, never a failure (SPEC decision 8)
                await asyncio.to_thread(shutil.rmtree, folder, True)
                raise NoSpaceError(f"the disk is full while downloading {rid}: {exc}") from exc
            log.warning("%s %s download failed: %s", self.name, rid, exc)
            await asyncio.to_thread(shutil.rmtree, folder, True)
            self.seen.record_failure(rid)
            stats["failed"] += 1
            return None
        finally:
            self._downloading = False
        self._download_sizes[rid] = limit - remaining  # a later deferral needs this much on top
        stats["new"] += 1
        return Candidate(
            path=folder,
            name=release.title or rid,
            is_dir=True,
            key=f"{self._prefix}:{rid}",
            meta={**release.meta, "identifier": rid, "signature": signature},
            disposable=True,
        )

    async def finalize(self, candidate: Candidate, outcome: Outcome) -> None:
        rid = candidate.meta.get("identifier") or candidate.key.removeprefix(f"{self._prefix}:")
        try:
            if outcome == "deferred":
                return  # not processed (no space / stopped): record nothing, fetch it again later
            self._download_sizes.pop(rid, None)
            if outcome == "failed":
                self.seen.record_failure(rid)
            else:
                self.seen.record_success(rid, candidate.meta.get("signature"))
        finally:
            await asyncio.to_thread(shutil.rmtree, candidate.path, True)
