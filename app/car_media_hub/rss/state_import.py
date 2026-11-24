"""State keys of the RSS source: a file URL or a feed item is remembered under a key derived from its
SHA-256, so the state never stores the raw URL or item id."""
from __future__ import annotations

import hashlib


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def url_key(prefix: str, url: str) -> str:
    """State key remembering that a file URL was handled."""
    return f"{prefix}:url:{_sha256(url)}"


def item_key(prefix: str, item_id: str) -> str:
    """State key remembering that a feed item was handled completely."""
    return f"{prefix}:item:{_sha256(item_id)}"
