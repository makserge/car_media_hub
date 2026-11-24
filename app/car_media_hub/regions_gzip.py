"""Gzip transfer copies ("sidecars") of the region files the car app downloads.

The car app installs ``routing.tar`` and ``places.sqlite`` exactly as they are; only the transfer is compressed. Next to
the plain file the add-on keeps::

    <regionId>/routing.tar.gz  places.sqlite.gz   (one gzip member, level 6, mtime 0, no file name: deterministic)
    <regionId>/transfer.json                      (size, mtime_ns and sha256 of every sidecar and the plain file it was
                                                   built from: sourceSha256, sourceSize; or {"skipped": true, ...})

A sidecar is offered in the catalogue (``"transfer"`` of a file entry) and served on ``/api/regions/files`` only while it
was built from the plain file with the SHA-256 the catalogue lists (``sourceSha256``) and is unchanged since it was
recorded. Stale or orphaned sidecars are deleted at a rescan, all of them when the option ``region_transfer_gzip`` is off.
Maps are never compressed (their tiles are gzip inside already). ``transfer.json`` is a file of its own so that
``files.json`` (the hash cache) never sees a ``.gz`` name.

One daemon thread (:class:`GzipWorker`) builds the sidecars one after the other, after the plain file is hashed and
settled, never while an upload or import writes that file. A result that is not clearly smaller (above 95 % of the plain
size) is recorded as ``skipped`` and not built again for the same plain file.
"""
from __future__ import annotations

import gzip
import hashlib
import logging
import os
import queue
import secrets
import stat
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .diskspace import NoSpaceError, ensure_free, human_bytes, is_no_space_error
from .regions import (
    FILE_NAMES, GZ_KINDS, GZ_NAMES, HASH_CHUNK, SHA256_RE, TRANSFER_FILES, TRANSFER_META, FileInfo, _flags, _stat_regular,
    read_hash_cache, read_small_json, valid_region_id, write_json_atomic,
)

log = logging.getLogger(__name__)

ENCODING = "gzip"
LEVEL = 6
MIN_BYTES = 1024 * 1024  # smaller files are sent plain (nothing worth saving)
WORTH = 0.95  # a sidecar above this share of the plain size is not built (recorded as skipped)
CHUNK = HASH_CHUNK
RETRY_S = 300.0  # a build that failed after real work is not started again for this long, doubling up to RETRY_MAX_S
RETRY_MAX_S = 6 * 3600.0


class _NotWorth(Exception):
    """The compressed output passed ``WORTH`` of the plain size (the output only grows, so the result would too)."""


class _Aborted(Exception):
    """The worker was stopped or the plain file changed while it was read."""


def _int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def read_transfer_meta(directory: Path) -> dict[str, dict[str, Any]]:
    """The valid entries of ``<directory>/transfer.json`` by sidecar name (anything malformed is left out; blocking)."""
    try:
        data = read_small_json(directory / TRANSFER_META)
    except (OSError, ValueError):
        return {}
    files = data.get("files") if isinstance(data, dict) and data.get("version") == 1 else None
    if not isinstance(files, dict):
        return {}
    good: dict[str, dict[str, Any]] = {}
    for name, entry in files.items():
        if name not in TRANSFER_FILES or not isinstance(entry, dict):
            continue
        src_sha, src_size = entry.get("sourceSha256"), entry.get("sourceSize")
        if not (isinstance(src_sha, str) and SHA256_RE.fullmatch(src_sha) and _int(src_size) and src_size > 0):
            continue
        if entry.get("skipped") is True:
            good[name] = {"skipped": True, "sourceSha256": src_sha, "sourceSize": src_size}
            continue
        size, mtime_ns, sha = entry.get("size"), entry.get("mtime_ns"), entry.get("sha256")
        if _int(size) and size > 0 and _int(mtime_ns) and isinstance(sha, str) and SHA256_RE.fullmatch(sha):
            good[name] = {"size": size, "mtime_ns": mtime_ns, "sha256": sha, "sourceSha256": src_sha, "sourceSize": src_size}
    return good


