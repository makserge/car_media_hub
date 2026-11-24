"""Offline map regions: storage, manifests, hashing and the catalogue served to the car app.

Layout (``region_storage_dir``, default ``/share/car_media_hub/regions``)::

    <regionId>/map.pmtiles  routing.tar  places.sqlite   (up to three files, none required except for the catalogue: map)
    <regionId>/manifest.json                              (name, bounds, version, source, license, routingGroup)
    <regionId>/files.json                                 (add-on cache: size, mtime and sha256 of every file)
    <regionId>/routing.tar.gz  places.sqlite.gz  transfer.json   (gzip transfer copies, option region_transfer_gzip:
                                                          see :mod:`car_media_hub.regions_gzip`)

``RegionManager`` owns a snapshot of that folder (rebuilt on demand, after every change and every
``RESCAN_SECONDS``), a single hash worker thread (so the first catalogue call on a 4 GB file never blocks the
event loop; the catalogue says ``hashing`` with a progress until the hash is known) and the admin operations
(create, delete, edit, upload, import from URL) used by :mod:`car_media_hub.regions_web`.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import math
import os
import queue
import re
import secrets
import shutil
import stat
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiohttp

from .diskspace import NoSpaceError, ensure_free, human_bytes, is_no_space_error
from .streams import STALL_SECONDS, Stream, StreamBudget  # noqa: F401 - STALL_SECONDS is re-exported

log = logging.getLogger(__name__)

REGION_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{1,40}")
KINDS = ("map", "routing", "places")  # the order of the catalogue (the app sorts the same way)
FILE_NAMES = {"map": "map.pmtiles", "routing": "routing.tar", "places": "places.sqlite"}
KIND_OF_FILE = {name: kind for kind, name in FILE_NAMES.items()}
ALLOWED_FILES = frozenset(FILE_NAMES.values())
# gzip transfer copies of the plain files (see regions_gzip.py): the app downloads these and installs the plain file
GZ_KINDS = ("routing", "places")
GZ_NAMES = {"routing": "routing.tar.gz", "places": "places.sqlite.gz"}
TRANSFER_FILES = {name: kind for kind, name in GZ_NAMES.items()}
SERVED_FILES = ALLOWED_FILES | frozenset(TRANSFER_FILES)  # what /api/regions/files serves
TRANSFER_META = "transfer.json"  # add-on cache of the sidecars (size, mtime, sha256, the plain file they were built from)
# the name the app stores a file under (its folders are per kind, so these must be unique per region)
CATALOGUE_NAMES = {"map": "{id}.pmtiles", "routing": "{id}-tiles.tar", "places": "{id}-places.sqlite"}
MANIFEST = "manifest.json"
HASH_CACHE = "files.json"
WANT_FILE = "want.json"  # desired state for the car app: wanted, wantedAt, wantedSeq, wantedBy, removeRequested, removeAt, cancelled
IMPORT_QUEUE = ".import-queue.json"  # in the regions folder (not a region id): the imports that are running or waiting
DEVICES_DIR = ".devices"  # one JSON file per reporting device (never a valid region id)
REMOVE_KEEP_SECONDS = 30 * 24 * 3600  # a "remove from car app" request that nobody confirmed expires after 30 days
FILES_URL = "/api/regions/files"

RESCAN_SECONDS = 600.0
IMPORT_RETRY_DELAYS = (5.0, 15.0, 45.0, 120.0, 300.0)  # pauses before an import from a URL resumes after a lost connection
SYNC_BYTES = 64 * 1024 * 1024  # an import part is fsynced (and its safe length noted) after this many bytes
SETTLE_SECONDS = 30.0  # a file younger than this may still be copied (Samba): it is not hashed, offered or served yet
HASH_CHUNK = 1 << 20
MAX_JSON_BYTES = 1 << 20
STALE_PART_SECONDS = 24 * 3600  # an unfinished .upload-* / .import-* file older than this is deleted
FREE_CHECK_BYTES = 64 * 1024 * 1024  # free space is checked again after every this many streamed bytes
FREE_RESERVE = 256 * 1024 * 1024  # default reserve (main.py passes min_free_space_gb)
IMPORT_MAX_SECONDS = 6 * 3600  # default overall time limit of one import from a URL
PMTILES_HEADER = 127
SQLITE_HEADER = 100
TAR_BLOCK = 512
MAX_NAME = 120
MAX_TEXT = 2000
SHA256_RE = re.compile(r"[0-9a-f]{64}")


class RegionError(ValueError):
    """A request the admin side refuses; ``status`` is the HTTP status the page answers with."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class TransferInterrupted(RegionError):
    """A download that ended early (the connection broke); the part is kept and the transfer resumes."""


# ------------------------------------------------------------------ small helpers


def valid_region_id(value: object) -> bool:
    return isinstance(value, str) and REGION_ID_RE.fullmatch(value) is not None


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _flags(extra: int = 0) -> int:
    return os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0) | extra


