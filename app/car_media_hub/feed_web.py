"""The RSS feed block of the Media page: lists the feed items; the titles open the item pages in the
browser itself (a new tab), so the files are downloaded by that browser into the watched folder."""
from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

from .netguard import safe_http_url

log = logging.getLogger(__name__)


def _rss_sources(controller: Any) -> list[Any]:
    return [s for s in controller.sources if getattr(s, "type_name", "") == "rss" and hasattr(s, "feed_items")]


async def _feed_api(controller: Any) -> web.Response:
    """The feed items of every RSS source: id, title, link and whether the item is marked as done."""
    sources = []
    for source in _rss_sources(controller):
        try:
            items = await source.feed_items()
        except Exception as exc:  # noqa: BLE001 - show the problem instead of an empty list
            log.warning("could not read the feed of %s: %s", source.name, exc)
            sources.append({"source": source.name, "error": str(exc) or type(exc).__name__, "items": []})
            continue
        # defence in depth: the source already filters, but a plugin source may not
        items = [{**item, "link": safe_http_url(item.get("link"))} for item in items]
        sources.append({"source": source.name, "error": "", "items": items})
    return web.json_response({"sources": sources, "browser": bool(getattr(controller.cfg, "browser_enabled", False))})


async def _feed_done(controller: Any, request: web.Request) -> web.Response:
    """Mark one feed item as done (it is then hidden from the list of new items)."""
    try:
        body = await request.json()
        name, item_id = str(body["source"]), str(body["id"])
    except (ValueError, KeyError, TypeError):
        return web.json_response({"error": "source and id are required"}, status=400)
    source = next((s for s in _rss_sources(controller) if s.name == name), None)
    if source is None:
        return web.json_response({"error": "unknown source"}, status=404)
    source.mark_handled(item_id)
    return web.json_response({"ok": True})


def register_feed_pages(app: web.Application, controller: Any) -> None:
    async def feed(request: web.Request) -> web.Response:
        return await _feed_api(controller)

    async def done(request: web.Request) -> web.Response:
        return await _feed_done(controller, request)

    app.router.add_get("/api/feed", feed)
    app.router.add_post("/api/feed/done", done)
