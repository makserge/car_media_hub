"""Session expiry behind Home Assistant ingress (2026-11-23): every page carries the 401 handler, relative URLs only."""
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web

from car_media_hub import browser as br
from car_media_hub import regions_web, trips_web, web as webmod
from car_media_hub.config import Config
from car_media_hub.session_guard import SESSION_GUARD_JS, SESSION_GUARD_TAG

ROOT = Path(__file__).resolve().parents[1]


def test_snippet_detects_401_and_offers_reload():
    js = SESSION_GUARD_JS
    assert "401" in js and "Session expired - reload this page" in js
    assert "window.top" in js and "location.reload()" in js and "Reload" in js
    assert "window.fetch" in js and "XMLHttpRequest" in js and "probe" in js
    assert "cmhSession" in js and "if(window.cmhSession)return" in js  # installed once per window


def test_snippet_uses_no_absolute_paths():
    # no request of its own besides the relative one given to probe(); no absolute URL or path literal
    assert not re.search(r"""['"](https?:)?//""", SESSION_GUARD_JS)
    assert not re.search(r"""fetch\(\s*['"]/""", SESSION_GUARD_JS)
    assert "</script" not in SESSION_GUARD_JS.lower()


def test_media_page_contains_the_handler_before_its_own_script():
    page = webmod.render_index([], [])
    assert SESSION_GUARD_TAG in page and "@@GUARD@@" not in page
    assert page.index("cmhSession") < page.index("var FAST = 1500")


def test_trips_and_regions_pages_contain_the_handler():
    page = trips_web._page("T", "./", "trips", "<p>x</p>", scripts="<script>1</script>")
    assert SESSION_GUARD_TAG in page and page.index("cmhSession") < page.index("<script>1</script>")
    assert SESSION_GUARD_TAG in page.split("</head>")[0]
    # the Regions page is built with the same _page()
    assert "_page(" in (ROOT / "app/car_media_hub/regions_web.py").read_text(encoding="utf-8")


def test_viewer_page_has_the_handler_and_checks_a_close():
    assert SESSION_GUARD_TAG in br._PAGE and "@@GUARD@@" not in br._PAGE
    assert br._PAGE.index("cmhSession") < br._PAGE.index("function connect()")
    assert "cmhSession.probe('api/browser/downloads')" in br._PAGE  # relative
    assert "cmhSession.dead" in br._PAGE  # no reconnect after a 401
    assert "retryMs" in br._PAGE  # back-off instead of a fixed 2 s


def test_viewer_probe_route_exists_and_is_relative(tmp_path):
    app = web.Application()
    cfg = Config(inbox_dir=tmp_path, staging_dir=tmp_path, library_dir=tmp_path, browser_enabled=True)
    br.register_browser_pages(app, SimpleNamespace(cfg=cfg))
    assert "/api/browser/downloads" in {r.canonical for r in app.router.resources()}


@pytest.mark.parametrize("src", ["web.py", "browser.py", "regions_web.py", "trips_web.py"])
def test_page_scripts_have_no_absolute_request_urls(src):
    text = (ROOT / "app/car_media_hub" / src).read_text(encoding="utf-8")
    bad = re.findall(r"""(?:fetch|\.open)\(\s*(?:'[A-Z]+'\s*,\s*)?['"]/(?!/)[^'"]*""", text)
    assert not bad, bad
    assert not re.search(r"""(?:href|src|action)=["']/(?!/)""", text)


async def test_pages_are_served_with_the_handler(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    app = web.Application()
    trips_html = trips_web._page("T", "./", "trips", "")
    async def h(request):
        return web.Response(text=trips_html, content_type="text/html")
    app.router.add_get("/", h)
    async with TestClient(TestServer(app)) as c:
        r = await c.get("/")
        assert "Session expired - reload this page" in await r.text()