def sidecars(
    directory: Path, files: dict[str, FileInfo], meta: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, FileInfo], list[str], frozenset[str]]:
    """Look at the sidecars of one region folder (blocking).

    Returns (valid sidecars by kind, names of sidecar files that are stale or orphaned, kinds recorded as ``skipped`` for
    the current plain file). ``files`` are the plain files (as the snapshot knows them). A sidecar of a plain file whose
    hash is not known yet (being hashed again) is neither valid nor stale: it is not offered, and kept until the hash
    tells whether it still fits.
    """
    meta = read_transfer_meta(directory) if meta is None else meta
    valid: dict[str, FileInfo] = {}
    stale: list[str] = []
    skipped: set[str] = set()
    for kind in GZ_KINDS:
        name = GZ_NAMES[kind]
        plain = files.get(kind)
        entry = meta.get(name)
        current = entry is not None and plain is not None and plain.sha256 is not None and (
            entry["sourceSha256"] == plain.sha256 and entry["sourceSize"] == plain.size)
        if current and entry.get("skipped"):
            skipped.add(kind)
        st = _stat_regular(directory / name)
        if st is None:
            continue  # missing (a link or folder of that name is never opened by the file endpoint either)
        if plain is not None and plain.sha256 is None and entry is not None and not entry.get("skipped"):
            continue  # the plain file is being hashed again: decide later
        if current and not entry.get("skipped") and entry["size"] == st.st_size and entry["mtime_ns"] == st.st_mtime_ns:
            valid[kind] = FileInfo(kind, name, st.st_size, st.st_mtime_ns, entry["sha256"])
        else:
            stale.append(name)
    return valid, stale, frozenset(skipped)


def plain_infos(directory: Path) -> dict[str, FileInfo]:
    """The plain routing/places files of a folder as they are on disk now, with the hash from ``files.json`` when it
    still fits (blocking)."""
    cache = read_hash_cache(directory)
    out: dict[str, FileInfo] = {}
    for kind in GZ_KINDS:
        name = FILE_NAMES[kind]
        st = _stat_regular(directory / name)
        if st is None or st.st_size <= 0:
            continue
        cached = cache.get(name)
        sha = cached["sha256"] if cached and cached["size"] == st.st_size and cached["mtime_ns"] == st.st_mtime_ns else None
        out[kind] = FileInfo(kind, name, st.st_size, st.st_mtime_ns, sha)
    return out


def _unlink(path: Path) -> bool:
    try:
        os.unlink(path)
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        log.warning("Regions: cannot delete %s: %s", path.name, exc.strerror or exc)
        return False


class _Sink:
    """The file object gzip writes into: counts and hashes the compressed bytes, stops above ``limit``."""

    def __init__(self, handle: Any, limit: int) -> None:
        self._handle = handle
        self._limit = limit
        self.digest = hashlib.sha256()
        self.size = 0

    def write(self, data: Any) -> int:
        view = memoryview(data).cast("B")
        self.size += len(view)
        if self.size > self._limit:
            raise _NotWorth()
        self.digest.update(view)
        self._handle.write(view)
        return len(view)

    def flush(self) -> None:
        self._handle.flush()


