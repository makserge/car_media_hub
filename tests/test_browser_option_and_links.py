"""The browser is opt-in (B1 item 4), the manual-only inbox source (H1) and feed link filtering (H2)."""
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web

from car_media_hub import browser as br
from car_media_hub import feed_web
from car_media_hub.config import Config, load_config
from car_media_hub.pipeline import Controller
from car_media_hub.sources import registry
from car_media_hub.sources.inbox import InboxSource

ROOT = Path(__file__).resolve().parents[1]


def cfg_for(tmp_path, **kw):
    return Config(inbox_dir=tmp_path / "inbox", staging_dir=tmp_path / "st", library_dir=tmp_path / "lib",
                  data_dir=tmp_path / "data", **kw)


# ---------------------------------------------------------------- option gating
def test_default_is_off(tmp_path):
    assert Config(inbox_dir=tmp_path, staging_dir=tmp_path, library_dir=tmp_path).browser_enabled is False
    opts = tmp_path / "options.json"
    opts.write_text(json.dumps({"inbox_dir": str(tmp_path / "in"), "staging_dir": str(tmp_path / "st"),
                                "library_dir": str(tmp_path / "lib")}))
    assert load_config(opts).browser_enabled is False
    opts.write_text(json.dumps({"inbox_dir": str(tmp_path / "in"), "staging_dir": str(tmp_path / "st"),
                                "library_dir": str(tmp_path / "lib"), "browser_enabled": True}))
    assert load_config(opts).browser_enabled is True
    opts.write_text(json.dumps({"inbox_dir": str(tmp_path / "in"), "staging_dir": str(tmp_path / "st"),
                                "library_dir": str(tmp_path / "lib"), "browser_enabled": "yes"}))
    with pytest.raises(ValueError):
        load_config(opts)


def test_disabled_registers_no_viewer_and_no_browser(tmp_path):
    app = web.Application()
    assert br.register_browser_pages(app, SimpleNamespace(cfg=cfg_for(tmp_path))) is None
    paths = {r.canonical for r in app.router.resources()}
    assert paths == {"/api/browser/downloads"}
    assert br.BROWSER_KEY not in app


def test_enabled_registers_the_viewer(tmp_path):
    app = web.Application()
    browser = br.register_browser_pages(app, SimpleNamespace(cfg=cfg_for(tmp_path, browser_enabled=True), sources=[]))
    assert browser is not None and app[br.BROWSER_KEY] is browser
    paths = {r.canonical for r in app.router.resources()}
    assert {"/viewer", "/viewer/ws", "/api/browser/downloads", "/api/browser/downloads/cancel"} <= paths


async def test_nothing_starts_when_disabled(tmp_path):
    browser = br.EmbeddedBrowser(SimpleNamespace(cfg=cfg_for(tmp_path), sources=[]))
    with pytest.raises(br.BrowserError, match="switched off"):
        await browser.ensure_started()
    assert browser._proc is None and browser._xvfb is None and browser._proxy is None


def test_browser_source_only_exists_when_enabled(tmp_path):
    off = registry.builtin_specs(cfg_for(tmp_path))
    assert all(s.get("name") != "browser" for s in off)
    on = registry.builtin_specs(cfg_for(tmp_path, browser_enabled=True))
    spec = next(s for s in on if s.get("name") == "browser")
    assert spec["manual_only"] is True and spec["dir"] == str(tmp_path / "data" / "browser-downloads")
    assert spec["dir"] != str(tmp_path / "inbox")  # never the shared inbox


def test_config_yaml_option_schema_and_translations():
    config = (ROOT / "config.yaml").read_text()
    assert re.search(r"^  browser_enabled: false\s*$", config, re.M)  # default OFF
    assert re.search(r"^  browser_enabled: bool\s*$", config, re.M)
    assert "browser_enabled:" in (ROOT / "translations" / "en.yaml").read_text()


def test_config_yaml_every_option_has_a_schema_entry():
    yaml = pytest.importorskip("yaml")
    data = yaml.safe_load((ROOT / "config.yaml").read_text())
    assert set(data["options"]) <= set(data["schema"])
    for lang in ("en", "de"):
        translated = yaml.safe_load((ROOT / "translations" / f"{lang}.yaml").read_text(encoding="utf-8"))["configuration"]
        assert set(data["options"]) <= set(translated), lang
        assert translated["browser_enabled"]["name"] and translated["browser_enabled"]["description"]


def test_dockerfile_creates_the_user_and_run_sh_protects_the_token():
    docker = (ROOT / "Dockerfile").read_text()
    assert "useradd" in docker and "nologin" in docker and "/data/browser-profile" in docker
    assert "container_environment" in (ROOT / "run.sh").read_text()


# ---------------------------------------------------------------- manual-only source (never auto-approved)
def test_inbox_source_manual_only(tmp_path):
    cfg = cfg_for(tmp_path, browser_enabled=True)
    src = InboxSource.create(cfg, None, None, {"name": "browser", "dir": str(cfg.browser_ready_dir), "manual_only": True})
    assert src.manual_only is True and src.auto_approve is False
    assert InboxSource.create(cfg, None, None, {}).manual_only is False


def test_pending_timeout_approve_skips_manual_only_releases():
    manual = SimpleNamespace(name="browser", manual_only=True)
    rss = SimpleNamespace(name="rss", manual_only=False)
    ctl = SimpleNamespace(sources=[manual, rss])
    release = lambda src: SimpleNamespace(report={"source": src})  # noqa: E731
    assert Controller._manual_only(ctl, release("browser")) is True
    assert Controller._manual_only(ctl, release("rss")) is False
    assert Controller._manual_only(ctl, SimpleNamespace(report={})) is False


# ---------------------------------------------------------------- feed links (H2)
class FakeSource:
    name = "rss"
    type_name = "rss"

    def __init__(self, links):
        self.links = links

    async def feed_items(self):
        return [{"id": str(i), "title": f"t{i}", "link": link, "handled": False} for i, link in enumerate(self.links)]


class FakeRequest:
    pass


async def test_feed_api_drops_unsafe_links():
    links = ["https://example.com/a", "javascript:alert(document.cookie)", "data:text/html,<script>1</script>",
             "  JAVASCRIPT:alert(1)", "vbscript:x", "file:///etc/passwd", "/relative", "http://example.com/x y", ""]
    ctl = SimpleNamespace(sources=[FakeSource(links)], cfg=SimpleNamespace(browser_enabled=False))
    response = await feed_web._feed_api(ctl)
    body = json.loads(response.body)
    assert [it["link"] for it in body["sources"][0]["items"]] == ["https://example.com/a", "", "", "", "", "", "/relative", "", ""]
    assert body["browser"] is False
    ctl.cfg.browser_enabled = True
    assert json.loads((await feed_web._feed_api(ctl)).body)["browser"] is True


def test_rss_source_filters_links_at_the_source():
    from car_media_hub.sources import rss
    import inspect
    assert "safe_http_url" in inspect.getsource(rss.RssSource.feed_items)


def test_page_script_checks_scheme_and_sets_rel():
    from car_media_hub.web import _PAGE
    assert "function safeHref" in _PAGE
    assert "p.protocol === 'http:' || p.protocol === 'https:'" in _PAGE
    assert "ext.href = link" in _PAGE and "ext.href = it.link" not in _PAGE and "a.href = it.link" not in _PAGE
    assert _PAGE.count("noopener noreferrer nofollow") >= 2
    assert "rel = 'noopener noreferrer';" not in _PAGE
