"""Safe archive extraction (zip, tar family; rar through ``bsdtar``, 7z through ``7z`` or ``bsdtar``).

Every extractor validates entry names before writing anything and verifies the
resulting tree afterwards, so a hostile archive cannot escape ``dest`` (zip-slip),
plant symlinks, or exhaust the disk with absurd entry counts or sizes.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import lzma
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import IO

try:  # module level on purpose: an import inside preexec_fn (child of a threaded process) can deadlock
    import resource
except ImportError:  # Windows
    resource = None  # type: ignore[assignment]

from car_media_hub.browser_proc import kill_user_processes
from car_media_hub.config import ARCHIVE_EXTS
from car_media_hub.names import is_hidden_entry

logger = logging.getLogger(__name__)

MAX_TOTAL_BYTES = 100 * 1024**3  # 100 GiB of uncompressed data
DISK_RESERVE_BYTES = 1024**3  # extraction never eats into the last 1 GiB of free space
MAX_ENTRIES = 100_000
_CHUNK = 1 << 20
_SEVENZIP_TIMEOUT = 6 * 3600
_BSDTAR_TIMEOUT = 6 * 3600
_WATCH_INTERVAL = 0.5  # seconds between two checks of a running external extractor
EXTRACT_USER = "extract"  # created in the Dockerfile (uid 1011): no shell, no home, owns nothing
EXTRACT_ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LC_ALL": "C.UTF-8", "HOME": "/nonexistent"}  # nothing from the add-on's environment
EXTRACT_AS_BYTES = 4 * 1024**3  # RLIMIT_AS of 7z / bsdtar
EXTRACT_NOFILE = 256
EXTRACT_NPROC = 64
BSDTAR_ENV = "CAR_MEDIA_HUB_BSDTAR"  # overrides the bsdtar executable (tests, unusual installs)

_DRIVE_RE = re.compile(r"^[A-Za-z]:")
# Unix mode string as printed by 7z, e.g. "-rw-r--r--" or "lrwxrwxrwx" (non-regular types only).
_SPECIAL_ATTR_RE = re.compile(r"(?:^|\s)[lcbps][rwxsStT-]{9}(?:\s|$)")
_TAR_SUFFIXES = (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".tar")
# One line of ``bsdtar --numeric-owner -tvf``: mode, link count, uid, gid, size, date, name. The
# owner fields are numbers, so every field before the name comes from bsdtar itself and the match
# is unique: a user or group name inside the archive cannot fake a size or a date.
# Only the date is relaxed: a bogus mtime (for example a RAR5 FILETIME far in the future or past) prints
# a year of any length, with or without a sign (musl strftime), as "-- -- ----", or as nothing at all
# (two spaces after the size). Everything before the date stays strict.
_BSDTAR_DATE = (
    r"(?:[A-Za-z]{3}\s+\d{1,2}\s+(?:[+-]?\d{1,6}|\d{1,2}:\d{2})"
    r"|\d{4}-\d{2}-\d{2}(?:\s+\d{1,2}:\d{2})?"
    r"|-+ -+ -+)"
)
_BSDTAR_LINE_RE = re.compile(
    r"^(?P<mode>[-dlhcbpsw?][rwxsStT-]{9}[@+.]?)\s+\d+\s+\d+\s+\d+\s+(?P<size>\d+(?:,\s*\d+)?)"
    r"(?:\s+" + _BSDTAR_DATE + r" | {2})(?P<name>.*)$"
)


class ArchiveError(Exception):
    """Raised when an archive is unreadable, encrypted, unsafe or exceeds limits."""


class ExtractionNoSpace(ArchiveError):
    """The archive is fine but the free disk space does not allow extracting it (now).

    ``needed`` is the free space (bytes, including the reserve) that would have been required when
    the extraction started, or None when it is not known (a write failed with ENOSPC). It is only
    raised for archives that would fit an otherwise empty disk, so a hostile or corrupt declared
    size stays a plain :class:`ArchiveError`.
    """

    def __init__(self, message: str, needed: int | None = None) -> None:
        super().__init__(message)
        self.needed = needed


@dataclass
class ExtractionBudget:
    """Tracks entries and uncompressed bytes against the limits.

    One budget can be shared by every extraction of a release (outer archive plus nested
    ones), so the limits apply to the release as a whole.
    """

    limit: int = -1  # bytes; negative means MAX_TOTAL_BYTES
    entries: int = 0
    total: int = 0
    disk_total: int | None = None  # size of the disk the limit was derived from (for_destination)
    reserve: int = 0  # free space that must stay free (for_destination)

    def __post_init__(self) -> None:
        if self.limit < 0:
            self.limit = MAX_TOTAL_BYTES

    @classmethod
    def for_destination(cls, dest: Path, reserve: int = DISK_RESERVE_BYTES) -> ExtractionBudget:
        """Budget bounded by MAX_TOTAL_BYTES and the free space (minus ``reserve``) at ``dest``."""
        limit = MAX_TOTAL_BYTES
        disk_total: int | None = None
        probe = dest
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        try:
            usage = shutil.disk_usage(probe)
        except OSError:
            logger.warning("Cannot determine free space at %s", probe)
        else:
            limit = min(limit, max(0, usage.free - reserve))
            disk_total = usage.total
        return cls(limit=limit, disk_total=disk_total, reserve=reserve)

    def check_declared(self, declared: int) -> None:
        """Fail early when sizes declared in an archive's directory exceed the budget."""
        if self.total + declared > self.limit:
            raise self._exceeded(self.total + declared)

    def add_entry(self) -> None:
        self.entries += 1
        if self.entries > MAX_ENTRIES:
            raise ArchiveError(f"archive has more than {MAX_ENTRIES} entries")

    def add_bytes(self, count: int) -> None:
        self.total += count
        if self.total > self.limit:
            raise self._exceeded(self.total)

    def check_total(self, extra: int) -> None:
        """Fail when ``extra`` more bytes (already written by an external tool) exceed the budget."""
        if self.total + extra > self.limit:
            raise self._exceeded(self.total + extra)

    def _exceeded(self, required: int) -> ArchiveError:
        """The error for ``required`` bytes not fitting: a space wait when the disk could hold them."""
        if self.limit < MAX_TOTAL_BYTES:
            message = f"archive exceeds the uncompressed size limit ({self.limit} bytes, bounded by free disk space)"
            if (self.disk_total is not None and required <= MAX_TOTAL_BYTES
                    and required + self.reserve <= self.disk_total):
                return ExtractionNoSpace(message, needed=required + self.reserve)
            return ArchiveError(message)
        return ArchiveError("archive exceeds the maximum uncompressed size")