def read_small_json(path: Path) -> Any:
    """The JSON document at ``path`` (regular file, no symlink, at most 1 MiB); raises OSError/ValueError."""
    fd = os.open(path, _flags())
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("not a regular file")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            raw = handle.read(MAX_JSON_BYTES + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError("file too large")
    return json.loads(raw.decode("utf-8"))


def write_json_atomic(path: Path, data: Any) -> None:
    """Write ``data`` next to ``path`` and rename it over it (never follows a link at the temp name)."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0), 0o644)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True).encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ------------------------------------------------------------------ manifest


def _text(value: Any, name: str, limit: int, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise RegionError(f"{name} must be text")
    value = value.strip()
    if len(value) > limit or any(ch < " " and ch not in "\n\t" for ch in value):
        raise RegionError(f"{name} must be text of at most {limit} characters without control characters")
    if required and not value:
        raise RegionError(f"{name} is required")
    return value


def _bounds(value: Any) -> list[float] | None:
    if value is None or value == []:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise RegionError("bounds must be [west, south, east, north]")
    nums = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item):
            raise RegionError("bounds must be four numbers")
        nums.append(float(item))
    west, south, east, north = nums
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise RegionError("bounds must satisfy -180 <= west < east <= 180 and -90 <= south < north <= 90")
    return nums


def normalise_manifest(data: Any, region_id: str, *, now: float | None = None, strict: bool = True) -> dict[str, Any]:
    """The cleaned manifest (only the known keys) or raises :class:`RegionError`.

    ``name`` may be a text (used for both languages) or ``{"de": ..., "en": ...}``; at least one language is needed.
    """
    if not isinstance(data, dict):
        raise RegionError("the manifest must be a JSON object")
    if strict and data.get("id", region_id) != region_id:
        raise RegionError("the manifest id must be the name of the region folder")
    raw_name = data.get("name")
    if isinstance(raw_name, str):
        raw_name = {"de": raw_name, "en": raw_name}
    if not isinstance(raw_name, dict):
        raise RegionError("name must be a text or {de, en}")
    de = _text(raw_name.get("de"), "name.de", MAX_NAME)
    en = _text(raw_name.get("en"), "name.en", MAX_NAME)
    if not de and not en:
        raise RegionError("name is required (de or en)")
    group = data.get("routingGroup")
    if group in (None, ""):
        group = None
    elif not valid_region_id(group):
        raise RegionError("routingGroup must match [a-z0-9][a-z0-9_-]{1,40}")
    created = _text(data.get("created"), "created", 40) or _iso(now if now is not None else time.time())
    return {
        "id": region_id,
        "name": {"de": de or en, "en": en or de},
        "bounds": _bounds(data.get("bounds")),
        "version": _text(data.get("version"), "version", 40),
        "source": _text(data.get("source"), "source", MAX_TEXT),
        "license": _text(data.get("license"), "license", MAX_TEXT),
        "created": created,
        "routingGroup": group,
    }


# ------------------------------------------------------------------ file validation


def _tar_checksum_ok(block: bytes) -> bool:
    """The checksum field (offset 148, 8 bytes, octal) of a tar header equals the sum of its bytes (field as spaces)."""
    field_ = block[148:156].split(b"\x00")[0].strip(b" ")
    if not field_ or any(c not in b"01234567" for c in field_):
        return False
    wanted = int(field_, 8)
    probe = block[:148] + b" " * 8 + block[156:512]
    return wanted in (sum(probe), sum(b if b < 128 else b - 256 for b in probe))


def validate_head(kind: str, head: bytes, size: int | None = None) -> None:
    """Raise :class:`RegionError` unless the first bytes of a file fit its kind.

    PMTiles: magic, version 3 and a complete 127 byte header; SQLite: magic and a valid page size field; tar: ``ustar``
    and the checksum of the first header block.
    """
    if kind == "map":
        if len(head) < 8 or head[:7] != b"PMTiles" or head[7] != 3:
            raise RegionError("this is not a PMTiles version 3 file (magic 'PMTiles' + 3 missing)", 415)
        if len(head) < PMTILES_HEADER or (size is not None and size < PMTILES_HEADER):
            raise RegionError("the PMTiles header is incomplete (the file is too short)", 415)
    elif kind == "places":
        if head[:16] != b"SQLite format 3\x00":
            raise RegionError("this is not an SQLite database file", 415)
        page = int.from_bytes(head[16:18], "big") if len(head) >= 18 else -1
        if not (page == 1 or (512 <= page <= 32768 and page & (page - 1) == 0)):
            raise RegionError("the SQLite header has an invalid page size", 415)
        if len(head) < SQLITE_HEADER or (size is not None and size < SQLITE_HEADER):
            raise RegionError("the SQLite header is incomplete (the file is too short)", 415)
    elif kind == "routing":
        if len(head) < TAR_BLOCK or head[257:262] != b"ustar":
            raise RegionError("this is not a tar archive (ustar header missing)", 415)
        if not _tar_checksum_ok(head[:TAR_BLOCK]):
            raise RegionError("this is not a valid tar archive (the checksum of the first header is wrong)", 415)
    else:
        raise RegionError("unknown file kind", 404)
    if size is not None and size <= 0:
        raise RegionError("the file is empty", 415)


def head_bytes_needed(kind: str) -> int:
    return {"routing": TAR_BLOCK, "map": PMTILES_HEADER, "places": SQLITE_HEADER}.get(kind, 16)


def open_region_file(root: Path, region_id: str, name: str) -> Any:
    """Open ``<root>/<region_id>/<name>`` for reading (blocking) without following a link at the folder or the file.

    Only a valid id and one of the three file names (or a gzip transfer copy of one) are accepted. Raises OSError (also
    for a link or a non-regular file), never returns anything but a regular file.
    """
    if not valid_region_id(region_id) or name not in SERVED_FILES:
        raise FileNotFoundError(name)
    directory = Path(root) / region_id
    if os.open in os.supports_dir_fd and hasattr(os, "O_DIRECTORY"):
        dfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
        try:
            fd = os.open(name, _flags(), dir_fd=dfd)
        finally:
            os.close(dfd)
    else:  # Windows (tests): no descriptor-relative open
        if stat.S_ISLNK(os.lstat(directory).st_mode):
            raise FileNotFoundError(region_id)
        fd = os.open(directory / name, _flags())
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise IsADirectoryError(name)
        return os.fdopen(fd, "rb", buffering=0)
    except BaseException:
        os.close(fd)
        raise


# ------------------------------------------------------------------ snapshot model


@dataclass(frozen=True)
class FileInfo:
    kind: str
    file: str
    size: int
    mtime_ns: int
    sha256: str | None
    settling: bool = False

    @property
    def etag(self) -> str:
        """Strong ETag of the file: its SHA-256 in quotes (the file endpoint sends the same value)."""
        return f'"{self.sha256}"'


@dataclass
class Region:
    id: str
    manifest: dict[str, Any]
    manifest_problem: str | None
    files: dict[str, FileInfo] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    updated: float = 0.0
    want: dict[str, Any] = field(default_factory=dict)
    parts: list[dict[str, Any]] = field(default_factory=list)  # unfinished .upload-* / .import-* files
    # gzip transfer copies (only with the option region_transfer_gzip): the valid ones by kind, the names of stale or
    # orphaned ones on disk (deleted by the manager), the kinds recorded as "not worth it" for the current plain file,
    # and whether any sidecar or transfer.json is on disk at all (all of it is deleted while the option is off)
    transfers: dict[str, FileInfo] = field(default_factory=dict)
    transfer_stale: list[str] = field(default_factory=list)
    transfer_skipped: frozenset[str] = frozenset()
    transfer_present: bool = False


def _stat_regular(path: Path) -> os.stat_result | None:
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return st if stat.S_ISREG(st.st_mode) else None


def read_hash_cache(directory: Path) -> dict[str, dict[str, Any]]:
    try:
        data = read_small_json(directory / HASH_CACHE)
    except (OSError, ValueError):
        return {}
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict):
        return {}
    good = {}
    for name, entry in files.items():
        if (
            name in ALLOWED_FILES and isinstance(entry, dict)
            and isinstance(entry.get("size"), int) and isinstance(entry.get("mtime_ns"), int)
            and isinstance(entry.get("sha256"), str) and SHA256_RE.fullmatch(entry["sha256"])
        ):
            good[name] = entry
    return good


PART_RE = re.compile(r"\.(import|upload)-(map|routing|places)(?:-[0-9a-f]{8})?\.part")
GZIP_PART_RE = re.compile(r"\.gzip-(routing|places)-[0-9a-f]{8}\.part")  # a gzip copy being built (regions_gzip.py)
IMPORT_META_RE = re.compile(r"\.import-(map|routing|places)\.json")


def _scan_parts(directory: Path) -> list[dict[str, Any]]:
    """The unfinished transfer files of a region folder (blocking): name, origin (import/upload), kind, size, mtime."""
    out: list[dict[str, Any]] = []
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return out
    for entry in entries:
        match = PART_RE.fullmatch(entry.name)
        if match is None:
            continue
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode):
            out.append({"name": entry.name, "origin": match.group(1), "kind": match.group(2), "size": st.st_size, "mtime": st.st_mtime})
    return sorted(out, key=lambda p: p["name"])


def _want_text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(ch for ch in value if ch >= " " and ch != "\x7f").strip()[:limit]


def _num_ts(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return 0.0
    return float(value)


def normalise_want(data: Any, now: float) -> dict[str, Any]:
    """The desired state of a region for the car app from ``want.json`` (anything wrong means "not wanted").

    ``removeRequested`` is only true while the request is younger than 30 days. An install request never expires (it
    stays until the owner cancels it or asks for a removal); ``cancelled`` is the owner's "Cancel install" and stays
    until a new install or removal request replaces it (it is a state of its own, so a car app that is in the middle of
    the install can tell "cancelled" from "never requested").
    """
    if not isinstance(data, dict):
        data = {}
    remove_ts = _num_ts(data.get("removeAtTs"))
    remove = data.get("removeRequested") is True and remove_ts > 0 and now - remove_ts < REMOVE_KEEP_SECONDS
    wanted = data.get("wanted") is True and not remove
    cancel_ts = _num_ts(data.get("cancelAtTs"))
    cancelled = data.get("cancelled") is True and not wanted and not remove and cancel_ts > 0
    seq = data.get("wantedSeq")
    return {
        "wanted": wanted, "wantedAt": _want_text(data.get("wantedAt"), 40), "wantedBy": _want_text(data.get("wantedBy"), 80),
        "wantedSeq": seq if isinstance(seq, int) and not isinstance(seq, bool) and seq > 0 and wanted else 0,
        "removeRequested": remove, "removeAt": _want_text(data.get("removeAt"), 40) if remove else "",
        "removeAtTs": remove_ts if remove else 0.0,
        "cancelled": cancelled, "cancelAt": _want_text(data.get("cancelAt"), 40) if cancelled else "",
        "cancelAtTs": cancel_ts if cancelled else 0.0,
    }


def _stamp(now: float, *previous: Any) -> tuple[str, float]:
    """The revision text (UTC, seconds) for a new request: strictly later than every earlier revision of the region,
    so a cancel and a new install within one second still differ (the app compares revisions, not clocks)."""
    ts = float(int(now))
    for text in previous:
        if isinstance(text, str) and text:
            try:
                ts = max(ts, datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp() + 1)
            except ValueError:
                pass
    return _iso(ts), ts


def _max_want_seq(root: Path) -> int:
    """The largest request sequence number in use (blocking)."""
    best = 0
    try:
        entries = list(os.scandir(root))
    except OSError:
        return 0
    for entry in entries:
        if valid_region_id(entry.name):
            seq = _read_raw_want(Path(entry.path)).get("wantedSeq")
            if isinstance(seq, int) and not isinstance(seq, bool):
                best = max(best, seq)
    return best


def _read_raw_want(directory: Path) -> dict[str, Any]:
    try:
        data = read_small_json(directory / WANT_FILE)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def read_want(directory: Path, now: float) -> dict[str, Any]:
    try:
        return normalise_want(read_small_json(directory / WANT_FILE), now)
    except (OSError, ValueError):
        return normalise_want(None, now)


def scan_region_dir(root: Path, now: float, transfers: bool = False) -> tuple[list[Region], list[tuple[str, str]]]:
    """Read every region folder below ``root`` (blocking). Returns (regions, files that still need a hash).

    ``transfers``: also look at the gzip transfer copies (option ``region_transfer_gzip``); without it no region has any.
    """
    regions: list[Region] = []
    to_hash: list[tuple[str, str]] = []
    entries = sorted(os.scandir(root), key=lambda e: e.name)  # a missing or unreadable folder raises OSError
    for entry in entries:
        if not valid_region_id(entry.name):
            continue
        try:
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        directory = Path(entry.path)
        region = _read_region(directory, entry.name, now, transfers)
        regions.append(region)
        for info in region.files.values():
            if info.sha256 is None and not info.settling:
                to_hash.append((region.id, info.file))
    return regions, to_hash


def _adopt_legacy_names(directory: Path, region_id: str) -> None:
    """``tools/build_region_pack.py`` names (``<id>.pmtiles``, ``<id>-tiles.tar``, ``<id>-places.sqlite``) become the
    canonical names when the canonical file is missing, so a pack folder can be copied as it is."""
    for kind, template in CATALOGUE_NAMES.items():
        legacy = directory / template.format(id=region_id)
        canonical = directory / FILE_NAMES[kind]
        if _stat_regular(legacy) is not None and _stat_regular(canonical) is None and not canonical.exists():
            try:
                os.rename(legacy, canonical)
                log.info("Regions: %s: renamed %s to %s", region_id, legacy.name, canonical.name)
            except OSError as exc:
                log.warning("Regions: %s: cannot rename %s: %s", region_id, legacy.name, exc.strerror or exc)


def _read_region(directory: Path, region_id: str, now: float, transfers: bool = False) -> Region:
    _adopt_legacy_names(directory, region_id)
    problems: list[str] = []
    manifest_problem: str | None = None
    manifest_path = directory / MANIFEST
    mst = _stat_regular(manifest_path)
    try:
        if mst is None:
            raise RegionError("manifest.json is missing")
        manifest = normalise_manifest(read_small_json(manifest_path), region_id, now=mst.st_mtime)
    except (OSError, ValueError) as exc:
        manifest_problem = str(exc) or type(exc).__name__
        manifest = {
            "id": region_id, "name": {"de": region_id, "en": region_id}, "bounds": None, "version": "",
            "source": "", "license": "", "created": "", "routingGroup": None,
        }
    cache = read_hash_cache(directory)
    files: dict[str, FileInfo] = {}
    updated = mst.st_mtime if mst is not None else 0.0
    for kind in KINDS:
        name = FILE_NAMES[kind]
        path = directory / name
        st = _stat_regular(path)
        if st is None:
            if os.path.lexists(path):
                problems.append(f"{name} is not a regular file (links and folders are ignored)")
            continue
        if st.st_size <= 0:
            problems.append(f"{name} is empty and ignored")
            continue
        cached = cache.get(name)
        sha = cached["sha256"] if cached and cached["size"] == st.st_size and cached["mtime_ns"] == st.st_mtime_ns else None
        settling = sha is None and now - st.st_mtime < SETTLE_SECONDS
        files[kind] = FileInfo(kind, name, st.st_size, st.st_mtime_ns, sha, settling)
        updated = max(updated, st.st_mtime)
    region = Region(region_id, manifest, manifest_problem, files, problems, updated, read_want(directory, now), _scan_parts(directory))
    region.transfer_present = any(os.path.lexists(directory / n) for n in (*TRANSFER_FILES, TRANSFER_META))
    if transfers and region.transfer_present:
        from .regions_gzip import sidecars

        region.transfers, region.transfer_stale, region.transfer_skipped = sidecars(directory, files)
    return region


# ------------------------------------------------------------------ catalogue


def grouped_routing_name(group: str, sha256: str | None, own_names: dict[str, str | None] | None = None) -> str:
    """The catalogue name of a routing file shared by a routing group: ``<group>-<sha8>-tiles.tar``.

    The name depends on the content, so two different routing files never share a name (the app keeps one file per
    name). ``own_names`` maps the names regions use for their own routing file to their hash; on the (very unlikely)
    clash with a different file the hash part is made longer.
    """
    if not sha256:
        return CATALOGUE_NAMES["routing"].format(id=group)  # not listed anyway: the file is still being hashed
    for length in (8, 16, 24, 32, 64):
        name = CATALOGUE_NAMES["routing"].format(id=f"{group}-{sha256[:length]}")
        other = (own_names or {}).get(name)
        if other is None or other == sha256:
            return name
    return CATALOGUE_NAMES["routing"].format(id=f"{group}-{sha256}")


def region_version(region: Region) -> str:
    """The version the catalogue shows for a region (the manifest's, else the date of the newest file)."""
    return region.manifest["version"] or _iso(region.updated)[:10]


def install_order(regions: list[Region]) -> dict[str, int]:
    """Region id -> 1-based position in the install queue (wanted regions only; the sequence number of the request, then
    its time, then the id, so a want.json without a sequence still has a stable order)."""
    wanted = [r for r in regions if r.want.get("wanted")]
    wanted.sort(key=lambda r: (r.want.get("wantedSeq", 0) or 1 << 62, r.want.get("wantedAt", ""), r.id))
    return {r.id: i for i, r in enumerate(wanted, 1)}


def build_catalogue(
    regions: list[Region], progress: Callable[[str, str], float | None], now: float,
    building: frozenset[tuple[str, str]] = frozenset(),
) -> dict[str, Any]:
    """The JSON document of ``GET /api/regions/catalogue`` (compatible with the app's ``regions.json`` v2).

    Per file: ``kind``, ``name``, ``url``, ``bytes`` (``sizeBytes`` repeats it), ``sha256``, and for routing/places with a
    valid gzip copy ``transfer: {encoding: "gzip", url, bytes, sha256}`` (of the ``.gz`` bytes). A region whose hash is not
    known yet has ``state: "hashing"``, ``files: []`` (the app parser skips an entry without files) and ``hashing`` with
    the progress of each file; only regions that have a map file are listed. ``install`` is true when the owner chose
    "Install on car app" (and the region is ready), ``remove`` when the owner chose "Remove from car app".
    ``building`` holds the ``(region, kind)`` pairs an upload or import is writing right now: a region that gets a *new*
    file kind that way is ``state: "updating"`` with ``files: []`` (not offered, like ``hashing``) until the file is
    in place and hashed; a file that is being *replaced* keeps its old complete file listed and served until the
    atomic rename.
    """
    # the provider of a routing group: the first region (by id) that holds routing.tar
    providers: dict[str, Region] = {}
    for region in regions:
        group = region.manifest.get("routingGroup")
        if group and "routing" in region.files and group not in providers:
            providers[group] = region
    # names of the routing files regions offer as their own: a group name must never equal one of them
    own_names: dict[str, str | None] = {}
    for region in regions:
        group = region.manifest.get("routingGroup")
        if "routing" in region.files and not (group and group in providers):
            own_names[CATALOGUE_NAMES["routing"].format(id=region.id)] = region.files["routing"].sha256
    # the order in which the owner asked for installs (a request made later is behind the earlier ones)
    order = install_order(regions)
    out: list[dict[str, Any]] = []
    for region in regions:
        if "map" not in region.files:
            continue
        group = region.manifest.get("routingGroup")
        sources: dict[str, tuple[Region, FileInfo]] = {k: (region, f) for k, f in region.files.items()}
        grouped = bool(group and group in providers)
        routing_name = CATALOGUE_NAMES["routing"].format(id=region.id)
        if grouped:
            provider = providers[group]
            sources["routing"] = (provider, provider.files["routing"])
            routing_name = grouped_routing_name(group, provider.files["routing"].sha256, own_names)
        entry_files: list[dict[str, Any]] = []
        hashing: list[dict[str, Any]] = []
        for kind in KINDS:
            if kind not in sources:
                continue
            owner, info = sources[kind]
            if info.sha256 is None:
                done = 0.0 if info.settling else (progress(owner.id, info.file) or 0.0)
                hashing.append({"kind": kind, "progress": round(done, 3)})
                continue
            name = routing_name if kind == "routing" else CATALOGUE_NAMES[kind].format(id=region.id)
            item: dict[str, Any] = {
                "kind": kind,
                "name": name,
                "url": f"{FILES_URL}/{owner.id}/{info.file}",
                "bytes": info.size,
                "sizeBytes": info.size,
                "sha256": info.sha256,
            }
            gz = owner.transfers.get(kind)
            if gz is not None and gz.sha256 and gz.size < info.size:
                # optional, ignored by older apps (they download "url"): the gzip copy of the same file, which a newer app
                # downloads instead and unpacks; url/bytes/sha256 above always describe the installed (plain) file
                item["transfer"] = {"encoding": "gzip", "url": f"{FILES_URL}/{owner.id}/{gz.file}", "bytes": gz.size, "sha256": gz.sha256}
            entry_files.append(item)
        arriving = [kind for kind in KINDS if (region.id, kind) in building and kind not in region.files]
        manifest = region.manifest
        names = manifest["name"]
        version = region_version(region)
        entry: dict[str, Any] = {
            "id": region.id,
            "name": names["en"] or names["de"],
            "names": names,
            "bounds": manifest["bounds"],
            "version": version,
            "updatedAt": _iso(region.updated),
            "source": manifest["source"],
            "license": manifest["license"],
            "routingGroup": group,
            "state": "hashing" if hashing else ("updating" if arriving else "ready"),
            "files": [] if hashing or arriving else entry_files,
            # desired state set on the Regions page; a region that is still hashing or being built is not offered yet
            "install": bool(region.want.get("wanted")) and not hashing and not arriving,
            "remove": bool(region.want.get("removeRequested")),
            # the time of the current request ("" when there is none): a new value is a new request, so the app can let
            # a local delete/cancel win over an old flag until the revision changes
            "installRevision": region.want.get("wantedAt", "") if region.want.get("wanted") else "",
            # 1, 2, 3 ... among the regions that are wanted, in the order of the requests (0: not wanted). The app installs
            # one region at a time and takes the next one with the smallest installOrder
            "installOrder": order.get(region.id, 0),
            "removeRevision": region.want.get("removeAt", "") if region.want.get("removeRequested") else "",
            # "Cancel install" pressed: stop installing this region (files that are complete stay, nothing is deleted);
            # cancelRevision is the time of the cancel, a later installRevision is a new request
            "cancel": bool(region.want.get("cancelled")),
            "cancelRevision": region.want.get("cancelAt", "") if region.want.get("cancelled") else "",
        }
        if hashing:
            entry["hashing"] = hashing
        elif arriving:
            entry["updating"] = [{"kind": kind} for kind in arriving]
        out.append(entry)
    return {"version": 2, "generatedAt": _iso(now), "regions": out}


# ------------------------------------------------------------------ hash worker


class HashWorker:
    """One daemon thread that hashes region files one after the other and stores the result in ``files.json``."""

    def __init__(self, root: Path, on_done: Callable[[], None]) -> None:
        self._root = root
        self._on_done = on_done
        self._queue: queue.Queue[tuple[str, str] | None] = queue.Queue()
        self._pending: set[tuple[str, str]] = set()
        self._progress: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()
        self._file_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stopped = False

    def submit(self, region_id: str, file: str) -> None:
        key = (region_id, file)
        with self._lock:
            if key in self._pending or self._stopped:
                return
            self._pending.add(key)
            self._progress[key] = 0.0
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="regions-hash", daemon=True)
                self._thread.start()
        self._queue.put(key)

    def progress(self, region_id: str, file: str) -> float | None:
        with self._lock:
            return self._progress.get((region_id, file))

    def busy(self) -> bool:
        with self._lock:
            return bool(self._pending)

    def stop(self) -> None:
        with self._lock:
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
            try:
                self._hash_one(*key)
            except Exception:  # noqa: BLE001 - one unreadable file must not stop the worker
                log.warning("Regions: hashing %s/%s failed", key[0], key[1], exc_info=True)
            finally:
                with self._lock:
                    self._pending.discard(key)
                    self._progress.pop(key, None)
            try:
                self._on_done()
            except Exception:  # noqa: BLE001
                log.debug("Regions: after-hash callback failed", exc_info=True)

    def _hash_one(self, region_id: str, file: str) -> None:
        if file not in ALLOWED_FILES or not valid_region_id(region_id):
            return
        directory = self._root / region_id
        path = directory / file
        for _attempt in range(3):
            try:
                fd = os.open(path, _flags())
            except OSError:
                return
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode):
                    return
                known = read_hash_cache(directory).get(file)
                if known is not None and known["size"] == st.st_size and known["mtime_ns"] == st.st_mtime_ns:
                    return  # a rescan that raced with the previous run asked again: the hash of this very file is stored
                digest = hashlib.sha256()
                done = 0
                with os.fdopen(fd, "rb", buffering=0) as handle:
                    fd = -1
                    while True:
                        chunk = handle.read(HASH_CHUNK)
                        if not chunk:
                            break
                        digest.update(chunk)
                        done += len(chunk)
                        with self._lock:
                            self._progress[(region_id, file)] = min(0.999, done / max(1, st.st_size))
            finally:
                if fd >= 0:
                    os.close(fd)
            again = _stat_regular(path)
            if again is None or again.st_size != st.st_size or again.st_mtime_ns != st.st_mtime_ns or done != st.st_size:
                continue  # changed while hashing: once more
            self._store(directory, file, st.st_size, st.st_mtime_ns, digest.hexdigest())
            log.info("Regions: hashed %s/%s (%s)", region_id, file, human_bytes(st.st_size))
            return
        log.warning("Regions: %s/%s keeps changing, not hashed", region_id, file)

    def record(self, directory: Path, file: str, size: int, mtime_ns: int, sha: str) -> None:
        """Store a SHA-256 that is known already (blocking), e.g. computed while an import streamed the file."""
        self._store(directory, file, size, mtime_ns, sha)

    def _store(self, directory: Path, file: str, size: int, mtime_ns: int, sha: str) -> None:
        with self._file_lock:
            cache = read_hash_cache(directory)
            cache[file] = {"size": size, "mtime_ns": mtime_ns, "sha256": sha}
            if not directory.is_dir():
                return
            write_json_atomic(directory / HASH_CACHE, {"version": 1, "files": cache})


