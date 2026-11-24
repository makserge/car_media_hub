"""Which code is running: a build id over the add-on's Python files, the start time and the add-on version.

``build_id`` is the first 10 hex characters of the SHA-256 over, for every ``*.py`` file below the package folder (sorted by
relative path with ``/`` as separator, ``__pycache__`` ignored): the relative path as UTF-8, a zero byte, the file bytes and a
zero byte. ``tools/addon_build_id.py`` computes the same value for any folder, to compare with the Regions page footer and the
``build`` key of ``/regions/api/list`` and ``/api/regions/devices``.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PACKAGE = Path(__file__).resolve().parent
_STARTED = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
_cache: dict[str, Any] | None = None


def compute_build_id(folder: Path) -> str:
    """The build id of the Python files below ``folder`` (blocking, a few hundred KB)."""
    digest = hashlib.sha256()
    files = sorted((p for p in Path(folder).rglob("*.py") if "__pycache__" not in p.parts), key=lambda p: p.relative_to(folder).as_posix())
    for path in files:
        digest.update(path.relative_to(folder).as_posix().encode("utf-8") + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()[:10]


def read_version(package: Path = PACKAGE) -> str:
    """The ``version:`` of config.yaml when it can be found next to the app (``/app/config.yaml`` in the image), else ''."""
    for candidate in (package.parent / "config.yaml", package.parent.parent / "config.yaml"):
        try:
            for line in candidate.read_text(encoding="utf-8").splitlines():
                match = re.match(r"version:\s*[\"']?([^\"'\s#]+)", line)
                if match:
                    return match.group(1)
        except OSError:
            continue
    return ""


def build_info() -> dict[str, str]:
    """``{"id", "started", "version"}``, computed once per process."""
    global _cache
    if _cache is None:
        try:
            build_id = compute_build_id(PACKAGE)
        except OSError:
            build_id = ""
        _cache = {"id": build_id, "started": _STARTED, "version": read_version()}
    return dict(_cache)


def footer_text(info: dict[str, str]) -> str:
    return f"build {info['id'] or '?'} · started {info['started']} · v{info['version']}"
