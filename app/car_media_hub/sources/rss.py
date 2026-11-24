"""RSS / Atom source: downloads the enclosures of a feed.

Settings: ``feed_url`` (required) and the :class:`RemoteSource` settings. Only enclosures are used;
an item without one is skipped (item pages are never opened). The Media page lists every item; the
files of items without enclosures are downloaded by the person's own browser into the inbox folder.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlparse

from ..netguard import safe_http_url
from ..names import dedupe_name, name_key, safe_component
from ..rss.context import is_http_url
from ..rss.feed import FeedItem, item_fingerprint, parse_feed, split_id
from ..rss.state_import import item_key, url_key
from .base import Candidate, Outcome
from .fetch import has_wanted_extension
from .registry import register_source
from .remote import RemoteFile, RemoteRelease, RemoteSource, is_wanted_file

log = logging.getLogger(__name__)


@dataclass
class _Found:
    """A file URL found for an item, with the name to give it (if known)."""

    url: str
    hint: str | None = None


def _basename(url: str) -> str:
    return unquote(urlparse(url).path.rsplit("/", 1)[-1], errors="replace")


def _last_segment(name: str) -> str:
    return name.replace("\\", "/").rsplit("/", 1)[-1]


@register_source("rss")
class RssSource(RemoteSource):
    """Feed items -> releases; an item's files are its enclosures."""

    name = "rss"
    default_poll_minutes = 1440
    default_max_items_per_scan = 10

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        feed_url = self.settings.get("feed_url")
        if not isinstance(feed_url, str) or not is_http_url(feed_url.strip()):
            raise ValueError(f"rss: 'feed_url' must be an http(s) URL, got {feed_url!r}")
        self.feed_url = feed_url.strip()
        removed = [key for key in ("parser", "parsers_dir") if self.settings.get(key) not in (None, "", "auto")]
        if removed:
            log.warning("%s: %s ignored: custom parsers were removed",
                        self.name, " and ".join(f"'{key}'" for key in removed))
        self._items: dict[str, FeedItem] = {}
        self._warned: set[str] = set()

    # ---- poll lifecycle
    async def poll(self) -> list[Candidate]:
        self._warned = set()
        self._items = {}
        return await super().poll()

    def _warn_once(self, key: str, message: str, *args: Any) -> None:
        if key not in self._warned:
            self._warned.add(key)
            log.warning(message, *args)

    async def feed_items(self) -> list[dict[str, Any]]:
        """The items of the feed as they stand now, for the Media page: id, title, the link to open
        (the item page, else its first file) and whether the item is handled (downloaded, or marked done)."""
        text, final_url = await self.http.get_text(self.feed_url, f"{self.name} feed")
        items = await asyncio.to_thread(parse_feed, text, final_url)
        return [
            {
                "id": item.id,
                "title": item.title or item.link or item.id,
                # only http(s) (or relative) links reach the page: a feed must not put javascript: or data: into an href
                "link": safe_http_url(item.link) or safe_http_url(item.enclosures[0] if item.enclosures else ""),
                "handled": bool(self._db.get_state(item_key(self._prefix, item.id))),
            }
            for item in items
        ]

    def mark_handled(self, item_id: str) -> None:
        """Mark a feed item as done by hand (its files were downloaded in the browser)."""
        self._db.set_state(item_key(self._prefix, item_id), "1")

    # ---- RemoteSource template
    async def list_releases(self) -> list[RemoteRelease]:
        text, final_url = await self.http.get_text(self.feed_url, f"{self.name} feed")
        items = await asyncio.to_thread(parse_feed, text, final_url)
        releases: list[RemoteRelease] = []
        for item in items:
            if item.id in self._items:
                continue
            handled = self._db.get_state(item_key(self._prefix, item.id))
            if handled:
                if not item.link_id or handled in ("1", item_fingerprint(item)):
                    continue  # every file of this item is known already
                # An item without guid takes its page link as id, and a feed that lists one item at
                # a time (or whose items share the home page) reuses it: this is another item than
                # the one handled under this id, so it gets its own release id (the one it would
                # get if the feed listed several items, see feed._split_shared_ids).
                item = FeedItem(split_id(item), item.title, item.link, item.enclosures)
                if item.id in self._items or self._db.get_state(item_key(self._prefix, item.id)):
                    continue
            self._items[item.id] = item
            meta: dict[str, Any] = {"feed": self.feed_url}
            url = item.link or (item.enclosures[0] if item.enclosures else "")
            if url:
                meta["url"] = url
            releases.append(RemoteRelease(item.id, item.title or item.link or item.id, meta))
        log.debug("%s: %d feed items, %d not handled yet", self.name, len(items), len(releases))
        return releases

    async def list_files(self, release: RemoteRelease) -> list[RemoteFile]:
        item = self._items.get(release.id)
        if item is None:
            return []
        found = [_Found(url) for url in item.enclosures]
        if not found:
            return []
        # one entry per URL (the first hint wins); only http(s) is ever fetched (RSS-7)
        unique: dict[str, _Found] = {}
        for entry in found:
            if not is_http_url(entry.url):
                log.warning("%s: ignoring non-http(s) file URL %r", self.name, entry.url)
                continue
            unique.setdefault(entry.url, entry)
        if not unique:
            return []
        new = [e for e in unique.values() if not self._db.get_state(url_key(self._prefix, e.url))]
        if not new:
            log.debug("%s: every file of %r is known; marking the item as handled", self.name, release.id)
            self._db.set_state(item_key(self._prefix, release.id), self._handled_mark(item))
            return []
        release.meta["urls"] = list(unique)
        release.meta["item_id"] = release.id
        release.meta["item_mark"] = self._handled_mark(item)
        taken: set[str] = set()
        files: list[RemoteFile] = []
        for entry in new:
            hint = safe_component(_last_segment(entry.hint)) if entry.hint and _last_segment(entry.hint) else None
            name = hint or safe_component(_basename(entry.url), fallback="download")
            name = dedupe_name(name, taken)
            taken.add(name_key(name))
            files.append(
                RemoteFile(
                    name=name,
                    url=entry.url,
                    size=None,  # <enclosure length> is often wrong: never a size limit
                    name_from_response=hint is None and not has_wanted_extension(name),
                )
            )
        return files

    @staticmethod
    def _handled_mark(item: FeedItem) -> str:
        """The value of an item's "handled" key: its fingerprint when its id is a page link (which
        other items may share), else "1"."""
        return item_fingerprint(item) if item.link_id else "1"

    def select_file(self, f: RemoteFile) -> bool:
        return is_wanted_file(f.name) or f.name_from_response

    async def finalize(self, candidate: Candidate, outcome: Outcome) -> None:
        if outcome in ("done", "duplicate", "empty"):
            try:
                for url in candidate.meta.get("urls") or []:
                    self._db.set_state(url_key(self._prefix, url), "1")
                item_id = candidate.meta.get("item_id") or candidate.meta.get("identifier")
                if item_id:
                    self._db.set_state(item_key(self._prefix, item_id), candidate.meta.get("item_mark") or "1")
            except Exception:  # noqa: BLE001 - the base class must still clean up
                log.warning("%s: could not record handled URLs", self.name, exc_info=True)
        await super().finalize(candidate, outcome)
