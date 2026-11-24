"""Release cleaning: drop unwanted/duplicate audio and report what was kept."""

from __future__ import annotations

import logging
import os
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import mutagen

from car_media_hub.config import AUDIO_EXTS
from car_media_hub.hashing import sha256_file
from car_media_hub.names import SYNC_AUDIO_EXTS, dedupe_name, is_hidden_entry, name_key, safe_component

if TYPE_CHECKING:
    from car_media_hub.config import Config

logger = logging.getLogger(__name__)

CYRILLIC_RE = re.compile(r"[Ѐ-ԯᲀ-᲏ⷠ-ⷿꙀ-ꚟ]")
_SURROGATE_RE = re.compile("[\ud800-\udfff]")


def has_cyrillic(text: str) -> bool:
    """Return True if ``text`` contains any Cyrillic character."""
    return CYRILLIC_RE.search(text) is not None


def display_text(text: str) -> str:
    """Replace lone surrogates (undecodable file-name bytes) so the text is valid UTF-8."""
    return _SURROGATE_RE.sub("�", text)


@dataclass
class CleanReport:
    """What the cleaner removed and what remains in the release."""

    removed_cyrillic_name: list[str] = field(default_factory=list)
    removed_cyrillic_tags: list[str] = field(default_factory=list)
    removed_mp3_due_to_flac: list[str] = field(default_factory=list)
    removed_duplicate_tracks: list[str] = field(default_factory=list)
    tags_stripped: list[str] = field(default_factory=list)
    kept_tracks: list[tuple[str, str]] = field(default_factory=list)  # (posix relpath, sha256)
    other_files: list[str] = field(default_factory=list)
    total_bytes: int = 0
    removed_hidden: list[str] = field(default_factory=list)  # hidden entries and __MACOSX folders
    renamed: list[list[str]] = field(default_factory=list)  # [old relpath, new relpath] pairs
    unsyncable: list[str] = field(default_factory=list)  # kept audio the sync API does not serve

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable representation (tuples become two-item lists).

        Paths are made valid UTF-8 (undecodable bytes become U+FFFD) so they can be stored
        and rendered.
        """
        def names(items: list[str]) -> list[str]:
            return [display_text(s) for s in items]

        return {
            "removed_cyrillic_name": names(self.removed_cyrillic_name),
            "removed_cyrillic_tags": names(self.removed_cyrillic_tags),
            "removed_mp3_due_to_flac": names(self.removed_mp3_due_to_flac),
            "removed_duplicate_tracks": names(self.removed_duplicate_tracks),
            "tags_stripped": names(self.tags_stripped),
            "kept_tracks": [[display_text(rel), digest] for rel, digest in self.kept_tracks],
            "other_files": names(self.other_files),
            "total_bytes": self.total_bytes,
            "removed_hidden": names(self.removed_hidden),
            "renamed": [[display_text(old), display_text(new)] for old, new in self.renamed],
            "unsyncable": names(self.unsyncable),
        }


def _ext(path: Path) -> str:
    """Lower-case extension of ``path`` as it will be named once the names are fixed.

    ``07 Track.mp3 `` and ``07 Track.mp3.`` become ``07 Track.mp3`` later (``safe_component``), so
    they are audio now already; the plain ``suffix`` would be ``.mp3 `` or empty.
    """
    return os.path.splitext(safe_component(path.name))[1].lower()


def _is_audio(path: Path) -> bool:
    return _ext(path) in AUDIO_EXTS


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _list_files(root: Path) -> list[Path]:
    """Return regular files under ``root`` sorted by posix relpath; symlinks are deleted."""
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in list(dirnames):
            path = Path(dirpath) / name
            if path.is_symlink():
                logger.warning("Removing symlink %s", path)
                path.unlink()
                dirnames.remove(name)
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_symlink():
                logger.warning("Removing symlink %s", path)
                path.unlink()
            else:
                files.append(path)
    files.sort(key=lambda p: _rel(root, p))
    return files


_MAX_TEXT_BYTES = 64 * 1024  # bytes values larger than this are binary (cover art etc.)


def _tag_texts(value: Any, depth: int = 0) -> list[str]:
    """Text found in a mutagen tag value of any format.

    Handles plain strings, lists, ID3 frames (``.text``, ``.desc``; COMM/USLT/TXXX...),
    ASF/APEv2 attribute objects (``str()``), and UTF-8 bytes (MP4 freeform atoms).
    """
    if depth > 3 or value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (bytes, bytearray)):
        if len(value) > _MAX_TEXT_BYTES:
            return []
        try:
            return [bytes(value).decode("utf-8")]
        except UnicodeDecodeError:
            return []
    if isinstance(value, (list, tuple)):
        return [t for item in value for t in _tag_texts(item, depth + 1)]
    if hasattr(value, "text"):  # ID3 frame
        texts = _tag_texts(value.text, depth + 1)
        desc = getattr(value, "desc", None)
        return texts + ([desc] if isinstance(desc, str) else [])
    if isinstance(value, (int, float, bool)):
        return []
    return [str(value)]


def _tags_have_cyrillic(path: Path) -> bool:
    """True if any text tag value of the audio file contains Cyrillic; errors -> False.

    Reads the easy view (as the spec prescribes) and the raw tags, which also expose frames
    the easy view hides (ID3 COMM/USLT/TXXX, WAV/AIFF ID3 chunks, ASF and APEv2 attributes).
    """
    try:
        for easy in (True, False):
            audio = mutagen.File(path, easy=easy)
            tags = audio.tags if audio is not None else None
            if not tags:
                continue
            for _key, values in tags.items():
                if any(has_cyrillic(text) for text in _tag_texts(values)):
                    return True
    except Exception as exc:  # noqa: BLE001 - mutagen raises many unrelated types
        logger.info("Cannot read tags of %s, keeping file: %s", path, exc)
    return False


def _strip_tags(path: Path) -> bool:
    """Remove all tags from an audio file. Returns True if tags were removed."""
    try:
        audio = mutagen.File(path)
        if audio is None or audio.tags is None:
            return False
        try:
            audio.delete()
        except Exception:  # noqa: BLE001
            audio.tags.clear()
            audio.save()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.info("Cannot strip tags of %s: %s", path, exc)
        return False


def _remove_empty_dirs(root: Path) -> None:
    for dirpath, _dirs, _files in os.walk(root, topdown=False):
        path = Path(dirpath)
        if path == root:
            continue
        try:
            path.rmdir()  # only succeeds when empty
        except OSError:
            pass


def _remove_hidden(root: Path) -> list[str]:
    """Delete hidden entries (leading ".") and ``__MACOSX`` folders; returns their relpaths."""
    removed: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        for name in list(dirnames):
            if not is_hidden_entry(name):
                continue
            path = base / name
            dirnames.remove(name)
            removed.append(_rel(root, path) + "/")
            if path.is_symlink():
                path.unlink()
            else:
                shutil.rmtree(path, ignore_errors=True)
        for name in filenames:
            if name.startswith("."):  # (a file named __MACOSX is an ordinary file)
                path = base / name
                path.unlink()
                removed.append(_rel(root, path))
    return sorted(removed)


def _collapse_wrappers(root: Path) -> None:
    """While ``root`` holds exactly one directory, move its children up and remove it."""
    while True:
        names = os.listdir(root)
        if len(names) != 1:
            return
        wrapper = root / names[0]
        if wrapper.is_symlink() or not wrapper.is_dir():
            return
        # Park the wrapper under a dot-name first: a child may carry the wrapper's own name.
        parked = root / ".mw-collapse"
        counter = 1
        while parked.exists() or parked.is_symlink():
            counter += 1
            parked = root / f".mw-collapse{counter}"
        os.rename(wrapper, parked)
        for child in sorted(os.listdir(parked)):
            os.rename(parked / child, root / child)
        parked.rmdir()
        logger.info("Collapsed wrapper folder %r", names[0])


def _normalise_directory(directory: Path, rel_dir: str, renames: dict[str, str]) -> None:
    """Give every entry of ``directory`` a listable, collision-free name.

    ``rel_dir`` is the directory's relpath as it was before any rename, and ``renames`` maps the
    original relpath of each renamed entry to its new name. Entries that need a new name are
    first parked under temporary names, so a new name never lands on a file that still exists.
    """
    entries = sorted(os.listdir(directory))
    claimed: set[str] = set()
    movers: list[tuple[str, str]] = []  # (current name, wanted name)
    for name in entries:
        wanted = safe_component(name, "folder" if (directory / name).is_dir() else "file")
        key = name_key(name)
        if wanted == name and key not in claimed:
            claimed.add(key)
        else:
            movers.append((name, wanted))
    if not movers:
        return
    parked: list[tuple[Path, str, str]] = []
    for index, (name, wanted) in enumerate(movers):
        temp = directory / f".mw-rename-{index}"
        os.rename(directory / name, temp)
        parked.append((temp, name, wanted))
    for temp, name, wanted in parked:
        final = dedupe_name(wanted, claimed)
        claimed.add(name_key(final))
        os.rename(temp, directory / final)
        renames[f"{rel_dir}/{name}" if rel_dir else name] = final


def _normalise_names(root: Path) -> list[list[str]]:
    """Normalise every name below ``root`` bottom-up; returns [old relpath, final relpath] pairs."""
    renames: dict[str, str] = {}
    for dirpath, _dirs, _files in os.walk(root, topdown=False, followlinks=False):
        directory = Path(dirpath)
        rel_dir = "" if directory == root else _rel(root, directory)
        _normalise_directory(directory, rel_dir, renames)

    def final_path(orig: str) -> str:
        parts: list[str] = []
        prefix = ""
        for part in orig.split("/"):
            prefix = f"{prefix}/{part}" if prefix else part
            parts.append(renames.get(prefix, part))
        return "/".join(parts)

    return [[orig, final_path(orig)] for orig in sorted(renames)]


def repair_names(root: Path) -> tuple[list[str], list[list[str]]]:
    """Apply the 0.2 name hygiene (hidden entries, listable names) to a release cleaned by 0.1.

    Returns ``(removed_hidden, renamed)`` like the matching :class:`CleanReport` fields. Wrapper
    folders are left alone, so a relpath only changes through ``renamed`` (see :func:`map_renamed`).
    """
    return _remove_hidden(root), _normalise_names(root)


def map_renamed(rel: str, renamed: list[list[str]]) -> str:
    """The final relpath of ``rel`` after the ``[old, new]`` pairs of ``renamed`` were applied."""
    mapping = {old: new for old, new in renamed}
    if rel in mapping:
        return mapping[rel]
    parts = rel.split("/")
    for cut in range(len(parts) - 1, 0, -1):
        head = "/".join(parts[:cut])
        if head in mapping:
            return "/".join([mapping[head], *parts[cut:]])
    return rel


def clean_release(root: Path, cfg: Config, is_known_track: Callable[[str], bool]) -> CleanReport:
    """Clean the extracted release in ``root`` in place and return a report."""
    report = CleanReport()

    # (0) Hidden entries and macOS resource folders.
    report.removed_hidden = _remove_hidden(root)

    # (1) Cyrillic file names (and optionally directory names).
    for path in _list_files(root):
        rel = path.relative_to(root)
        bad = has_cyrillic(path.name)
        if not bad and cfg.check_directory_names:
            bad = any(has_cyrillic(part) for part in rel.parts[:-1])
        if bad:
            path.unlink()
            report.removed_cyrillic_name.append(rel.as_posix())

    # (2) Cyrillic in text tags.
    for path in _list_files(root):
        if _is_audio(path) and _tags_have_cyrillic(path):
            path.unlink()
            report.removed_cyrillic_tags.append(_rel(root, path))

    # (3) FLAC beats MP3.
    files = _list_files(root)
    if any(_ext(p) == ".flac" for p in files):
        for path in files:
            if _ext(path) == ".mp3":
                path.unlink()
                report.removed_mp3_due_to_flac.append(_rel(root, path))

    # (4) Strip tags.
    if cfg.strip_all_tags:
        for path in _list_files(root):
            if _is_audio(path) and _strip_tags(path):
                report.tags_stripped.append(_rel(root, path))

    # (4a) Empty directories, then wrapper folders; (4b) listable, collision-free names.
    _remove_empty_dirs(root)
    _collapse_wrappers(root)
    report.renamed = _normalise_names(root)

    # (5) Dedupe against known tracks and within the release.
    seen: set[str] = set()
    for path in _list_files(root):
        if not _is_audio(path):
            continue
        rel = _rel(root, path)
        digest = sha256_file(path)
        if digest in seen or (cfg.dedupe_tracks and is_known_track(digest)):
            path.unlink()
            report.removed_duplicate_tracks.append(rel)
            continue
        seen.add(digest)
        report.kept_tracks.append((rel, digest))

    # (6) Empty directories, then final inventory.
    _remove_empty_dirs(root)
    for path in _list_files(root):
        report.total_bytes += path.stat().st_size
        if not _is_audio(path):
            report.other_files.append(_rel(root, path))
    report.unsyncable = [rel for rel, _digest in report.kept_tracks
                         if Path(rel).suffix.lower() not in SYNC_AUDIO_EXTS]
    return report
