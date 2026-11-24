"""RSS 2.0 / Atom feed parsing (namespace-agnostic) into :class:`FeedItem` objects."""
from __future__ import annotations

import hashlib
import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse, urlsplit

log = logging.getLogger(__name__)


@dataclass
class FeedItem:
    """One feed entry: a stable id, a title, the page link and the enclosure URLs."""

    id: str
    title: str
    link: str | None
    enclosures: list[str] = field(default_factory=list)
    link_id: bool = False  # the id is the item's page link (no guid): a link shared by items is not unique


def _local(tag: object) -> str:
    """The tag name without its XML namespace (comments and processing instructions give '')."""
    return tag.split("}")[-1] if isinstance(tag, str) else ""


def _absolute(base_url: str, url: str) -> str | None:
    """``url`` resolved against ``base_url``; absolute URLs are kept byte for byte.

    None (and a warning naming the URL) for a malformed URL such as ``http://host[/x]``.
    """
    url = url.strip()
    try:
        if urlparse(url).scheme:
            return url
        return urljoin(base_url, url)
    except ValueError as exc:  # e.g. "Invalid IPv6 URL"
        log.warning("Feed: ignoring malformed URL %r: %s", url, exc)
        return None


def _dedupe(urls: list[str]) -> list[str]:
    return list(dict.fromkeys(urls))


def _parse_item(item: ET.Element, base_url: str) -> FeedItem | None:
    enclosures: list[str] = []
    link: str | None = None
    guid = title = ""
    for el in item:
        name = _local(el.tag)
        if name == "enclosure" and el.get("url", "").strip():
            url = _absolute(base_url, el.get("url", ""))
            if url is not None:
                enclosures.append(url)
        elif name == "link":
            rel = (el.get("rel") or "").strip().lower()
            href = (el.get("href") or "").strip()
            if rel == "enclosure":
                if href:
                    url = _absolute(base_url, href)
                    if url is not None:
                        enclosures.append(url)
            elif rel in ("", "alternate") and link is None:  # never replies / edit / self (RSS-12)
                target = (el.text or "").strip() or href
                if target:
                    link = _absolute(base_url, target)  # None for a malformed link: a later one may do
        elif name in ("guid", "id") and not guid:
            guid = (el.text or "").strip()
        elif name == "title" and not title:
            title = "".join(el.itertext()).strip()
    enclosures = _dedupe(enclosures)
    item_id = guid or link or (enclosures[0] if enclosures else "")
    if not item_id:
        return None
    return FeedItem(id=item_id, title=title, link=link, enclosures=enclosures, link_id=not guid and bool(link))


def _without_query(url: str) -> str:
    try:
        return urlsplit(url)._replace(query="", fragment="").geturl()
    except ValueError:
        return url


def _basis(item: FeedItem) -> str:
    """What tells an item apart from others that share its id: its enclosure URLs without query
    string and fragment (signed URLs rotate) and its title, else its own link, else its title."""
    if item.enclosures:
        return "\n".join([*(_without_query(u) for u in item.enclosures), item.title])
    if item.link and item.link != item.id:
        return item.link
    return item.title or item.id


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def item_fingerprint(item: FeedItem) -> str:
    """A stable fingerprint of what an item is (:func:`_basis`): it survives rotating query strings.

    The RSS source stores it for items whose id is their (possibly shared) page link, to tell a new
    item that reuses the id from the one it already handled.
    """
    return _digest(_basis(item))


def split_id(item: FeedItem) -> str:
    """The release id ``<id>#<hash>`` of an item that shares its id with other items."""
    return f"{item.id}#{item_fingerprint(item)[:16]}"


def _identity(item: FeedItem) -> str:
    """Items of one id with the same identity are true duplicates: the same files (full URLs, so
    two files that differ only in the query string are different items), else the same link / title."""
    if item.enclosures:
        return "files:" + "\n".join(item.enclosures)
    return "text:" + _basis(item)


def _split_shared_ids(items: list[FeedItem], base_url: str) -> list[FeedItem]:
    """Tell items that share an id apart: when the items of one id differ in what they point to
    (:func:`_identity`), every one of them (the first too) gets the id ``<id>#<hash>`` (:func:`split_id`).
    Items that still share an id are true duplicates: only the first is kept.

    A feed without guids whose items all link to the show's home page would otherwise collapse
    into one release (and, once that is handled, never download anything again). The hash covers the
    enclosure URLs without their query string and the title, so rotating tokens do not change the id;
    items that would still get the same hash (``download.php?id=1`` and ``?id=2`` with equal titles)
    are told apart by their full URLs.
    """
    kinds: dict[str, dict[str, FeedItem]] = {}
    for item in items:
        kinds.setdefault(item.id, {}).setdefault(_identity(item), item)
    split = {i for i, group in kinds.items() if len(group) > 1}
    new_ids: dict[tuple[str, str], str] = {}
    for i in split:
        group = kinds[i]
        hashes = {ident: split_id(it) for ident, it in group.items()}
        for ident, it in group.items():
            clash = sum(1 for other in hashes.values() if other == hashes[ident]) > 1
            new_ids[(i, ident)] = (
                f"{i}#{_digest(_basis(it) + chr(0) + chr(10).join(it.enclosures))[:16]}" if clash else hashes[ident]
            )
    if split:
        log.warning(
            "Feed %s: %d feed id(s) are used by several different items (first: %r); telling the items "
            "apart by their enclosure URLs and titles. Give the items unique <guid>s if possible.",
            base_url, len(split), next(iter(split)),
        )
    out: list[FeedItem] = []
    taken: set[str] = set()
    for item in items:
        if item.id in split:
            item = FeedItem(new_ids[(item.id, _identity(item))], item.title, item.link, item.enclosures)
        if item.id in taken:
            log.debug("Feed %s: dropping a repeated item %r", base_url, item.id)
            continue
        taken.add(item.id)
        out.append(item)
    return out


def parse_feed(xml_text: str, base_url: str) -> list[FeedItem]:
    """Parse an RSS 2.0 or Atom document into feed items, in document order.

    Enclosures are ``<enclosure url>`` or Atom ``<link rel="enclosure" href>``; the item link is
    the text of a ``<link>`` or the href of an Atom ``<link>`` whose rel is absent or "alternate".
    The id is the guid / Atom id, else the link, else the first enclosure; items without any of
    them are skipped. When several items share an id (no guid and the same page link, or a repeated
    guid) and they point to different files (full URLs), each gets ``<id>#<hash of its enclosure URLs
    without query, and its title>``; identical repeats are dropped (the first is kept). A malformed URL (``http://host[/x]``) is ignored, never failing the whole feed.
    URLs are made absolute against ``base_url``. A document that declares
    entities is refused (``ValueError``), as is one that is not well-formed XML.
    """
    if "<!ENTITY" in xml_text:
        raise ValueError("feed declares XML entities; refusing to parse it")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError(f"feed is not valid XML: {exc}") from exc
    items: list[FeedItem] = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        try:
            parsed = _parse_item(el, base_url)
        except Exception as exc:  # noqa: BLE001 - one broken item must not fail the whole feed
            log.warning("Feed %s: skipping a malformed item: %s", base_url, exc)
            continue
        if parsed is None:
            log.debug("Skipping a feed item without id, link or enclosure")
            continue
        items.append(parsed)
    return _split_shared_ids(items, base_url)
