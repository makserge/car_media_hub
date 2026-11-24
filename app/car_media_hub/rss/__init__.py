"""RSS support: feed parsing, page fetching and the one-time state import. Nothing here depends on
``car_media_hub.sources`` (the source imports us)."""
from __future__ import annotations

from .context import FetchedPage, extract_links, fetch_page, is_file_url, matches_format
from .feed import FeedItem, parse_feed
from .state_import import item_key, url_key

__all__ = [
    "FeedItem",
    "FetchedPage",
    "extract_links",
    "fetch_page",
    "is_file_url",
    "item_key",
    "matches_format",
    "parse_feed",
    "url_key",
]