_Budget = ExtractionBudget

_CYRILLIC_LETTER_RE = re.compile("[\u0400-\u04FF]")
_CYRILLIC_RUN_RE = re.compile("[\u0400-\u04FF]{2}")


def _decode_legacy_name(raw: bytes, fallback: str) -> str:
    """Decode a non-UTF-8 entry name, e.g. one written by a Russian Windows archiver.

    Tries UTF-8, then cp866 (DOS/zip default on Russian systems) and cp1251 (Windows ANSI).
    A legacy decoding is accepted only if every non-ASCII character becomes a Cyrillic letter
    and at least two of them are adjacent, so Western names (a lone accented letter) keep
    ``fallback``.
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    for encoding in ("cp866", "cp1251"):
        text = raw.decode(encoding, errors="replace")
        high = [c for c in text if ord(c) > 0x7F]
        if high and all(_CYRILLIC_LETTER_RE.match(c) for c in high) and _CYRILLIC_RUN_RE.search(text):
            return text
    return fallback


def _zip_name(info: zipfile.ZipInfo) -> str:
    """Entry name of ``info``; legacy names (no UTF-8 flag, decoded as cp437) are re-decoded."""
    name = info.filename
    if info.flag_bits & 0x800 or name.isascii():
        return name
    try:
        raw = name.encode("cp437")
    except UnicodeEncodeError:  # pragma: no cover - zipfile used another metadata encoding
        return name
    return _decode_legacy_name(raw, name)


def _tar_name(name: str) -> str:
    """Entry name of a tar member; undecodable bytes (surrogate escapes) are re-decoded."""
    if not any("\udc80" <= c <= "\udcff" for c in name):
        return name
    raw = name.encode("utf-8", "surrogateescape")
    return _decode_legacy_name(raw, raw.decode("utf-8", errors="replace"))


def _archive_suffix(path: Path) -> str | None:
    """Return the matching archive extension of ``path`` (lowercase) or None."""
    name = path.name.lower()
    for ext in sorted(ARCHIVE_EXTS, key=len, reverse=True):
        if name.endswith(ext):
            return ext
    return None


def is_archive(path: Path) -> bool:
    """Return True if the file name ends with a known archive extension."""
    return _archive_suffix(path) is not None


def _entry_parts(name: str) -> list[str]:
    """Validate an archive entry name and return its path components.

    Rejects absolute paths, drive letters, ``..`` components and NUL bytes.
    Returns an empty list for entries that denote the root itself.
    """
    if "\x00" in name:
        raise ArchiveError(f"unsafe entry name (NUL byte): {name!r}")
    norm = name.replace("\\", "/")
    if norm.startswith("/") or _DRIVE_RE.match(norm):
        raise ArchiveError(f"unsafe absolute entry path: {name!r}")
    parts = [p for p in norm.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise ArchiveError(f"unsafe entry path with '..': {name!r}")
    return parts


def _copy_limited(src: IO[bytes], target: Path, budget: _Budget) -> None:
    """Stream ``src`` into ``target`` while counting real bytes against the budget."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "wb") as out:
        while chunk := src.read(_CHUNK):
            budget.add_bytes(len(chunk))
            out.write(chunk)