class GzipWorker:
    """One daemon thread that builds the gzip sidecars one after the other (see the module documentation).

    Every write and delete of a sidecar or of ``transfer.json`` happens under :attr:`lock`, so a rescan never deletes a
    sidecar that was just published, and a published sidecar always has its ``transfer.json`` entry first.
    """

    def __init__(
        self, root: Path, on_done: Callable[[], None], *, enabled: bool = True, min_free: int = 0,
        is_busy: Callable[[str, str], bool] = lambda region, kind: False, level: int = LEVEL,
    ) -> None:
        self._root = Path(root)
        self._on_done = on_done
        self.enabled = enabled
        self.min_free = min_free
        self._is_busy = is_busy
        self.level = level
        self.lock = threading.Lock()
        self._queue: queue.Queue[tuple[str, str] | None] = queue.Queue()
        self._pending: set[tuple[str, str]] = set()
        self._failures: dict[tuple[str, str], tuple[int, float]] = {}  # failed after real work: count, last time
        self._cancelled: set[tuple[str, str]] = set()  # pending builds whose plain file was deleted: stop at the next chunk
        self._progress: dict[tuple[str, str], float] = {}
        self._state_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stopped = False
        self.after_chunk: Callable[[str, str], None] | None = None  # test hook: called after every chunk read
        self.builds = 0  # finished build attempts that wrote something (for tests and the log)

    # -- queue
    def submit(self, region_id: str, kind: str) -> None:
        key = (region_id, kind)
        if kind not in GZ_KINDS or not valid_region_id(region_id):
            return
        with self._state_lock:
            if key in self._pending or self._stopped or not self.enabled:
                return
            self._pending.add(key)
            self._progress[key] = 0.0
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="regions-gzip", daemon=True)
                self._thread.start()
        self._queue.put(key)

    def cancel(self, region_id: str, kind: str | None = None) -> None:
        """Stop the running or queued build of a file whose plain file is gone (a region delete or a file delete)."""
        with self._state_lock:
            for k in (GZ_KINDS if kind is None else (kind,)):
                if (region_id, k) in self._pending:
                    self._cancelled.add((region_id, k))

    def backing_off(self, region_id: str, kind: str) -> bool:
        """True while the last build of this file failed after real work (a read error, the disk filled up half way, the file
        changed): a rescan every few minutes must not start a multi-gigabyte compression again and again."""
        with self._state_lock:
            count, last = self._failures.get((region_id, kind), (0, 0.0))
        return count > 0 and time.monotonic() - last < min(RETRY_MAX_S, RETRY_S * 2 ** (count - 1))

    def _note_failure(self, key: tuple[str, str]) -> None:
        with self._state_lock:
            count, _ = self._failures.get(key, (0, 0.0))
            self._failures[key] = (count + 1, time.monotonic())

    def progress(self, region_id: str, kind: str) -> float | None:
        with self._state_lock:
            return self._progress.get((region_id, kind))

    def busy(self) -> bool:
        with self._state_lock:
            return bool(self._pending)

    def stop(self) -> None:
        with self._state_lock:
            self._stopped = True
        self._queue.put(None)
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)

    def _run(self) -> None:
        while True:
            key = self._queue.get()
            if key is None or self._stopped:
                return
            changed = False
            try:
                changed = self._build(*key)
            except Exception:  # noqa: BLE001 - one unreadable file must not stop the worker
                self._note_failure(key)
                log.warning("Regions: compressing %s/%s failed", key[0], FILE_NAMES.get(key[1], key[1]), exc_info=True)
            finally:
                with self._state_lock:
                    self._pending.discard(key)
                    self._cancelled.discard(key)
                    self._progress.pop(key, None)
            if changed:
                try:
                    self._on_done()
                except Exception:  # noqa: BLE001
                    log.debug("Regions: after-gzip callback failed", exc_info=True)

    # -- maintenance (blocking, called from the manager in a worker thread)
    def sweep(self, region_id: str, everything: bool = False) -> int:
        """Delete the stale or orphaned sidecars of one region (all sidecars and ``transfer.json`` with ``everything``)
        and drop the ``transfer.json`` entries that no longer fit. Looks at the disk again under the lock. Returns the
        number of files deleted."""
        if not valid_region_id(region_id):
            return 0
        directory = self._root / region_id
        removed = 0
        with self.lock:
            if everything:
                for name in TRANSFER_FILES:
                    removed += _unlink(directory / name)
                removed += _unlink(directory / TRANSFER_META)
                return removed
            plain = plain_infos(directory)
            meta = read_transfer_meta(directory)
            valid, stale, skipped = sidecars(directory, plain, meta)
            for name in stale:
                if _unlink(directory / name):
                    removed += 1
                    log.info("Regions: removed the stale compressed copy %s/%s", region_id, name)
            keep = {}
            for name, entry in meta.items():
                kind = TRANSFER_FILES[name]
                info = plain.get(kind)
                if kind in valid or kind in skipped:
                    keep[name] = entry
                elif info is not None and info.sha256 is None and entry["sourceSize"] == info.size:
                    keep[name] = entry  # the plain file is being hashed again: decide later
            if keep != meta:
                self._write_meta(directory, keep)
        return removed

    def remove(self, region_id: str, kind: str) -> None:
        """Delete the sidecar of one file kind and its ``transfer.json`` entry (the plain file was deleted; blocking)."""
        if kind not in GZ_KINDS or not valid_region_id(region_id):
            return
        directory = self._root / region_id
        name = GZ_NAMES[kind]
        with self.lock:
            _unlink(directory / name)
            meta = read_transfer_meta(directory)
            if meta.pop(name, None) is not None:
                self._write_meta(directory, meta)

    @staticmethod
    def _write_meta(directory: Path, meta: dict[str, dict[str, Any]]) -> None:
        if not directory.is_dir():
            return
        if meta:
            write_json_atomic(directory / TRANSFER_META, {"version": 1, "files": meta})
        else:
            _unlink(directory / TRANSFER_META)

    # -- build
    def _build(self, region_id: str, kind: str) -> bool:
        """Build the sidecar of one plain file when it is missing or stale. Returns True when something changed."""
        if not self.enabled or self._stopped or (region_id, kind) in self._cancelled or self._is_busy(region_id, kind):
            return False
        directory = self._root / region_id
        plain_path = directory / FILE_NAMES[kind]
        name = GZ_NAMES[kind]
        try:
            fd = os.open(plain_path, _flags())
        except OSError:
            return False
        tmp: Path | None = None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_size < max(1, MIN_BYTES):
                return False
            cached = read_hash_cache(directory).get(FILE_NAMES[kind])
            if cached is None or cached["size"] != st.st_size or cached["mtime_ns"] != st.st_mtime_ns:
                return False  # not hashed (yet): the next rescan asks again
            source_sha = cached["sha256"]
            entry = read_transfer_meta(directory).get(name)
            if entry is not None and entry["sourceSha256"] == source_sha and entry["sourceSize"] == st.st_size:
                if entry.get("skipped"):
                    return False
                gst = _stat_regular(directory / name)
                if gst is not None and gst.st_size == entry["size"] and gst.st_mtime_ns == entry["mtime_ns"]:
                    return False  # built already
            try:
                ensure_free(directory, st.st_size, self.min_free)
            except NoSpaceError as exc:
                log.info("Regions: %s/%s is not compressed now: %s", region_id, name, exc)
                return False
            tmp = directory / f".gzip-{kind}-{secrets.token_hex(4)}.part"
            out_fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0), 0o644)
            limit = int(st.st_size * WORTH)
            plain_digest = hashlib.sha256()
            done = 0
            try:
                with os.fdopen(out_fd, "wb") as out, os.fdopen(fd, "rb", buffering=0) as src:
                    fd = -1
                    sink = _Sink(out, limit)
                    try:
                        with gzip.GzipFile(filename="", mode="wb", compresslevel=self.level, fileobj=sink, mtime=0) as gz:
                            while True:
                                if self._stopped or not self.enabled or (region_id, kind) in self._cancelled:
                                    raise _Aborted("stopped")
                                chunk = src.read(CHUNK)
                                if not chunk:
                                    break
                                plain_digest.update(chunk)
                                gz.write(chunk)
                                done += len(chunk)
                                with self._state_lock:
                                    self._progress[(region_id, kind)] = min(0.999, done / max(1, st.st_size))
                                if self.after_chunk is not None:
                                    self.after_chunk(region_id, kind)
                    except _NotWorth:
                        if self._stopped or not self.enabled or (region_id, kind) in self._cancelled:
                            return False  # stopped: the trailer written while unwinding may have passed the limit
                        return self._record_skipped(region_id, directory, name, source_sha, st)
                    after = os.fstat(src.fileno())
                    out.flush()
                    os.fsync(out.fileno())
                    gz_st = os.fstat(out.fileno())
            except _Aborted:
                return False
            except OSError as exc:
                if is_no_space_error(exc):
                    log.info("Regions: %s/%s is not compressed now: the disk is full", region_id, name)
                    self._note_failure((region_id, kind))
                    return False
                raise
            now = _stat_regular(plain_path)
            if (done != st.st_size or plain_digest.hexdigest() != source_sha or after.st_size != st.st_size
                    or after.st_mtime_ns != st.st_mtime_ns or now is None or now.st_size != st.st_size
                    or now.st_mtime_ns != st.st_mtime_ns):
                log.info("Regions: %s/%s changed while it was compressed: not used", region_id, FILE_NAMES[kind])
                self._note_failure((region_id, kind))
                return False
            record = {"size": gz_st.st_size, "mtime_ns": gz_st.st_mtime_ns, "sha256": sink.digest.hexdigest(),
                      "sourceSha256": source_sha, "sourceSize": st.st_size}
            with self.lock:
                again = _stat_regular(plain_path)
                if again is None or again.st_size != st.st_size or again.st_mtime_ns != st.st_mtime_ns:
                    return False
                meta = read_transfer_meta(directory)
                meta[name] = record
                self._write_meta(directory, meta)  # the entry first: a rescan never sees a published file without it
                os.replace(tmp, directory / name)
                tmp = None
            self.builds += 1
            with self._state_lock:
                self._failures.pop((region_id, kind), None)
            log.info("Regions: compressed %s/%s: %s -> %s", region_id, FILE_NAMES[kind], human_bytes(st.st_size),
                     human_bytes(gz_st.st_size))
            return True
        finally:
            if fd >= 0:
                os.close(fd)
            if tmp is not None:
                _unlink(tmp)

    def _record_skipped(self, region_id: str, directory: Path, name: str, source_sha: str, st: os.stat_result) -> bool:
        with self.lock:
            again = _stat_regular(directory / FILE_NAMES[TRANSFER_FILES[name]])
            if again is None or again.st_size != st.st_size or again.st_mtime_ns != st.st_mtime_ns:
                return False
            _unlink(directory / name)
            meta = read_transfer_meta(directory)
            meta[name] = {"skipped": True, "sourceSha256": source_sha, "sourceSize": st.st_size}
            self._write_meta(directory, meta)
        self.builds += 1
        log.info("Regions: %s/%s does not compress well: sent plain", region_id, FILE_NAMES[TRANSFER_FILES[name]])
        return True