# ------------------------------------------------------------------ transfers (upload / import)


@dataclass
class ImportTask:
    id: str
    region: str
    kind: str
    url: str  # without query and credentials: safe to show and log
    state: str = "queued"  # queued | running | done | failed | cancelled (imports run one at a time, in the order requested)
    downloaded: int = 0
    total: int | None = None
    error: str = ""
    resumed_from: int = 0
    started: float = field(default_factory=time.time)
    finished: float | None = None
    expected_sha256: str = ""
    sha256: str = ""  # of the finished file, known when this run streamed all of it
    published: tuple[int, int] | None = None  # (size, mtime_ns) of the file as it was renamed into place
    user_cancelled: bool = False
    created: float = field(default_factory=time.monotonic)
    keep: bool = False  # stays in the queue file (stopped or lost connection): picked up again at the next start
    address: str = field(default="", repr=False)  # the full address (may hold a token): only in memory and in the queue file
    job: asyncio.Task[None] | None = field(default=None, repr=False, compare=False)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id, "region": self.region, "kind": self.kind, "url": self.url, "state": self.state, "queuePosition": 0,
            "downloaded": self.downloaded, "total": self.total, "error": self.error, "resumedFrom": self.resumed_from,
            "started": _iso(self.started), "finished": _iso(self.finished) if self.finished else None,
        }