def _wrap_extract_error(message: str, exc: BaseException) -> ArchiveError:
    """ArchiveError for ``exc``; a full disk (ENOSPC, quota) becomes :class:`ExtractionNoSpace`."""
    if isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)):
        return ExtractionNoSpace(message)
    return ArchiveError(message)


def _extract_zip(archive: Path, dest: Path, budget: ExtractionBudget) -> None:
    try:
        with zipfile.ZipFile(archive) as zf:
            infos = zf.infolist()
            if len(infos) > MAX_ENTRIES:
                raise ArchiveError(f"archive has more than {MAX_ENTRIES} entries")
            declared = 0
            plan: list[tuple[zipfile.ZipInfo, list[str]]] = []
            for info in infos:
                parts = _entry_parts(_zip_name(info))
                if info.flag_bits & 0x1:
                    raise ArchiveError("password-protected archive")
                mode = info.external_attr >> 16
                if info.create_system == 3 and mode:
                    fmt = stat.S_IFMT(mode)
                    if fmt not in (0, stat.S_IFREG, stat.S_IFDIR):
                        raise ArchiveError(f"special entry not allowed: {info.filename!r}")
                declared += info.file_size
                budget.check_declared(declared)
                plan.append((info, parts))
            dest.mkdir(parents=True, exist_ok=True)
            for info, parts in plan:
                if not parts:
                    continue
                budget.add_entry()
                target = dest.joinpath(*parts)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                with zf.open(info) as src:
                    _copy_limited(src, target, budget)
    except ArchiveError:
        raise
    except (zipfile.BadZipFile, zlib.error, lzma.LZMAError, EOFError, RuntimeError,
            NotImplementedError, OSError) as exc:
        raise _wrap_extract_error(f"cannot extract zip {archive.name}: {exc}", exc) from exc


