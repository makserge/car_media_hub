"""Free-space guard shared by the downloaders and the pipeline."""
from __future__ import annotations

import errno
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config


class NoSpaceError(Exception):
    """Not enough free space (deliberately not an ``OSError``: HTTP retries must not swallow it).

    ``needed`` (when known) is the free space in bytes, reserve included, the work would have needed.
    """

    def __init__(self, message: str = "", needed: int | None = None) -> None:
        super().__init__(message)
        self.needed = needed


_NO_SPACE_ERRNOS = frozenset({errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)})


def is_no_space_error(exc: BaseException | None) -> bool:
    """True when ``exc`` (or the exception it was raised from) is a full disk or quota error.

    ``shutil.copytree`` collects per-file failures into one ``shutil.Error`` whose ``errno`` is
    None, so its messages are searched for ``[Errno 28]`` too.
    """
    seen = 0
    while exc is not None and seen < 8:
        if isinstance(exc, OSError) and exc.errno in _NO_SPACE_ERRNOS:
            return True
        if isinstance(exc, shutil.Error) and any(
            f"[Errno {number}]" in str(exc) for number in _NO_SPACE_ERRNOS
        ):
            return True
        exc = exc.__cause__
        seen += 1
    return False


def human_bytes(value: float) -> str:
    """Format a byte count like ``1.5 GiB``."""
    size = float(max(0, value))
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024:
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def free_bytes(path: Path) -> int:
    """Free bytes of the file system holding ``path`` (its nearest existing ancestor)."""
    probe = Path(path)
    while True:
        try:
            if probe.exists():
                break
        except OSError:
            pass
        if probe.parent == probe:
            break
        probe = probe.parent
    return shutil.disk_usage(probe).free


def min_free_bytes(cfg: Config) -> int:
    """The configured free-space reserve in bytes."""
    return int(cfg.min_free_space_gb * 1024**3)


def ensure_free(path: Path, need_bytes: int, min_free: int) -> None:
    """Raise :class:`NoSpaceError` unless ``need_bytes`` fit on ``path`` and leave ``min_free`` free."""
    free = free_bytes(path)
    if free - need_bytes < min_free:
        raise NoSpaceError(
            f"not enough free space on {path}: {human_bytes(free)} free, "
            f"{human_bytes(need_bytes)} needed, {human_bytes(min_free)} must stay free",
            needed=need_bytes + min_free,
        )