DownloadStream = Stream  # a file download running on the sync API (see :mod:`car_media_hub.streams`)


# ------------------------------------------------------------------ manager


class RegionManager:
    """The regions folder as the add-on sees it. All public coroutines run on the event loop."""

    def __init__(
        self, root: Path, max_file_bytes: int = 8192 * 1024 * 1024, *, min_free: int = FREE_RESERVE,
        clock: Callable[[], float] = time.time, import_policy: Any = None, import_timeout: float = IMPORT_MAX_SECONDS,
        import_retry_delays: tuple[float, ...] = IMPORT_RETRY_DELAYS, transfer_gzip: bool = False,
    ) -> None:
        self.root = Path(root)
        self.transfer_gzip = transfer_gzip  # build and offer gzip transfer copies of routing/places (main.py: option)
        self.import_retry_delays = import_retry_delays
        self._stopping = False
        self.max_file_bytes = max_file_bytes
        self.min_free = min_free
        self.import_timeout = import_timeout
        self._seen: dict[tuple[str, str], tuple[int, int]] = {}  # (region, file) -> (size, mtime_ns) of the previous scan
        self._trusted: dict[tuple[str, str], tuple[int, int]] = {}  # files this add-on finished writing itself
        self._clock = clock
        self._import_policy = import_policy
        self._hasher = HashWorker(self.root, self._hash_done)
        from .regions_gzip import GzipWorker

        self._gzip = GzipWorker(self.root, self._hash_done, enabled=transfer_gzip, min_free=min_free,
                                is_busy=lambda region, kind: (region, kind) in self._busy)
        from .regions_devices import DeviceStore

        self.devices = DeviceStore(self.root / DEVICES_DIR, clock)
        self._want_lock = asyncio.Lock()
        self._snapshot: list[Region] | None = None
        self._snapshot_at = 0.0
        self._dirty = True
        self._scan_lock = asyncio.Lock()
        self._catalogue_cache: tuple[int, bytes] | None = None
        self._generation = 0
        self._busy: set[tuple[str, str]] = set()  # (region, kind) with an upload or import running or waiting
        self._active: set[tuple[str, str]] = set()  # (region, kind) written right now (what the catalogue calls "updating")
        self._import_slot = asyncio.Lock()  # one import at a time; the lock is fair: first come, first served
        self._queue_lock = asyncio.Lock()
        self._tasks: dict[str, ImportTask] = {}
        self._loop_task: asyncio.Task[None] | None = None
        self._ready_error = ""
        self.streams = StreamBudget()  # downloads on the sync API: per client and in total (shared with music and probe)
        self._refresh_task: asyncio.Task[None] | None = None  # a rescan started by a file request, running in the background

    # -- lifecycle
    async def start(self) -> None:
        try:
            await asyncio.to_thread(self._ensure_root)
        except OSError as exc:
            self._ready_error = f"cannot create {self.root}: {exc.strerror or exc}"
            log.warning("Regions: %s", self._ready_error)
        await self.rescan()
        self._loop_task = asyncio.create_task(self._rescan_loop(), name="regions-rescan")
        if not self._ready_error:
            await self.resume_imports()

    async def resume_imports(self) -> int:
        """Start again the imports that an add-on restart interrupted (their address is in ``.import-<kind>.json``).

        An import the owner cancelled, or one that failed for another reason than the connection, is not started again.
        Returns how many were started.
        """
        from . import regions_import

        try:
            pending = await asyncio.to_thread(regions_import.pending_imports, self.root)
            queued = await asyncio.to_thread(_read_import_queue, self.root)
        except OSError:
            return 0
        known = {(r, k) for r, k, _a, _s in queued}
        pending = queued + [p for p in pending if (p[0], p[1]) not in known]  # the queue file keeps the order of the requests
        started = 0
        for region_id, kind, address, sha in pending:
            try:
                self.start_import(region_id, kind, address, sha)
                started += 1
                log.info("Regions: resuming the import of %s/%s after the restart", region_id, FILE_NAMES[kind])
            except RegionError as exc:
                log.warning("Regions: the import of %s/%s is not resumed: %s", region_id, FILE_NAMES[kind], exc)
                if exc.status not in (409, 429):  # busy or too many at once: try again at the next start
                    directory = self.root / region_id
                    await asyncio.to_thread(regions_import.set_auto_resume, directory, kind, False)
        return started

    def _ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._cleanup_stale_parts(gzip_all=True)  # nothing is compressed before the start: every .gzip-* temp is left over

    def _cleanup_stale_parts(self, busy: frozenset[tuple[str, str]] = frozenset(), gzip_all: bool = False) -> None:
        """Delete unfinished ``.upload-*`` and ``.import-*`` files older than 24 h (blocking); running transfers stay.

        Unfinished gzip temps (``.gzip-<kind>-<hex>.part``) go after 24 h too, and all of them with ``gzip_all`` (start).
        """
        now = self._clock()
        try:
            dirs = [Path(e.path) for e in os.scandir(self.root) if valid_region_id(e.name) and e.is_dir(follow_symlinks=False)]
        except OSError:
            return
        for directory in dirs:
            try:
                entries = list(os.scandir(directory))
            except OSError:
                continue
            for entry in entries:
                gzip_temp = GZIP_PART_RE.fullmatch(entry.name) is not None
                match = PART_RE.fullmatch(entry.name) or IMPORT_META_RE.fullmatch(entry.name)
                if match is None and not gzip_temp:
                    continue
                if match is not None and (directory.name, match.group(match.lastindex)) in busy:
                    continue
                kind = match.group(match.lastindex) if match is not None else ""
                try:
                    st = entry.stat(follow_symlinks=False)
                    if not (gzip_temp and gzip_all) and now - st.st_mtime <= STALE_PART_SECONDS:
                        continue
                    if entry.name.endswith(".json") and os.path.lexists(directory / f".import-{kind}.part"):
                        continue  # the meta file goes together with its part
                    os.unlink(entry.path)
                    log.info("Regions: removed the stale partial file %s/%s", directory.name, entry.name)
                except OSError:
                    continue

    async def stop(self) -> None:
        self._stopping = True  # imports cancelled from here on are picked up again at the next start
        if self._loop_task is not None:
            self._loop_task.cancel()
            await asyncio.gather(self._loop_task, return_exceptions=True)
            self._loop_task = None
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            await asyncio.gather(self._refresh_task, return_exceptions=True)
            self._refresh_task = None
        for task in list(self._tasks.values()):
            if task.job is not None and not task.job.done():
                task.job.cancel()
        jobs = [t.job for t in self._tasks.values() if t.job is not None]
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)
        await asyncio.to_thread(self._hasher.stop)
        await asyncio.to_thread(self._gzip.stop)

    async def _rescan_loop(self) -> None:
        while True:
            await asyncio.sleep(RESCAN_SECONDS)
            try:
                await self.rescan()
            except Exception:  # noqa: BLE001
                log.warning("Regions: automatic rescan failed", exc_info=True)

    def _hash_done(self) -> None:
        self._dirty = True

    # -- downloads on the sync API
    @property
    def active_downloads(self) -> int:
        return self.streams.active

    def begin_download(self, key: tuple[str, str, str], transport: Any) -> "DownloadStream | None":
        """Take a download slot (None: the client or the add-on is at its stream limit with downloads that make progress).

        When a limit is reached, the stalled download of the same client and file (else the one stalled longest) is
        dropped, so a resumed request is not refused with 503 while the old connection of the sleeping car is still
        open. A download that still moves is never dropped. ``key[0]`` is the client address.
        """
        return self.streams.begin(key[0], key, transport)

    def end_download(self, stream: "DownloadStream") -> None:
        self.streams.end(stream)

    # -- snapshot
    def invalidate(self) -> None:
        self._dirty = True

    async def rescan(self) -> list[Region]:
        async with self._scan_lock:
            now = self._clock()
            # cleared before the folder is read: a change (a finished hash, a rename) that happens during the scan keeps
            # the snapshot dirty instead of being forgotten, so a catalogue never stays on "hashing" for a whole interval
            self._dirty = False
            await asyncio.to_thread(self._cleanup_stale_parts, frozenset(self._busy))
            try:
                regions, to_hash = await asyncio.to_thread(scan_region_dir, self.root, now, self.transfer_gzip)
                to_hash = self._adjust_settling(regions)
            except OSError as exc:
                self._ready_error = f"cannot read {self.root}: {exc.strerror or exc}"
                log.warning("Regions: %s", self._ready_error)
                regions, to_hash = [], []
            else:
                self._ready_error = ""
            for region_id, file in to_hash:
                self._hasher.submit(region_id, file)
            await self._transfer_upkeep(regions)
            self._snapshot = regions
            self._snapshot_at = now
            self._generation += 1
            return regions

    async def _transfer_upkeep(self, regions: list[Region]) -> None:
        """Gzip transfer copies after a scan: delete the stale ones (all of them while the option is off) and ask the
        gzip worker for the missing ones of hashed, settled routing/places files that no upload or import writes."""
        from . import regions_gzip

        for region in regions:
            if region.transfer_present and (not self.transfer_gzip or region.transfer_stale):
                try:
                    await asyncio.to_thread(self._gzip.sweep, region.id, not self.transfer_gzip)
                except OSError:
                    log.warning("Regions: cannot clean the compressed copies of %s", region.id, exc_info=True)
                region.transfer_stale = []
        if not self.transfer_gzip:
            return
        # a routing file of a region whose group takes the routing file of another region is never offered: not compressed
        providers: dict[str, str] = {}
        for region in regions:
            group = region.manifest.get("routingGroup")
            if group and "routing" in region.files and group not in providers:
                providers[group] = region.id
        for region in regions:
            group = region.manifest.get("routingGroup")
            for kind in GZ_KINDS:
                info = region.files.get(kind)
                if (info is None or info.sha256 is None or info.settling or info.size < regions_gzip.MIN_BYTES
                        or kind in region.transfers or kind in region.transfer_skipped or (region.id, kind) in self._busy):
                    continue
                if kind == "routing" and group and providers.get(group) != region.id:
                    continue
                if "map" not in region.files:
                    continue  # never offered by the catalogue: not worth the CPU and the disk
                if self._gzip.backing_off(region.id, kind):
                    continue  # the last build of this file failed after real work: wait before the next one
                self._gzip.submit(region.id, kind)

    def _adjust_settling(self, regions: list[Region]) -> list[tuple[str, str]]:
        """Apply the settle rules that need memory: a file whose size or mtime differs from the previous scan is still
        being written (settling); a file this add-on finished writing itself is not. Returns the files to hash."""
        seen: dict[tuple[str, str], tuple[int, int]] = {}
        for region in regions:
            for kind, info in list(region.files.items()):
                key, current = (region.id, info.file), (info.size, info.mtime_ns)
                seen[key] = current
                if info.sha256 is not None:
                    continue
                settling = info.settling
                if self._trusted.get(key) == current:
                    settling = False
                elif key in self._seen and self._seen[key] != current:
                    settling = True
                if settling != info.settling:
                    region.files[kind] = dataclasses.replace(info, settling=settling)
        self._seen = seen
        self._trusted = {k: v for k, v in self._trusted.items() if seen.get(k) == v}
        return [(r.id, f.file) for r in regions for f in r.files.values() if f.sha256 is None and not f.settling]

    def _trust(self, region_id: str, file: str) -> None:
        """Remember that the file now on disk was completely written by this add-on (blocking stat)."""
        try:
            st = os.stat(self.root / region_id / file)
        except OSError:
            return
        self._trusted[(region_id, file)] = (st.st_size, st.st_mtime_ns)

    async def servable(self, region_id: str, name: str, st: os.stat_result) -> FileInfo | None:
        """The snapshot entry when the open file (``st``) is the hashed file: same size and mtime as the one whose
        SHA-256 is known, else None.

        The cached snapshot answers first, so a file request never waits for a rescan (a rescan that is due runs in the
        background). Only a mismatch drops the snapshot and looks once more (the file may just have changed or been hashed).
        """
        info = self._servable_cached(region_id, name, st)
        if info is not None:
            self._refresh_soon()
            return info
        for attempt in (0, 1):
            for region in await self.snapshot():
                if region.id != region_id:
                    continue
                info = _served_info(region, name)
                if info is not None and info.sha256 and info.size == st.st_size and info.mtime_ns == st.st_mtime_ns:
                    return info
            if attempt == 0:
                self.invalidate()
        return None

    def _servable_cached(self, region_id: str, name: str, st: os.stat_result) -> FileInfo | None:
        for region in self._snapshot or []:
            if region.id != region_id:
                continue
            info = _served_info(region, name)
            if info is not None and info.sha256 and info.size == st.st_size and info.mtime_ns == st.st_mtime_ns:
                return info
        return None

    def _snapshot_due(self, now: float) -> bool:
        snap = self._snapshot
        settling = snap is not None and any(f.settling for r in snap for f in r.files.values())
        if snap is None or self._dirty or settling or now - self._snapshot_at > RESCAN_SECONDS:
            return not settling or now - self._snapshot_at >= 1.0 or self._dirty
        return False

    def _refresh_soon(self) -> None:
        """Start a rescan in the background when one is due (at most one at a time); file requests do not wait for it."""
        if self._stopping or (self._refresh_task is not None and not self._refresh_task.done()):
            return
        if not self._snapshot_due(self._clock()):
            return
        self._refresh_task = asyncio.create_task(self._refresh_quietly(), name="regions-refresh")

    async def _refresh_quietly(self) -> None:
        try:
            await self.rescan()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.warning("Regions: background rescan failed", exc_info=True)

    async def snapshot(self) -> list[Region]:
        """The current snapshot, rescanned when something changed, a file is still settling or it is old."""
        snap = self._snapshot
        if self._snapshot_due(self._clock()):
            snap = await self.rescan()
        return snap or []

    @property
    def problem(self) -> str:
        return self._ready_error

    async def catalogue_body(self) -> bytes:
        regions = await self.snapshot()
        hashing = self._hasher.busy() or any(f.sha256 is None for r in regions for f in r.files.values())
        cached = self._catalogue_cache
        if cached is not None and cached[0] == self._generation and not hashing and not self._active:
            return cached[1]
        building = frozenset(self._active)
        doc = build_catalogue(regions, self._hasher.progress, self._clock(), building)
        body = json.dumps(doc, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        if not hashing and not building:
            self._catalogue_cache = (self._generation, body)
        return body

    # -- admin views
    async def overview(self) -> dict[str, Any]:
        regions = await self.snapshot()
        order = install_order(regions)
        items = []
        for region in regions:
            files = []
            for kind in KINDS:
                info = region.files.get(kind)
                if info is None:
                    files.append({"kind": kind, "file": FILE_NAMES[kind], "present": False, "busy": (region.id, kind) in self._busy})
                    continue
                if info.sha256:
                    state = "ready"
                elif info.settling:
                    state = "settling"
                else:
                    state = "hashing"
                files.append({
                    "kind": kind, "file": info.file, "present": True, "size": info.size, "sha256": info.sha256,
                    "state": state, "progress": self._hasher.progress(region.id, info.file) if state == "hashing" else None,
                    "busy": (region.id, kind) in self._busy, "transfer": self._transfer_view(region, kind),
                })
            items.append({
                "id": region.id, "manifest": region.manifest, "manifestProblem": region.manifest_problem,
                "problems": region.problems, "files": files,
                "totalBytes": sum(f.size for f in region.files.values()), "updatedAt": _iso(region.updated) if region.updated else "",
                "wanted": region.want.get("wanted", False), "wantedAt": region.want.get("wantedAt", ""),
                "installOrder": order.get(region.id, 0),
                "wantedBy": region.want.get("wantedBy", ""), "removeRequested": region.want.get("removeRequested", False),
                "removeAt": region.want.get("removeAt", ""),
                "cancelled": region.want.get("cancelled", False), "cancelAt": region.want.get("cancelAt", ""),
                "ready": "map" in region.files and all(f.sha256 for f in region.files.values()),
                "hasMap": "map" in region.files,
                "parts": [
                    {"kind": p["kind"], "origin": p["origin"], "size": p["size"], "ageSeconds": max(0, int(self._clock() - p["mtime"])),
                     "running": (region.id, p["kind"]) in self._busy}
                    for p in region.parts
                ],
            })
        try:
            devices = await self.devices_public()
        except OSError:
            log.warning("Regions: cannot read the device reports", exc_info=True)
            devices = []
        self._apply_done(items, devices)
        return {
            "root": str(self.root), "problem": self._ready_error, "maxFileBytes": self.max_file_bytes,
            "regions": items, "tasks": self._public_tasks(),
            "devices": devices,
        }

    def _transfer_view(self, region: Region, kind: str) -> dict[str, Any] | None:
        """What the Regions page shows of the gzip copy of a file: ready (size), building (progress) or skipped."""
        if not self.transfer_gzip or kind not in GZ_KINDS:
            return None
        gz = region.transfers.get(kind)
        if gz is not None:
            return {"state": "ready", "size": gz.size}
        done = self._gzip.progress(region.id, kind)
        if done is not None:
            return {"state": "building", "progress": round(done, 3)}
        if kind in region.transfer_skipped:
            return {"state": "skipped"}
        return None

    @staticmethod
    def _apply_done(items: list[dict[str, Any]], devices: list[dict[str, Any]]) -> None:
        """What the page shows of an install request: ``done`` when every known device has the region installed (current
        version, or version unknown), ``updatePending`` when a device has an older version, else the request is pending.
        ``installOrder`` counts only the pending regions (dense, request order); ``requestOrder`` is the catalogue's value
        (all wanted regions, what the app queues by). The request itself stays in want.json."""
        for item in items:
            entries = [next((e for e in d["regions"] if e["id"] == item["id"]), None) for d in devices]
            current = [e is not None and e["current"] for e in entries]
            item["devicesTotal"] = len(devices)
            item["devicesInstalled"] = sum(1 for c in current if c)
            item["done"] = bool(item["wanted"] and devices and all(current))
            item["updatePending"] = bool(item["wanted"] and any(e is not None and e["updatePending"] for e in entries))
            item["requestOrder"] = item["installOrder"]
        pending = sorted((i for i in items if i["wanted"] and not i["done"]), key=lambda i: i["requestOrder"])
        ranks = {i["id"]: n for n, i in enumerate(pending, 1)}
        for item in items:
            item["installOrder"] = ranks.get(item["id"], 0)

    # -- admin operations
    def _region_dir(self, region_id: str) -> Path:
        if not valid_region_id(region_id):
            raise RegionError("invalid region id (a-z, 0-9, _ and -, 2 to 41 characters, starting with a letter or digit)")
        return self.root / region_id

    def _existing_dir(self, region_id: str) -> Path:
        directory = self._region_dir(region_id)
        try:
            st = os.lstat(directory)
        except OSError:
            raise RegionError("unknown region", 404) from None
        if not stat.S_ISDIR(st.st_mode):
            raise RegionError("unknown region", 404)
        return directory

    async def _existing_dir_async(self, region_id: str) -> Path:
        """:meth:`_existing_dir` without blocking the event loop on a slow disk."""
        self._region_dir(region_id)  # the id check needs no I/O
        return await asyncio.to_thread(self._existing_dir, region_id)

    async def create_region(self, region_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        directory = self._region_dir(region_id)
        manifest = normalise_manifest({**fields, "id": region_id}, region_id, now=self._clock())

        def work() -> None:
            self.root.mkdir(parents=True, exist_ok=True)
            try:
                os.mkdir(directory, 0o755)
            except FileExistsError:
                raise RegionError("a region with this id exists already", 409) from None
            write_json_atomic(directory / MANIFEST, manifest)

        await asyncio.to_thread(work)
        log.info("Regions: created %s", region_id)
        self.invalidate()
        return manifest

    async def update_manifest(self, region_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        directory = await self._existing_dir_async(region_id)
        manifest = normalise_manifest({**fields, "id": region_id}, region_id, now=self._clock())
        await asyncio.to_thread(write_json_atomic, directory / MANIFEST, manifest)
        log.info("Regions: manifest of %s changed", region_id)
        self.invalidate()
        return manifest

    async def _write_want(self, directory: Path, want: dict[str, Any]) -> None:
        await asyncio.to_thread(write_json_atomic, directory / WANT_FILE, {
            "version": 1, "wanted": want["wanted"], "wantedAt": want["wantedAt"], "wantedBy": want["wantedBy"],
            "wantedSeq": want.get("wantedSeq", 0),
            "removeRequested": want["removeRequested"], "removeAt": want["removeAt"], "removeAtTs": want["removeAtTs"],
            "cancelled": want.get("cancelled", False), "cancelAt": want.get("cancelAt", ""), "cancelAtTs": want.get("cancelAtTs", 0.0),
        })

    async def set_wanted(self, region_id: str, wanted: Any, by: Any = "") -> dict[str, Any]:
        """Mark a region as wanted (or not) on the car app. Either choice also cancels a pending removal."""
        if not isinstance(wanted, bool):
            raise RegionError("wanted must be true or false")
        note = _want_text(by, 80)
        async with self._want_lock:
            directory = await self._existing_dir_async(region_id)
            now = self._clock()
            before = await asyncio.to_thread(read_want, directory, now)
            raw = await asyncio.to_thread(_read_raw_want, directory)
            stamp, _ts = _stamp(now, raw.get("wantedAt"), raw.get("removeAt"), raw.get("cancelAt"))
            if wanted:
                # a region that is wanted already keeps its place in the queue; a new request goes to the end
                seq = before["wantedSeq"] or await asyncio.to_thread(_max_want_seq, self.root) + 1
                want = {"wanted": True, "wantedAt": stamp, "wantedBy": note, "wantedSeq": seq, "removeRequested": False,
                        "removeAt": "", "removeAtTs": 0.0, "cancelled": False, "cancelAt": "", "cancelAtTs": 0.0}
            elif before["wanted"]:
                # "Cancel install": a state the catalogue shows (cancel + cancelRevision), not just a missing flag
                want = {"wanted": False, "wantedAt": before["wantedAt"], "wantedBy": note, "removeRequested": False,
                        "removeAt": "", "removeAtTs": 0.0, "cancelled": True, "cancelAt": stamp, "cancelAtTs": _ts}
            else:  # nothing was requested: nothing to cancel, a pending removal is withdrawn
                want = {**before, "removeRequested": False, "removeAt": "", "removeAtTs": 0.0}
            await self._write_want(directory, want)
        log.info("Regions: %s is %s on the car app", region_id, "wanted" if wanted else "no longer wanted")
        self.invalidate()
        return want

    async def set_wanted_many(self, region_ids: Any, by: Any = "") -> list[str]:
        """Ask for the install of several regions at once, in the given order (a region that is wanted already keeps its
        place, one with a pending removal request is left alone). All of them are accepted before the first one is
        looked at by the car app. Returns the ids that were newly requested."""
        if not isinstance(region_ids, list) or len(region_ids) > 200 or not all(isinstance(i, str) for i in region_ids):
            raise RegionError("ids must be a list of region ids")
        note = _want_text(by, 80)
        done: list[str] = []
        async with self._want_lock:
            directories = [await self._existing_dir_async(i) for i in dict.fromkeys(region_ids)]
            now = self._clock()
            seq = await asyncio.to_thread(_max_want_seq, self.root)
            for directory in directories:
                before = await asyncio.to_thread(read_want, directory, now)
                if before["wanted"] or before["removeRequested"]:
                    continue
                raw = await asyncio.to_thread(_read_raw_want, directory)
                stamp, _ts = _stamp(now, raw.get("wantedAt"), raw.get("removeAt"), raw.get("cancelAt"))
                seq += 1
                await self._write_want(directory, {
                    "wanted": True, "wantedAt": stamp, "wantedBy": note, "wantedSeq": seq, "removeRequested": False,
                    "removeAt": "", "removeAtTs": 0.0, "cancelled": False, "cancelAt": "", "cancelAtTs": 0.0})
                done.append(directory.name)
        log.info("Regions: install requested for %d region(s) at once", len(done))
        self.invalidate()
        return done

    async def install_all(self, by: Any = "") -> list[str]:
        """Request the install of every region that has a map and is not requested yet (by id)."""
        regions = await self.snapshot()
        return await self.set_wanted_many([r.id for r in regions if "map" in r.files], by)

    async def request_remove(self, region_id: str, by: Any = "") -> dict[str, Any]:
        """Ask the car app to remove a region (not wanted + removeRequested, until it reports it gone or 30 days)."""
        note = _want_text(by, 80)
        async with self._want_lock:
            directory = await self._existing_dir_async(region_id)
            now = self._clock()
            want = {"wanted": False, "wantedAt": _iso(now), "wantedBy": note,
                    "removeRequested": True, "removeAt": _iso(now), "removeAtTs": now,
                    "cancelled": False, "cancelAt": "", "cancelAtTs": 0.0}
            await self._write_want(directory, want)
        log.info("Regions: removal of %s from the car app requested", region_id)
        self.invalidate()
        return want

    async def devices_public(self) -> list[dict[str, Any]]:
        """The device list (see :meth:`DeviceStore.public_list`) with the cancelled installs of the regions applied."""
        regions = await self.snapshot()
        cancelled = {r.id: r.want["cancelAtTs"] for r in regions if r.want.get("cancelled")}
        providers: dict[str, Region] = {}
        for r in regions:
            group = r.manifest.get("routingGroup")
            if group and "routing" in r.files and group not in providers:
                providers[group] = r
        shared = {r.id: (g, providers[g].files["routing"].size) for r in regions
                  if (g := r.manifest.get("routingGroup")) and g in providers}
        versions = {r.id: region_version(r) for r in regions}
        wanted = frozenset(r.id for r in regions if r.want.get("wanted"))
        return await self.devices.public_list(cancelled, install_order(regions), shared, versions, wanted)

    async def device_report(self, data: Any) -> None:
        """Store the report of a car app and settle removals that every known device confirmed."""
        await self.devices.report(data)
        await self._settle_removals()

    async def forget_device(self, device_id: str) -> None:
        """Remove one car app device from the reports (it shows up again the next time it reports)."""
        from .regions_devices import ReportError

        try:
            found = await self.devices.forget(device_id)
        except ReportError as exc:
            raise RegionError(str(exc), exc.status) from None
        if not found:
            raise RegionError("unknown device", 404)
        log.info("Regions: forgot device %s", device_id[:8])
        await self._settle_removals()

    async def forget_all_devices(self) -> int:
        count = await self.devices.forget_all()
        log.info("Regions: forgot %d device(s)", count)
        await self._settle_removals()
        return count

    async def _settle_removals(self) -> None:
        regions = await self.snapshot()
        pending = [r for r in regions if r.want.get("removeRequested")]
        if not pending:
            return
        devices = await self.devices.load()
        if not devices:
            return
        for region in pending:
            ts = region.want.get("removeAtTs", 0.0)
            gone = True
            for d in devices:
                entry = d["regions"].get(region.id) or {}
                if entry.get("state") not in ("absent", "removed") or entry.get("updatedTs", 0) < ts:
                    gone = False
                    break
            if not gone:
                continue
            async with self._want_lock:
                try:
                    directory = await self._existing_dir_async(region.id)
                except RegionError:
                    continue
                current = await asyncio.to_thread(read_want, directory, self._clock())
                if current["removeRequested"] and current["removeAtTs"] == ts:
                    current.update(removeRequested=False, removeAt="", removeAtTs=0.0)
                    await self._write_want(directory, current)
                    log.info("Regions: %s is gone from the car app, removal request cleared", region.id)
                    self.invalidate()

    async def delete_region(self, region_id: str) -> None:
        directory = await self._existing_dir_async(region_id)
        if any(r == region_id for r, _ in self._busy):
            raise RegionError("an upload or import of this region is running: cancel it first", 409)
        self._gzip.cancel(region_id)  # a running compression of its files would only read deleted data

        def work() -> None:
            # only regular files are expected inside; links are unlinked, never followed
            for entry in os.scandir(directory):
                if entry.is_dir(follow_symlinks=False):
                    shutil.rmtree(entry.path)
                else:
                    os.unlink(entry.path)
            os.rmdir(directory)

        for attempt in range(5):
            try:
                await asyncio.to_thread(work)
                break
            except OSError:  # a Samba client or the hash worker wrote a file meanwhile: look again
                if attempt == 4:
                    raise RegionError("the folder is in use (a file is being written): try again in a moment", 409) from None
                await asyncio.sleep(0.1)
        log.info("Regions: deleted %s", region_id)
        self.invalidate()

    async def delete_file(self, region_id: str, kind: str) -> None:
        directory = await self._existing_dir_async(region_id)
        if kind not in FILE_NAMES:
            raise RegionError("unknown file kind", 404)
        if (region_id, kind) in self._busy:
            raise RegionError("an upload or import of this file is running: cancel it first", 409)
        if kind in GZ_KINDS:
            self._gzip.cancel(region_id, kind)
        try:
            await asyncio.to_thread(os.unlink, directory / FILE_NAMES[kind])
        except FileNotFoundError:
            raise RegionError("file not found", 404) from None
        finally:
            if kind in GZ_KINDS:  # its gzip transfer copy goes with it (also when only that one was left)
                try:
                    await asyncio.to_thread(self._gzip.remove, region_id, kind)
                except OSError:
                    log.warning("Regions: cannot delete the compressed copy of %s/%s", region_id, kind, exc_info=True)
        self.invalidate()

    async def discard_part(self, region_id: str, kind: str) -> int:
        """Delete the unfinished transfer files of one file kind (a partial import or an orphaned upload)."""
        directory = await self._existing_dir_async(region_id)
        if kind not in FILE_NAMES:
            raise RegionError("unknown file kind", 404)
        if (region_id, kind) in self._busy:
            raise RegionError("an upload or import of this file is running: cancel it first", 409)

        def work() -> int:
            removed = 0
            for entry in os.scandir(directory):
                match = PART_RE.fullmatch(entry.name) or IMPORT_META_RE.fullmatch(entry.name)
                if match is not None and match.group(match.lastindex) == kind:
                    try:
                        os.unlink(entry.path)
                        removed += 1
                    except OSError:
                        pass
            return removed

        removed = await asyncio.to_thread(work)
        if not removed:
            raise RegionError("there is no partial file", 404)
        log.info("Regions: discarded the partial %s file of %s", kind, region_id)
        self.invalidate()
        return removed

    def _claim(self, region_id: str, kind: str) -> None:
        if kind not in FILE_NAMES:
            raise RegionError("unknown file kind (map, routing or places)", 404)
        if (region_id, kind) in self._busy:
            raise RegionError("an upload or import of this file is already running", 409)
        self._busy.add((region_id, kind))

    def _release(self, region_id: str, kind: str) -> None:
        self._busy.discard((region_id, kind))

    async def save_upload(self, region_id: str, kind: str, chunks: Any, declared: int | None) -> int:
        """Store an uploaded file: ``chunks`` is an async iterator of bytes. Returns the size. Atomic: the file appears
        under its name only when it is complete and has the right header."""
        directory = await self._existing_dir_async(region_id)
        if declared is not None and declared > self.max_file_bytes:
            raise RegionError(f"the file is larger than the limit of {human_bytes(self.max_file_bytes)} (option region_max_file_mb)", 413)
        self._claim(region_id, kind)
        self._active.add((region_id, kind))
        tmp = directory / f".upload-{kind}-{secrets.token_hex(4)}.part"
        try:
            # also without a stated size (the ingress proxy streams without Content-Length): the reserve must stay free
            await self._check_free(directory, declared or 0)
            size = await self._write_stream(tmp, kind, chunks, directory, declared or 0)
            final = directory / FILE_NAMES[kind]
            await asyncio.to_thread(os.replace, tmp, final)
            await asyncio.to_thread(self._trust, region_id, FILE_NAMES[kind])
            log.info("Regions: stored %s/%s (%s)", region_id, FILE_NAMES[kind], human_bytes(size))
        except BaseException:
            await asyncio.shield(asyncio.to_thread(_unlink_quiet, tmp))
            raise
        finally:
            self._active.discard((region_id, kind))
            self._release(region_id, kind)
        self.invalidate()
        await self.rescan()  # starts the hash worker
        return size

    async def _check_free(self, directory: Path, need: int) -> None:
        try:
            await asyncio.to_thread(ensure_free, directory, need, self.min_free)
        except NoSpaceError as exc:
            raise RegionError(str(exc), 507) from None

    async def _write_stream(self, tmp: Path, kind: str, chunks: Any, directory: Path | None = None, expected: int = 0) -> int:
        fd = await asyncio.to_thread(
            os.open, tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0), 0o644)
        size = 0
        head = b""
        need = head_bytes_needed(kind)
        next_check = FREE_CHECK_BYTES
        try:
            with os.fdopen(fd, "wb") as handle:
                async for chunk in chunks:
                    if not chunk:
                        continue
                    if len(head) < need:
                        head += chunk[: need - len(head)]
                        if len(head) >= need:
                            validate_head(kind, head)
                    size += len(chunk)
                    if size > self.max_file_bytes:
                        raise RegionError(f"the file is larger than the limit of {human_bytes(self.max_file_bytes)}", 413)
                    await asyncio.to_thread(handle.write, chunk)
                    if directory is not None and size >= next_check:
                        next_check = size + FREE_CHECK_BYTES
                        await self._check_free(directory, max(0, expected - size))  # what is still to come must fit too
                if len(head) < need:
                    validate_head(kind, head)
                validate_head(kind, head, size)
                await asyncio.to_thread(handle.flush)
                await asyncio.to_thread(os.fsync, handle.fileno())
        except OSError as exc:
            if exc.errno in (28, 122):
                raise RegionError("the disk is full", 507) from None
            raise
        return size

    # -- import from URL
    def _public_tasks(self) -> list[dict[str, Any]]:
        waiting = {t.id: i for i, t in enumerate(sorted((t for t in self._tasks.values() if t.state == "queued"), key=lambda t: t.created), 1)}
        out = []
        for t in sorted(self._tasks.values(), key=lambda t: -t.created):
            doc = t.public()
            doc["queuePosition"] = waiting.get(t.id, 0)
            out.append(doc)
        return out

    def tasks(self) -> list[ImportTask]:
        return list(self._tasks.values())

    def start_import(self, region_id: str, kind: str, url: str, expected_sha256: str = "") -> ImportTask:
        from . import regions_import

        directory = self._existing_dir(region_id)
        expected = (expected_sha256 or "").strip().lower()
        if expected and not SHA256_RE.fullmatch(expected):
            raise RegionError("sha256 must be 64 hexadecimal digits")
        policy = self._import_policy or regions_import.ImportPolicy()
        safe_url = regions_import.check_url_syntax(url, policy)
        if sum(1 for t in self._tasks.values() if t.state in ("queued", "running")) >= regions_import.MAX_QUEUED_IMPORTS:
            raise RegionError("too many imports are queued, wait for one to finish", 429)
        self._claim(region_id, kind)
        task = ImportTask(secrets.token_hex(6), region_id, kind, regions_import.display_url(safe_url), expected_sha256=expected,
                          address=safe_url)
        self._tasks[task.id] = task
        self._trim_tasks()
        task.job = asyncio.create_task(self._run_import(task, directory, safe_url, policy), name=f"region-import-{task.id}")
        return task

    def _trim_tasks(self) -> None:
        finished = sorted((t for t in self._tasks.values() if t.state not in ("queued", "running")), key=lambda t: t.started)
        for old in finished[:-20]:
            self._tasks.pop(old.id, None)

    async def _import_with_retries(self, task: ImportTask, directory: Path, url: str, policy: Any) -> None:
        """One import; after a lost connection it resumes by itself (Range/If-Range from the part) a few times.

        The pauses grow; an attempt that moved the download forward starts the count again, so a flaky link finishes
        a big file, while a dead server ends the task after the last pause (the part stays for the next try).
        """
        from . import regions_import

        failures = 0
        best = 0
        while True:
            try:
                await regions_import.run_import(self, task, directory, url, policy)
                task.error = ""
                return
            except (aiohttp.ClientError, asyncio.TimeoutError, TransferInterrupted) as exc:
                failures = 0 if task.downloaded > best else failures + 1
                best = max(best, task.downloaded)
                delays = self.import_retry_delays
                if not delays or failures > len(delays):
                    raise
                delay = delays[max(0, failures - 1)]
                task.error = f"connection lost ({type(exc).__name__}); resuming in {delay:g} s"
                log.info("Regions: import of %s/%s lost its connection at %d bytes, resuming in %g s",
                         task.region, FILE_NAMES[task.kind], task.downloaded, delay)
                await asyncio.sleep(delay)

    async def _run_import(self, task: ImportTask, directory: Path, url: str, policy: Any) -> None:
        from . import regions_import

        discard = False  # a failure that leaves nothing worth resuming (a full disk): the partial file is deleted
        auto = False  # the part is picked up again at the next add-on start (a stop or a lost connection, not a verdict)
        timeout = asyncio.timeout(self.import_timeout if self.import_timeout > 0 else None)
        try:
            await self._persist_queue()
            async with self._import_slot:  # waits here while another import runs
                task.state, task.started = "running", self._clock()
                self._active.add((task.region, task.kind))
                async with timeout:
                    await self._import_with_retries(task, directory, url, policy)
            task.state = "done"
            log.info("Regions: imported %s/%s from %s", task.region, FILE_NAMES[task.kind], task.url)
        except asyncio.CancelledError:
            task.state = "cancelled"
            task.error = "cancelled (the partial download is kept and resumes with the same address)"
            auto = self._stopping and not task.user_cancelled
        except RegionError as exc:
            task.state, task.error = "failed", str(exc)
            discard = exc.status == 507
            auto = isinstance(exc, TransferInterrupted)
            log.warning("Regions: import of %s/%s failed: %s", task.region, FILE_NAMES[task.kind], exc)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            task.state = "failed"
            if timeout.expired():
                task.error = (f"the import took longer than the time limit of {self.import_timeout / 3600:g} h "
                              "(option region_import_max_hours); the partial download is kept: import again to resume")
            else:
                auto = True
                task.error = f"connection problem ({type(exc).__name__}); the partial download is kept: import again to resume"
            log.warning("Regions: import of %s/%s failed: %s", task.region, FILE_NAMES[task.kind], type(exc).__name__)
        except NoSpaceError as exc:
            task.state, task.error = "failed", f"{exc}; the partial download was deleted"
            discard = True
            log.warning("Regions: import of %s/%s failed: disk space", task.region, FILE_NAMES[task.kind])
        except Exception as exc:  # noqa: BLE001
            task.state, task.error = "failed", f"{type(exc).__name__}: {exc}"
            discard = is_no_space_error(exc)
            if discard:
                task.error = "the disk is full; the partial download was deleted"
            log.warning("Regions: import of %s/%s failed: %s", task.region, FILE_NAMES[task.kind], type(exc).__name__)
        finally:
            if discard:
                await asyncio.shield(asyncio.to_thread(regions_import.discard_import_files, directory, task.kind))
            elif task.state != "done":
                await asyncio.shield(asyncio.to_thread(regions_import.set_auto_resume, directory, task.kind, auto))
            task.finished = self._clock()
            task.keep = auto and task.state != "done"
            self._active.discard((task.region, task.kind))
            self._release(task.region, task.kind)
            await asyncio.shield(self._persist_queue())
            self.invalidate()
            if task.state == "done":
                try:
                    await asyncio.to_thread(self._trust, task.region, FILE_NAMES[task.kind])
                    if task.sha256 and task.published is not None:
                        # the SHA-256 was computed while the bytes streamed in: no second read of a multi-GB file
                        size, mtime_ns = task.published
                        await asyncio.to_thread(self._hasher.record, directory, FILE_NAMES[task.kind], size, mtime_ns, task.sha256)
                    await self.rescan()
                except Exception:  # noqa: BLE001
                    log.debug("Regions: rescan after import failed", exc_info=True)

    async def _persist_queue(self) -> None:
        """Write the imports that are running, waiting or were stopped on purpose (add-on stop, lost connection) to the
        queue file, in the order they were requested; an add-on restart picks them up again (:meth:`resume_imports`)."""
        async with self._queue_lock:
            items = [
                {"region": t.region, "kind": t.kind, "address": t.address, "sha256": t.expected_sha256}
                for t in sorted(self._tasks.values(), key=lambda t: t.created)
                if t.address and (t.state in ("queued", "running") or t.keep)
            ]
            path = self.root / IMPORT_QUEUE
            try:
                if items:
                    await asyncio.to_thread(write_json_atomic, path, {"version": 1, "imports": items})
                else:
                    await asyncio.to_thread(_unlink_quiet, path)
            except OSError:
                log.warning("Regions: cannot write the import queue", exc_info=True)

    def cancel_task(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if task is None:
            raise RegionError("unknown task", 404)
        if task.job is None or task.job.done():
            return False
        task.user_cancelled = True  # the owner stopped it: it is not picked up again at the next start
        task.job.cancel()
        return True


def _read_import_queue(root: Path) -> list[tuple[str, str, str, str]]:
    """The imports of the queue file (blocking): (region id, kind, address, expected sha256) in request order."""
    try:
        data = read_small_json(root / IMPORT_QUEUE)
    except (OSError, ValueError):
        return []
    out = []
    for item in data.get("imports", []) if isinstance(data, dict) else []:
        if isinstance(item, dict) and valid_region_id(item.get("region")) and item.get("kind") in FILE_NAMES and isinstance(item.get("address"), str):
            sha = item.get("sha256")
            out.append((item["region"], item["kind"], item["address"], sha if isinstance(sha, str) else ""))
    return out


def _served_info(region: Region, name: str) -> FileInfo | None:
    """The snapshot entry of a file name of ``/api/regions/files``: a plain file, or a valid gzip copy (``transfers``)."""
    if name in TRANSFER_FILES:
        return region.transfers.get(TRANSFER_FILES[name])
    return region.files.get(KIND_OF_FILE.get(name, ""))


def _unlink_quiet(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