def _extract_tar(archive: Path, dest: Path, budget: ExtractionBudget) -> None:
    try:
        with tarfile.open(archive, "r:*") as tf:
            dest.mkdir(parents=True, exist_ok=True)
            for member in tf:
                parts = _entry_parts(_tar_name(member.name))
                if not (member.isreg() or member.isdir()):
                    raise ArchiveError(f"link or special entry not allowed: {member.name!r}")
                if not parts:
                    continue
                budget.add_entry()
                target = dest.joinpath(*parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                budget.check_declared(member.size)
                src = tf.extractfile(member)
                if src is None:
                    raise ArchiveError(f"cannot read entry: {member.name!r}")
                with src:
                    _copy_limited(src, target, budget)
    except ArchiveError:
        raise
    except (tarfile.TarError, zlib.error, lzma.LZMAError, EOFError, OSError) as exc:
        raise _wrap_extract_error(f"cannot extract tar {archive.name}: {exc}", exc) from exc


# ---------------------------------------------------------------- unprivileged external extractors
# 7z and bsdtar parse files the browser user (a possibly compromised renderer) controls, and they are the parsers with
# memory-safety history. They therefore never run as root: the add-on drops to the user ``extract`` (a uid that is
# neither root nor the browser user) with a clean environment and resource limits, and the extraction target is a folder
# that user owns for the duration of the run. The archive is handed over as an open descriptor (/proc/self/fd/N), so
# no path inside a tree somebody else controls is resolved by the extractor.
def extract_user_ids() -> tuple[int, int] | None:
    """(uid, gid) of the ``extract`` user; None when the add-on is not root (development machine, tests)."""
    if os.name != "posix" or os.geteuid() != 0:
        return None
    import pwd  # POSIX only

    try:
        entry = pwd.getpwnam(EXTRACT_USER)
    except KeyError:
        raise ArchiveError(f"the user '{EXTRACT_USER}' does not exist in this image: archives are not extracted as root; "
                           "rebuild the add-on") from None
    if entry.pw_uid == 0:
        raise ArchiveError(f"the user '{EXTRACT_USER}' must not be root")
    return entry.pw_uid, entry.pw_gid


def _limits_fn(fsize: int):
    """preexec_fn: resource limits of the extractor (runs in the child, after the switch to the unprivileged user)."""
    def apply() -> None:
        if resource is None:
            return
        for name, value in (("RLIMIT_AS", EXTRACT_AS_BYTES), ("RLIMIT_CPU", _BSDTAR_TIMEOUT), ("RLIMIT_FSIZE", fsize),
                            ("RLIMIT_NOFILE", EXTRACT_NOFILE), ("RLIMIT_NPROC", EXTRACT_NPROC), ("RLIMIT_CORE", 0)):
            try:
                resource.setrlimit(getattr(resource, name), (value, value))
            except (ValueError, OSError):
                pass
    return apply


def extractor_kwargs(ids: tuple[int, int] | None, fds: tuple[int, ...] = (), fsize: int = MAX_TOTAL_BYTES) -> dict:
    """``subprocess`` arguments for an extractor: clean environment, no stdin, own session, limits, dropped user."""
    kw: dict = {"env": dict(EXTRACT_ENV), "stdin": subprocess.DEVNULL}
    if os.name == "posix":
        kw.update(start_new_session=True, cwd="/", pass_fds=tuple(fds), preexec_fn=_limits_fn(max(1 << 20, int(fsize))))
        if ids is not None:
            kw.update(user=ids[0], group=ids[1], extra_groups=[], umask=0o077)
    return kw


@contextlib.contextmanager
def _opened_archive(archive: Path):
    """Yield ``(path for the extractor, ids, fds to inherit)`` for ``archive``."""
    ids = extract_user_ids()
    if ids is None:
        yield os.path.abspath(archive), None, ()
        return
    fd = os.open(archive, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ArchiveError(f"not a regular file: {archive.name}")
        if not info.st_mode & 0o004:
            os.fchmod(fd, info.st_mode | 0o004)  # the extract user must be able to read the (untrusted) archive
        yield f"/proc/self/fd/{fd}", ids, (fd,)
    finally:
        os.close(fd)


def _traversable(path: Path) -> bool:
    """True when every folder above ``path`` (and ``path``) can be entered by 'others'."""
    for folder in [path, *path.resolve().parents]:
        try:
            if not os.stat(folder).st_mode & 0o001:
                return False
        except OSError:
            return False
    return True


def _give_dest(dest: Path, ids: tuple[int, int] | None) -> int | None:
    """Make ``dest`` (a folder the add-on just created) the private property of the extract user. Returns its old mode."""
    if ids is None:
        return None
    if dest.is_symlink() or not dest.is_dir():
        raise ArchiveError(f"extraction target is not a plain folder: {dest}")
    if not _traversable(dest):
        raise ArchiveError(f"the extraction folder {dest} is not reachable for the unprivileged user '{EXTRACT_USER}' "
                           "(a folder above it lacks the 'x' bit for others)")
    fd = os.open(dest, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        old = stat.S_IMODE(os.fstat(fd).st_mode)
        os.fchown(fd, ids[0], ids[1])
        os.fchmod(fd, 0o700)
        return old
    finally:
        os.close(fd)


_OPEN_FLAGS = getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
MAX_RECLAIM_DEPTH = 64
EXTRACTED_DIR_MODE = 0o755
EXTRACTED_FILE_MODE = 0o644


def _strip_acl_and_xattrs(fd: int) -> None:
    """Remove every extended attribute (POSIX ACLs included) of the open file; failures are ignored (best effort)."""
    if not hasattr(os, "listxattr"):
        return
    try:
        names = os.listxattr(fd)
    except OSError:
        return
    for name in names:
        with contextlib.suppress(OSError):
            os.removexattr(fd, name)


def _scrub_fd(fd: int, mode: int, owner: tuple[int, int]) -> None:
    os.fchown(fd, *owner)  # first: it clears set-id bits
    _strip_acl_and_xattrs(fd)
    os.fchmod(fd, mode)  # after the ACL removal (chmod would otherwise only change the ACL mask)


def _remove_entry_at(dir_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=dir_fd)
    except (IsADirectoryError, PermissionError):
        shutil.rmtree(name, dir_fd=dir_fd)


def _scrub_dir(dir_fd: int, owner: tuple[int, int], depth: int) -> None:
    """Take the folder open as ``dir_fd`` back: every entry is looked at and opened relative to the descriptor
    with O_NOFOLLOW, nothing is resolved by path. Regular files get root and 0644, folders root and 0755, ACLs and
    xattrs go, everything else (links, FIFOs, sockets, devices) is deleted."""
    for name in os.listdir(dir_fd):
        try:
            info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                if depth >= MAX_RECLAIM_DEPTH:
                    logger.warning("Removing a folder nested deeper than %d levels from an extracted archive", depth)
                    shutil.rmtree(name, dir_fd=dir_fd)
                    continue
                sub_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | _OPEN_FLAGS, dir_fd=dir_fd)
                try:
                    if not os.path.samestat(info, os.fstat(sub_fd)):
                        raise OSError(errno.EBUSY, "entry changed while it was taken back")
                    _scrub_fd(sub_fd, EXTRACTED_DIR_MODE, owner)
                    _scrub_dir(sub_fd, owner, depth + 1)
                finally:
                    os.close(sub_fd)
            elif stat.S_ISREG(info.st_mode):
                if info.st_nlink > 1:
                    continue  # not touched (it could be a link to a file elsewhere); _verify_tree refuses the tree
                # O_NONBLOCK: never hang on a FIFO that replaced the file between stat and open
                file_fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | _OPEN_FLAGS, dir_fd=dir_fd)
                try:
                    now = os.fstat(file_fd)
                    if not stat.S_ISREG(now.st_mode) or not os.path.samestat(info, now):
                        raise OSError(errno.EBUSY, "entry changed while it was taken back")
                    _scrub_fd(file_fd, EXTRACTED_FILE_MODE, owner)
                finally:
                    os.close(file_fd)
            else:
                logger.warning("Removing %s from an extracted archive: %s",
                               "a link" if stat.S_ISLNK(info.st_mode) else "a special file", name)
                os.unlink(name, dir_fd=dir_fd)
        except OSError as exc:
            logger.warning("Cannot take back %r after the extraction (%s); removing it", name, exc)
            with contextlib.suppress(OSError):
                _remove_entry_at(dir_fd, name)


def _reclaim(dest: Path, ids: tuple[int, int] | None, old_mode: int | None) -> None:
    """After the extractor ended: kill anything left of its user, then take the whole tree back with a
    descriptor-based walk (O_NOFOLLOW): owner root, modes 0755/0644, no ACLs/xattrs, no links or special files."""
    if ids is None:
        return
    kill_user_processes(ids)
    owner = (os.geteuid(), os.getegid())
    try:
        top = os.open(dest, os.O_RDONLY | os.O_DIRECTORY | _OPEN_FLAGS)
    except OSError as exc:
        logger.warning("Cannot take back %s after the extraction: %s", dest, exc)
        return
    try:
        _scrub_dir(top, owner, 1)
        _scrub_fd(top, EXTRACTED_DIR_MODE if old_mode is None else old_mode, owner)
    except OSError as exc:
        logger.warning("Cannot take back %s after the extraction: %s", dest, exc)
    finally:
        os.close(top)


def kill_extractor_leftovers() -> None:
    """Kill every process of the ``extract`` user (add-on start). Never raises."""
    try:
        ids = extract_user_ids()
        if ids is not None:
            kill_user_processes(ids)
    except Exception:  # noqa: BLE001
        logger.warning("Killing leftover processes of the user '%s' failed", EXTRACT_USER, exc_info=True)


def _sevenzip_exe() -> str | None:
    """Path of the 7-Zip executable: ``7z`` (Alpine, p7zip) or ``7zz`` (Debian's 7zip package)."""
    return shutil.which("7z") or shutil.which("7zz")


def _run_7z(args: list[str], ids: tuple[int, int] | None = None, fds: tuple[int, ...] = ()) -> str:
    exe = _sevenzip_exe()
    if exe is None:
        raise ArchiveError(("neither 7z nor bsdtar is installed in this image, so this .rar/.7z release was marked as failed; "
                           "rebuild the add-on (the image build installs 7zip and libarchive-tools)"))
    try:
        proc = subprocess.run(
            [exe, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_SEVENZIP_TIMEOUT,
            **extractor_kwargs(ids, fds),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ArchiveError(f"7z failed to run: {exc}") from exc
    finally:
        kill_user_processes(ids)  # a daemon forked by an exploited lister must not outlive the run
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        raise ArchiveError(f"7z exited with code {proc.returncode}: {' | '.join(tail)}")
    return proc.stdout


_NO_SPACE_RE = re.compile(r"no space left on device|disk quota exceeded|not enough space", re.IGNORECASE)


def _dir_bytes(root: Path) -> int:
    """Bytes of the regular files below ``root`` (no symlinks followed; vanishing entries ignored)."""
    total = 0
    stack = [os.fspath(root)]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += os.lstat(entry.path).st_size  # (DirEntry.stat can be stale on Windows)
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _watch_problem(dest: Path, budget: ExtractionBudget) -> ArchiveError | None:
    """Why a running extraction into ``dest`` must be stopped now, or None."""
    try:
        budget.check_total(_dir_bytes(dest))
    except ArchiveError as exc:
        return exc
    if budget.reserve > 0:
        try:
            free = shutil.disk_usage(dest).free
        except OSError:
            return None
        if free < budget.reserve:
            return ExtractionNoSpace("the free disk space fell below its reserve while extracting")
    return None


def _run_extractor(command: list[str], dest: Path, budget: ExtractionBudget, label: str,
                   ids: tuple[int, int] | None = None, fds: tuple[int, ...] = ()) -> str:
    """Run an external extractor and stop it as soon as ``dest`` outgrows the budget.

    The listing the extractor is validated against is only what the archive claims; this bounds
    what is really written, so a lying archive cannot fill the disk. Returns the tool's output.
    """
    deadline = time.monotonic() + _BSDTAR_TIMEOUT
    with tempfile.TemporaryFile() as log:
        try:
            proc = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                    **extractor_kwargs(ids, fds, budget.limit - budget.total))
        except OSError as exc:
            raise ArchiveError(f"{label} failed to run: {exc}") from exc
        try:
            while True:
                try:
                    proc.wait(timeout=_WATCH_INTERVAL)
                    break
                except subprocess.TimeoutExpired:
                    pass
                problem = _watch_problem(dest, budget)
                if problem is None and time.monotonic() > deadline:
                    problem = ArchiveError(f"{label} failed to run: timed out")
                if problem is not None:
                    raise problem
        except BaseException:
            proc.kill()
            proc.wait()
            raise
        finally:
            kill_user_processes(ids)  # also the children the tool left behind
        log.seek(0)
        text = log.read().decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        if _NO_SPACE_RE.search(text):
            raise ExtractionNoSpace(f"{label}: no space left on device")
        if label == "bsdtar" and re.search(r"passphrase|password|encrypted", text, re.IGNORECASE):
            raise ArchiveError("password-protected archive")
        tail = text.splitlines()[-3:]
        raise ArchiveError(f"{label} exited with code {proc.returncode}: {' | '.join(tail)}")
    return text


def _validate_7z_listing(archive: Path, budget: ExtractionBudget) -> None:
    """List the archive with ``7z l -slt -ba`` and validate every entry."""
    with _opened_archive(archive) as (arg, ids, fds):
        output = _run_7z(["l", "-slt", "-ba", "--", arg], ids, fds)
    listing = _Budget(limit=budget.limit)
    declared = 0
    for block in re.split(r"\r?\n\r?\n", output):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            key, sep, value = line.partition(" = ")
            if sep:
                fields[key.strip()] = value.strip()
            elif line.endswith(" ="):
                fields[line[:-2].strip()] = ""
        if "Path" not in fields:
            continue
        _entry_parts(fields["Path"])
        listing.add_entry()
        if fields.get("Encrypted") == "+":
            raise ArchiveError("password-protected archive")
        if any(fields.get(k) for k in ("Link", "Symbolic Link", "Hard Link")):
            raise ArchiveError(f"link entry not allowed: {fields['Path']!r}")
        if _SPECIAL_ATTR_RE.search(fields.get("Attributes", "")):
            raise ArchiveError(f"link or special entry not allowed: {fields['Path']!r}")
        size = fields.get("Size", "")
        if size.isdigit():
            declared += int(size)
            budget.check_declared(declared)


def _bsdtar_exe() -> str | None:
    """Path of the bsdtar executable (``CAR_MEDIA_HUB_BSDTAR`` or ``bsdtar`` on PATH), or None."""
    return shutil.which(os.environ.get(BSDTAR_ENV) or "bsdtar")


def _run_bsdtar(args: list[str], ids: tuple[int, int] | None = None, fds: tuple[int, ...] = ()) -> str:
    exe = _bsdtar_exe()
    if exe is None:
        raise ArchiveError(("bsdtar is not installed in this image, so this release was marked as failed; "
                           "rebuild the add-on (the image build installs 7zip and libarchive-tools)"))
    try:
        proc = subprocess.run(
            [exe, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_BSDTAR_TIMEOUT,
            **extractor_kwargs(ids, fds),  # clean environment (a UTF-8 locale lets libarchive decode entry names)
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ArchiveError(f"bsdtar failed to run: {exc}") from exc
    finally:
        kill_user_processes(ids)
    if proc.returncode != 0:
        text = (proc.stderr or proc.stdout or "").strip()
        if re.search(r"passphrase|password|encrypted", text, re.IGNORECASE):
            raise ArchiveError("password-protected archive")
        tail = text.splitlines()[-3:]
        raise ArchiveError(f"bsdtar exited with code {proc.returncode}: {' | '.join(tail)}")
    return proc.stdout


def _validate_bsdtar_listing(archive: Path, budget: ExtractionBudget) -> None:
    """List the archive with ``bsdtar -tvf`` and allow only regular files and directories."""
    # --numeric-owner: uid/gid are printed as numbers, so no archive text sits before the size
    with _opened_archive(archive) as (arg, ids, fds):
        output = _run_bsdtar(["--numeric-owner", "-tvf", arg], ids, fds)
    listing = _Budget(limit=budget.limit)
    declared = 0
    for line in output.splitlines():
        if not line.strip():
            continue
        match = _BSDTAR_LINE_RE.match(line)
        if match is None:
            raise ArchiveError(f"cannot parse the bsdtar listing: {line[:200]!r}")
        name = match["name"]
        if match["mode"][0] not in "-d":
            raise ArchiveError(f"link or special entry not allowed: {name!r}")
        _entry_parts(name)
        listing.add_entry()
        if match["mode"][0] == "-":
            declared += int(match["size"])
            budget.check_declared(declared)


def _extract_bsdtar(archive: Path, dest: Path, budget: ExtractionBudget) -> None:
    _validate_bsdtar_listing(archive, budget)
    exe = _bsdtar_exe()
    if exe is None:
        raise ArchiveError(("bsdtar is not installed in this image, so this release was marked as failed; "
                           "rebuild the add-on (the image build installs 7zip and libarchive-tools)"))
    dest.mkdir(parents=True, exist_ok=True)
    with _opened_archive(archive) as (arg, ids, fds):
        old_mode = _give_dest(dest, ids)
        try:
            _run_extractor([exe, "-xf", arg, "-C", os.fspath(dest), "--no-same-owner", "--no-same-permissions"],
                           dest, budget, "bsdtar", ids, fds)
        finally:
            _reclaim(dest, ids, old_mode)


def _extract_7z(archive: Path, dest: Path, budget: ExtractionBudget) -> None:
    if _sevenzip_exe() is None and _bsdtar_exe() is not None:
        logger.info("7z is not installed, using bsdtar for %s", archive.name)
        _extract_bsdtar(archive, dest, budget)
        return
    _validate_7z_listing(archive, budget)
    exe = _sevenzip_exe()
    if exe is None:
        raise ArchiveError(("neither 7z nor bsdtar is installed in this image, so this .rar/.7z release was marked as failed; "
                           "rebuild the add-on (the image build installs 7zip and libarchive-tools)"))
    dest.mkdir(parents=True, exist_ok=True)
    with _opened_archive(archive) as (arg, ids, fds):
        old_mode = _give_dest(dest, ids)
        try:
            _run_extractor([exe, "x", "-y", "-bd", f"-o{dest}", "--", arg], dest, budget, "7z", ids, fds)
        finally:
            _reclaim(dest, ids, old_mode)


def _verify_tree(dest: Path, budget: ExtractionBudget | None = None) -> None:
    """Remove symlinks and make sure everything resolves inside ``dest``.

    With ``budget`` (extractions done by an external tool, which are not streamed through
    Python) the bytes found on disk are charged to it and hard-linked files are refused
    (a hard link could expose a file outside ``dest``).
    """
    root = dest.resolve()
    entries = _Budget()
    for dirpath, dirnames, filenames in os.walk(dest):
        for name in list(dirnames) + filenames:
            path = Path(dirpath) / name
            if path.is_symlink():
                logger.warning("Removing symlink from extracted archive: %s", path)
                path.unlink()
                if name in dirnames:
                    dirnames.remove(name)
                continue
            if not path.resolve().is_relative_to(root):
                raise ArchiveError(f"extracted path escapes destination: {path}")
            entries.add_entry()
            if name in filenames:
                info = path.stat()
                if not stat.S_ISREG(info.st_mode):
                    raise ArchiveError(f"special file not allowed: {path.relative_to(dest).as_posix()!r}")
                entries.add_bytes(info.st_size)
                if budget is not None:
                    if info.st_nlink > 1:
                        raise ArchiveError(f"hard-linked file not allowed: {path.relative_to(dest).as_posix()!r}")
                    budget.add_bytes(info.st_size)


def safe_extract(archive: Path, dest: Path, budget: ExtractionBudget | None = None) -> None:
    """Extract ``archive`` into ``dest`` or raise :class:`ArchiveError`.

    ``budget`` bounds the uncompressed size; by default a fresh one limited by the free
    disk space at ``dest``. ``dest`` may be left partially filled on failure; the caller
    removes it.
    """
    ext = _archive_suffix(archive)
    if ext is None:
        raise ArchiveError(f"unsupported archive type: {archive.name}")
    if not archive.is_file():
        raise ArchiveError(f"archive not found: {archive}")
    if budget is None:
        budget = ExtractionBudget.for_destination(dest)
    if ext == ".zip":
        _extract_zip(archive, dest, budget)
        _verify_tree(dest)
    elif ext in _TAR_SUFFIXES:
        _extract_tar(archive, dest, budget)
        _verify_tree(dest)
    elif ext == ".rar":
        _extract_bsdtar(archive, dest, budget)
        _verify_tree(dest, budget)
    else:
        _extract_7z(archive, dest, budget)
        _verify_tree(dest, budget)


def _stem(path: Path) -> str:
    ext = _archive_suffix(path) or ""
    return path.name[: len(path.name) - len(ext)] or path.name


def _unique_dir(parent: Path, stem: str) -> Path:
    candidate = parent / stem
    n = 2
    while candidate.exists():
        candidate = parent / f"{stem} ({n})"
        n += 1
    return candidate


def _visible_files(root: Path):
    """``(dirpath, filenames)`` below ``root``, skipping hidden entries and ``__MACOSX`` folders.

    Only the names of entries below ``root`` are tested (``root`` itself may sit under a dot
    folder). Same rule as the cleaner's hidden-entry removal, which still deletes and reports them.
    """
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not is_hidden_entry(d)]
        yield dirpath, [f for f in filenames if not f.startswith(".")]


def extract_nested(root: Path, max_depth: int = 1, budget: ExtractionBudget | None = None) -> int:
    """Extract archives found inside ``root`` next to themselves and delete them.

    Each archive goes into a folder named after its stem. ``max_depth`` rounds are
    performed, so archives unpacked in round N are only unpacked again if N < max_depth.
    All nested archives share ``budget`` (default: one bounded by free disk space).
    Returns the number of archives extracted.
    """
    if budget is None:
        budget = ExtractionBudget.for_destination(root)
    count = 0
    for _ in range(max_depth):
        found = sorted(
            Path(dirpath) / name
            for dirpath, files in _visible_files(root)
            for name in files
            if is_archive(Path(name)) and not (Path(dirpath) / name).is_symlink()
        )
        if not found:
            break
        for archive in found:
            target = _unique_dir(archive.parent, _stem(archive))
            try:
                safe_extract(archive, target, budget)
            except ArchiveError:
                shutil.rmtree(target, ignore_errors=True)
                raise
            archive.unlink()
            count += 1
    return count
