"""SHA-256 helpers for files and directory trees."""
from __future__ import annotations

import hashlib
from pathlib import Path


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    """Return the hex SHA-256 digest of a file, read in ``chunk``-sized blocks."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def hash_path(path: Path) -> str:
    """Hash a file or a directory tree.

    A file hashes to its SHA-256. A directory hashes over the sorted
    ``(posix relpath, sha256)`` pairs of all regular files; symlinks are ignored.
    """
    path = Path(path)
    if not path.is_dir():
        return sha256_file(path)
    entries: list[tuple[str, str]] = []
    for child in path.rglob("*"):
        if child.is_symlink() or not child.is_file():
            continue
        entries.append((child.relative_to(path).as_posix(), sha256_file(child)))
    entries.sort()
    digest = hashlib.sha256()
    for rel, file_hash in entries:
        digest.update(_encode_rel(rel) + f"\0{file_hash}\n".encode("ascii"))
    return digest.hexdigest()


def _encode_rel(rel: str) -> bytes:
    """``rel`` as bytes with undecodable name bytes restored (surrogate escapes), so any name hashes.

    Valid names hash exactly as before; a name that cannot be restored (a lone surrogate that is
    not an escaped byte, possible on Windows) is encoded with ``surrogatepass`` instead of failing.
    """
    try:
        return rel.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:
        return rel.encode("utf-8", "surrogatepass")
