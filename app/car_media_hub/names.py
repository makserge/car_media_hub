"""File and path-segment name hygiene shared by the cleaner, the downloaders and the sync API.

The rules follow docs/SYNC_PROTOCOL.md section 4.2 (rules 1 and 4): a segment that fails
:func:`segment_problem` would not be listed by the sync API, so names are fixed up front with
:func:`safe_component`.
"""
from __future__ import annotations

import re
import unicodedata

SYNC_AUDIO_EXTS = frozenset({".mp3", ".flac", ".wav", ".ogg", ".m4a"})
MAX_SEGMENT_BYTES = 240  # cyp writes "<name>.part" and NAME_MAX is 255 bytes
_MAX_EXT_BYTES = 16
_BAD_CHARS = frozenset('"*:<>?|')
_SURROGATES_RE = re.compile("[\ud800-\udfff]")
_REPLACE_RE = re.compile('[\\x00-\\x1f\\x7f/\\\\"*:<>?|]')


MACOSX_DIR = "__MACOSX"


def is_hidden_entry(name: str) -> bool:
    """True for a single entry name the importer treats as junk: hidden (leading dot) or ``__MACOSX``.

    The cleaner deletes such entries and the nested-archive extraction skips them, so both use this
    one rule. Pass a bare file or folder name, never a path.
    """
    return name.startswith(".") or name == MACOSX_DIR


def replace_surrogates(text: str) -> str:
    """Replace lone surrogates (undecodable file-name bytes) with U+FFFD so the text is valid UTF-8."""
    return _SURROGATES_RE.sub("�", text)


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8", errors="surrogatepass"))


def segment_problem(seg: str) -> str | None:
    """Why ``seg`` (one path segment) is not listable by the sync API, or None when it is fine."""
    if seg in ("", ".", ".."):
        return "empty or dot segment"
    if seg.startswith("."):
        return "hidden (leading dot)"
    try:
        encoded = seg.encode("utf-8")
    except UnicodeEncodeError:
        return "not valid UTF-8"
    if "/" in seg:
        return "contains a slash"
    if "\\" in seg:
        return "contains a backslash"
    for ch in seg:
        if ord(ch) < 0x20 or ord(ch) == 0x7F:
            return "contains a control character"
    bad = sorted(set(seg) & _BAD_CHARS)
    if bad:
        return f"contains a character that is not allowed ({' '.join(bad)})"
    if seg[0] == " " or seg[-1] == " ":
        return "leading or trailing space"
    if seg.endswith("."):
        return "trailing dot"
    if len(encoded) > MAX_SEGMENT_BYTES:
        return f"longer than {MAX_SEGMENT_BYTES} bytes"
    return None


def _split_ext(name: str, max_bytes: int) -> tuple[str, str]:
    """Split off the extension (the last ``.xyz``, at most 16 bytes) when a stem remains."""
    idx = name.rfind(".")
    if idx > 0:
        ext = name[idx:]
        if _utf8_len(ext) <= _MAX_EXT_BYTES and _utf8_len(ext) < max_bytes:
            return name[:idx], ext
    return name, ""


def _truncate(text: str, max_bytes: int) -> str:
    """Cut ``text`` to ``max_bytes`` UTF-8 bytes without splitting a character."""
    if max_bytes <= 0:
        return ""
    return text.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")


def _sanitize(name: str, max_bytes: int) -> str:
    text = _SURROGATES_RE.sub("_", name)
    text = unicodedata.normalize("NFC", text)
    text = _REPLACE_RE.sub("_", text).strip(". ")
    if _utf8_len(text) > max_bytes:
        stem, ext = _split_ext(text, max_bytes)
        text = _truncate(stem, max_bytes - _utf8_len(ext)).rstrip(". ") + ext
        text = text.strip(". ")
    return text


def safe_component(name: str, fallback: str = "file", max_bytes: int = MAX_SEGMENT_BYTES) -> str:
    """Turn ``name`` into a path segment that passes :func:`segment_problem`.

    NFC; lone surrogates, slashes, backslashes, control characters and ``" * : < > ? |`` become
    ``_``; leading dots/spaces and trailing dots/spaces are stripped; the result is capped at
    ``max_bytes`` UTF-8 bytes (keeping a short extension, never splitting a character). An empty
    result becomes ``fallback``.
    """
    result = _sanitize(name, max_bytes) or _sanitize(fallback, max_bytes) or "file"
    return result


def name_key(path: str) -> str:
    """Key under which two names collide on case- and normalisation-insensitive file systems."""
    return unicodedata.normalize("NFC", path).casefold()


def dedupe_name(name: str, taken_keys: set[str], max_bytes: int = MAX_SEGMENT_BYTES,
                split_ext: bool = True) -> str:
    """Return ``name``, or ``name`` with " (2)", " (3)", ... before the extension, whose
    :func:`name_key` is not in ``taken_keys`` (the set is not modified).

    With ``split_ext=False`` the whole name is the stem (for folder names, which have no extension:
    "Vol. 2" is not a file called "Vol" with the extension ". 2").
    """
    if name_key(name) not in taken_keys:
        return name
    stem, ext = _split_ext(name, max_bytes) if split_ext else (name, "")
    counter = 2
    while True:
        suffix = f" ({counter})"
        room = max_bytes - _utf8_len(ext) - _utf8_len(suffix)
        base = stem if _utf8_len(stem) <= room else _truncate(stem, room).rstrip(". ")
        candidate = f"{base}{suffix}{ext}"
        if name_key(candidate) not in taken_keys:
            return candidate
        counter += 1
