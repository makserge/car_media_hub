"""Inbox source: watches a folder for archives and folders that stopped changing."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from ..config import ARCHIVE_EXTS, Config, check_inbox_dir, path_relation
from .base import Candidate, Outcome, Source
from .registry import register_source
from .remote import _NAME_RE

if TYPE_CHECKING:
    import aiohttp

    from ..db import Database

log = logging.getLogger(__name__)

PROCESSED_DIR = "_processed"


def _archive_suffix(name: str) -> str | None:
    lower = name.lower()
    for ext in sorted(ARCHIVE_EXTS, key=len, reverse=True):
        if lower.endswith(ext):
            return ext
    return None


def _tree_signature(path: Path) -> tuple[int, float]:
    """Return (total size, newest mtime) for a file or directory tree."""
    if path.is_symlink():
        return 0, 0.0
    st = path.stat()
    if not path.is_dir():
        return st.st_size, st.st_mtime
    size, newest = 0, st.st_mtime
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                est = entry.stat()
            except OSError:
                continue
            newest = max(newest, est.st_mtime)
            if entry.is_dir():
                stack.append(entry)
            else:
                size += est.st_size
    return size, newest


def _remove(path: Path) -> None:
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        log.warning("Could not delete %s", path, exc_info=True)


def _unique_dest(directory: Path, name: str) -> Path:
    dest = directory / name
    counter = 2
    while dest.exists():
        dest = directory / f"{name} ({counter})"
        counter += 1
    return dest


def _derived_name(directory: Path) -> str:
    """``inbox-<folder name>`` for an inbox folder other than ``inbox_dir`` (a short hash if unusable)."""
    base = re.sub(r"[^A-Za-z0-9_.\-]", "_", directory.name).strip("._-")
    if not base:
        base = hashlib.sha1(str(directory).encode("utf-8")).hexdigest()[:8]
    return f"inbox-{base}"[:60]


@register_source("inbox")
class InboxSource(Source):
    """Watches the inbox folder (``cfg.inbox_dir`` unless overridden) for archives and folders
    that stopped changing.

    Settings: ``dir`` (optional, absolute) overrides ``cfg.inbox_dir``; ``name`` (optional) names the
    source. Without a name the source is called ``inbox`` for ``cfg.inbox_dir`` and
    ``inbox-<folder name>`` for any other folder, so several inboxes can be configured.
    """

    name = "inbox"
    offers_deferred_again = True  # a deferred entry stays in _seen and is offered at every poll

    def __init__(
        self,
        cfg: Config,
        clock: Callable[[], float] = time.time,
        inbox_dir: Path | None = None,
        name: str | None = None,
        manual_only: bool = False,
    ) -> None:
        self.manual_only = manual_only  # a human always decides: never auto-approved, not even by the pending timeout
        self._cfg = cfg
        self._clock = clock
        self._inbox = Path(inbox_dir) if inbox_dir is not None else cfg.inbox_dir
        if name is not None:
            self.name = name
        self._seen: dict[Path, tuple[int, float]] = {}
        # entries left in place after finalize; ignored until their signature changes
        self._handled: dict[Path, tuple[int, float]] = {}
        self._warned: set[str] = set()
        self._noted: set[str] = set()  # entries already mentioned in the log (once each)
        self._last_poll: float | None = None  # shown in the Sources table of the web page
        self._last_result: str | None = None
        self._last_outcome: str | None = None

    @property
    def inbox_dir(self) -> Path:
        return self._inbox

    @classmethod
    def create(
        cls, cfg: Config, db: Database, session: aiohttp.ClientSession, settings: dict[str, Any]
    ) -> InboxSource:
        directory = settings.get("dir")
        if directory is not None and (not isinstance(directory, str) or not directory.strip()):
            raise ValueError(f"inbox: 'dir' must be a non-empty string, got {directory!r}")
        custom = settings.get("name")
        if custom is not None and (not isinstance(custom, str) or not _NAME_RE.match(custom)):
            raise ValueError(f"inbox: name must match {_NAME_RE.pattern}, got {custom!r}")
        inbox = Path(directory.strip()) if directory else cfg.inbox_dir
        if directory and not (inbox.is_absolute() or directory.strip().startswith("/")):
            raise ValueError(f"inbox: 'dir' must be an absolute path, got {directory!r}")
        # the same layout rules as load_config (which never sees a 'dir' from sources.json)
        check_inbox_dir(inbox, cfg.staging_dir, cfg.library_dir, what="inbox 'dir'" if directory else "inbox_dir")
        name = custom
        if name is None and directory and path_relation(inbox, cfg.inbox_dir) != "same":
            name = _derived_name(inbox)
        return cls(cfg, inbox_dir=inbox if directory else None, name=name, manual_only=settings.get("manual_only") is True)

    # ---- safety: the inbox deletes what it imports, so it must never touch the library or staging
    def _layout_problem(self) -> str | None:
        """Why the whole inbox folder is unusable (it is, or lies inside, the library or staging)."""
        cfg = self._cfg
        if path_relation(self._inbox, cfg.library_dir) in ("same", "inside"):
            return f"the inbox {self._inbox} is the library folder or inside it"
        if path_relation(self._inbox, cfg.staging_dir) == "same":
            return f"the inbox {self._inbox} is the staging folder"
        return None

    def _entry_problem(self, entry: Path) -> str | None:
        """Why ``entry`` must not be imported or deleted (it is, contains or lies inside the library,
        or is or contains the staging folder), or None."""
        cfg = self._cfg
        if path_relation(entry, cfg.library_dir) is not None:
            return "it is, contains or lies inside the library folder"
        if path_relation(entry, cfg.staging_dir) in ("same", "contains"):
            return "it is or contains the staging folder"
        return None

    def _warn_once(self, key: str, message: str, *args: Any) -> None:
        if key not in self._warned:
            self._warned.add(key)
            log.error(message, *args)

    def _note_once(self, key: str, message: str, *args: Any) -> None:
        if key not in self._noted:
            self._noted.add(key)
            log.info(message, *args)

    def _scan(self) -> list[Candidate]:
        inbox = self._inbox
        problem = self._layout_problem()
        if problem:
            self._warn_once("layout", "Inbox %s disabled: %s; fix inbox_dir / library_dir / staging_dir", self.name, problem)
            self._last_poll, self._last_result = self._clock(), f"disabled: {problem}"
            return []
        inbox.mkdir(parents=True, exist_ok=True)
        now = self._clock()
        self._last_poll = now
        present: set[Path] = set()
        ready: list[Candidate] = []
        waiting = held = ignored = partial = 0  # for the Sources table: what the inbox sees but does not import yet
        for entry in sorted(inbox.iterdir(), key=lambda p: p.name):
            if entry.name.startswith((".", "_")) or entry.is_symlink():
                continue
            is_dir = entry.is_dir()
            if not is_dir:
                if not entry.is_file():
                    continue
                if _archive_suffix(entry.name) is None:
                    if entry.name.lower().endswith((".crdownload", ".part", ".tmp")):
                        partial += 1
                    else:
                        ignored += 1
                        self._note_once(f"skip:{entry.name}", "Inbox %s: ignoring %s: not a folder and not an archive (zip, rar, 7z, tar)",
                                        self.name, entry.name)
                    continue
            problem = self._entry_problem(entry)
            if problem:
                self._warn_once(f"entry:{entry}", "Inbox %s: ignoring %s: %s", self.name, entry, problem)
                continue
            try:
                sig = _tree_signature(entry)
            except OSError:
                continue
            present.add(entry)
            if self._handled.get(entry) == sig:
                held += 1
                continue
            self._handled.pop(entry, None)
            previous = self._seen.get(entry)
            self._seen[entry] = sig
            if previous is None:
                self._note_once(f"found:{entry.name}:{sig}", "Inbox %s: found %s; it is imported when unchanged for %ss and two scans in a row",
                                self.name, entry.name, self._cfg.settle_seconds)
            if previous != sig or now - sig[1] < self._cfg.settle_seconds:
                waiting += 1
                continue
            if is_dir:
                name = entry.name
            else:
                name = entry.name[: -len(_archive_suffix(entry.name) or "")]
            ready.append(Candidate(path=entry, name=name or entry.name, is_dir=is_dir, key=f"{self.name}:{entry.name}"))
        parts = []
        if ready:
            parts.append(f"{len(ready)} ready to import")
        if waiting:
            parts.append(f"{waiting} waiting until unchanged for {self._cfg.settle_seconds}s")
        if partial:
            parts.append(f"{partial} still downloading")
        if held:
            parts.append(f"{held} left in place (failed or already imported)")
        if ignored:
            parts.append(f"{ignored} ignored (not an archive)")
        text = ", ".join(parts) or "empty"
        self._last_result = f"{text} - last: {self._last_outcome}" if self._last_outcome else text
        for gone in set(self._seen) - present:
            del self._seen[gone]
        for gone in set(self._handled) - present:
            del self._handled[gone]
        return ready

    async def poll(self) -> list[Candidate]:
        return await asyncio.to_thread(self._scan)

    def status(self) -> dict[str, Any]:
        base = super().status()
        base.update(
            last_poll_at=self._last_poll,
            next_poll_at=(self._last_poll + self._cfg.scan_interval) if self._last_poll else None,  # about: the scan loop may wake earlier
            last_result=self._last_result,
        )
        return base

    def _finalize_sync(self, candidate: Candidate, outcome: Outcome) -> None:
        path = candidate.path
        self._last_outcome = f"{candidate.name} ({outcome})"
        if outcome == "deferred":
            return  # not processed (no space / stopped): leave it, it is offered again next poll
        problem = self._layout_problem() or self._entry_problem(path)
        if problem:
            log.error("Inbox %s: not deleting or moving %s: %s", self.name, path, problem)
            return
        self._seen.pop(path, None)
        if outcome in ("done", "empty"):
            if not path.exists() and not path.is_symlink():
                return
            if self._cfg.delete_source:
                _remove(path)
            else:
                target_dir = self._inbox / PROCESSED_DIR
                target_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), str(_unique_dest(target_dir, path.name)))
        elif outcome == "duplicate" and self._cfg.duplicate_action == "delete":
            _remove(path)
        else:
            # failed, or duplicate with action "keep": leave in place, don't re-poll until changed
            try:
                self._handled[path] = _tree_signature(path)
            except OSError:
                pass

    async def finalize(self, candidate: Candidate, outcome: Outcome) -> None:
        try:
            await asyncio.to_thread(self._finalize_sync, candidate, outcome)
        except Exception:
            log.exception("Failed to finalize inbox entry %s", candidate.path)

