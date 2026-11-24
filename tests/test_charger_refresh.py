"""The automatic charger database: converter, schedule, safe replacement, error messages, Regions page button."""
from __future__ import annotations

import asyncio
import datetime
import json
import socket
import sqlite3

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from car_media_hub import charger_refresh as C
from car_media_hub import resources_api as RA
from car_media_hub.config import load_config
from car_media_hub.regions_web import HEADER, register_region_pages

SAMPLE = (
    "Ladesäulenregister Bundesnetzagentur;;;\n"
    "Letzte Aktualisierung vom: 01.09.2026;;;\n"
    "Allgemeine Informationen;;;\n"
    "Ladeeinrichtungs-ID;Betreiber;Anzeigename (Karte);Status;Art der Ladeeinrichtung;"
    "Anzahl Ladepunkte;Nennleistung Ladeeinrichtung [kW];Breitengrad;Längengrad;"
    "Steckertypen1;Nennleistung Stecker1;Steckertypen2;Nennleistung Stecker2\n"
    "1;OpA;A;In Betrieb;Normal;2;22;48,5;12,1;AC Typ 2 Steckdose;22;AC Typ 2 Steckdose;22\n"
    "2;OpB;B;In Betrieb;Schnell;2;150;49,123456;11,5;"
    "DC Fahrzeugkupplung Typ Combo 2 (CCS);150;AC Typ 2 Steckdose;22\n"
    "2;OpB;B;In Betrieb;Schnell;2;150;49,123456;11,5;DC CHAdeMO;50;;\n"
    "3;OpC;C;In Betrieb;Normal;1;11;;;AC Typ 2 Steckdose;11;;\n"
    "4;OpD;D;In Wartung;Normal;1;11;50,1;10,1;AC Typ 2 Steckdose;11;;\n"
    "5;OpE;E;In Betrieb;Normal;1;11;10,0;10,1;AC Typ 2 Steckdose;11;;\n"
    "6;OpF;F;In Betrieb;Normal;1;50;50,1,2;10,1;DC CHAdeMO;50;;\n"
    "7;OpG;G;In Betrieb;Normal;1;;50,1;10,1;AC Typ 2 Steckdose;;;\n"
    "8;broken\n"
)
NEWER = SAMPLE.replace("01.09.2026", "08.09.2026") + "9;OpH;H;In Betrieb;Normal;1;22;52,5;13,4;AC Typ 2 Steckdose;22;;\n"


def rows(path):
    con = sqlite3.connect(str(path))
    try:
        return {r[0]: r for r in con.execute("SELECT * FROM chargers")}, dict(con.execute("SELECT key, value FROM meta"))
    finally:
        con.close()


def test_converter_matches_the_app_schema_and_rules(tmp_path):
    src = tmp_path / "in.csv"
    src.write_text(SAMPLE, encoding="utf-8-sig")
    out = tmp_path / "out.sqlite"
    n, skips = C.build_from_file(src, out)
    assert n == 2
    for k in ("no_coordinates", "status_not_in_betrieb", "coordinates_outside_germany", "bad_coordinates",
              "no_power", "broken_row", "duplicate_id"):
        assert skips[k] == 1, k
    r, m = rows(out)
    assert r["1"][3:5] == (48.5, 12.1) and r["1"][7] == "Type2:22"
    assert r["2"][7] == "CCS:150;Type2:22" and r["2"][5] == 150.0 and r["2"][8] == C.SOURCE and r["2"][9] == "2026-09-01"
    assert m["row_count"] == "2" and m["licence"] == "CC BY 4.0" and m["schema_version"] == "1"
    con = sqlite3.connect(str(out))
    cols = [x[1] for x in con.execute("PRAGMA table_info(chargers)")]
    idx = {x[1] for x in con.execute("PRAGMA index_list(chargers)")}
    con.close()
    assert cols == ["id", "name", "operator", "lat", "lon", "max_kw", "points", "connectors", "source", "updated"]
    assert {"idx_chargers_lat", "idx_chargers_lon"} <= idx
    assert C.validate_db(out, 2) == 2
    with pytest.raises(C.SourceFormatError):
        C.validate_db(out, 3)


def test_cp1252_file_is_read(tmp_path):
    src = tmp_path / "in.csv"
    src.write_bytes(SAMPLE.encode("cp1252"))
    out = tmp_path / "out.sqlite"
    assert C.build_from_file(src, out)[0] == 2


def test_changed_format_is_a_clear_error(tmp_path):
    src = tmp_path / "in.csv"
    src.write_text("<html>moved</html>\n", encoding="utf-8")
    with pytest.raises(C.SourceFormatError, match="changed its format"):
        C.build_from_file(src, tmp_path / "o.sqlite")
    src.write_text("Ladeeinrichtungs-ID;Betreiber;Status\n1;a;In Betrieb\n", encoding="utf-8")
    with pytest.raises(C.SourceFormatError, match="columns missing"):
        C.build_from_file(src, tmp_path / "o.sqlite")


def test_find_csv_link():
    page = ('<a href="/x/Ladesaeulenregister_BNetzA_2026-08-01.csv">a</a>'
            '<a href="https://data.example/Ladesaeulenregister_BNetzA_2026-09-01.csv?x=1&amp;y=2">b</a>'
            '<a href="/other.csv">c</a>')
    assert C.find_csv_link(page, "https://www.example/page.html") == \
        "https://data.example/Ladesaeulenregister_BNetzA_2026-09-01.csv?x=1&y=2"
    with pytest.raises(C.SourceFormatError, match="charger_source_url"):
        C.find_csv_link("<html></html>", "https://www.example/")


# ------------------------------------------------------------------ the run, against a local server

class Source:
    def __init__(self):
        self.csv = SAMPLE.encode("utf-8-sig")
        self.ctype = "text/csv"
        self.status = 200
        self.hits = 0
        self.etag = '"v1"'
        self.page_mode = "ok"  # ok | stall | chunks | status:<n> | flaky:<n failures first>
        self.page_hits = 0
        self.csv_headers: list = []
        self.have_dates: set = set()  # dates with a file under /reg_<date>.csv
        self.probed: list = []
        self.release = asyncio.Event()

    def app(self):
        app = web.Application()
        link = b'<a href="/Ladesaeulenregister_BNetzA_2026-09-01.csv">csv</a>'

        async def page(request):
            self.page_hits += 1
            mode = self.page_mode
            if mode == "stall":  # headers and a few bytes, then nothing
                resp = web.StreamResponse(headers={"Content-Type": "text/html"})
                await resp.prepare(request)
                await resp.write(b"<html><body>")
                await self.release.wait()
                return resp
            if mode == "chunks":  # the link arrives in the last of several pieces
                resp = web.StreamResponse(headers={"Content-Type": "text/html"})
                await resp.prepare(request)
                for piece in (b"<html>" + b"x" * 100_000, b"y" * 50_000, link):
                    await resp.write(piece)
                    await asyncio.sleep(0.01)
                return resp
            if mode.startswith("status:"):
                return web.Response(status=int(mode.split(":")[1]))
            if mode.startswith("flaky:") and self.page_hits <= int(mode.split(":")[1]):
                return web.Response(status=503)
            return web.Response(body=link, content_type="text/html")

        async def csv_file(request):
            self.hits += 1
            self.csv_headers.append(dict(request.headers))
            if self.status != 200:
                return web.Response(status=self.status)
            if request.headers.get("If-None-Match") == self.etag:
                return web.Response(status=304)
            return web.Response(body=self.csv, content_type=self.ctype.split("/")[0] + "/" + self.ctype.split("/")[1],
                                headers={"ETag": self.etag})

        async def by_date(request):
            d = request.match_info["d"]
            if request.method == "HEAD":
                self.probed.append(d)
            if d not in self.have_dates:
                return web.Response(status=404)
            return await csv_file(request)

        app.router.add_get("/page.html", page)
        app.router.add_get("/Ladesaeulenregister_BNetzA_2026-09-01.csv", csv_file)
        app.router.add_get("/reg_{d}.csv", by_date)
        return app


@pytest.fixture
async def src():
    s = Source()
    server = TestServer(s.app())
    await server.start_server()
    s.base = f"http://127.0.0.1:{server.port}"
    yield s
    s.release.set()
    await server.close()


def make(root, src, **kw):
    kw.setdefault("min_rows", 2)
    kw.setdefault("backoff", (0, 0))
    kw.setdefault("url_pattern", src.base + "/reg_{date}.csv")
    kw.setdefault("page_timeout", C.aiohttp.ClientTimeout(total=10, connect=2, sock_read=0.3))
    return C.ChargerRefresher(root, 7, "", landing_page=src.base + "/page.html", startup_delay=0, **kw)


async def test_refresh_builds_atomically_and_the_manifest_sees_it(tmp_path, src):
    ref = make(tmp_path, src)
    st = await ref.refresh()
    assert st["lastError"] is None and st["rows"] == 2 and st["sourceUpdated"] == "2026-09-01" and not st["running"]
    assert st["lastSuccess"].endswith("Z")
    assert ref.output.exists()
    assert [p.name for p in ref.folder.iterdir()] == [C.OUTPUT_NAME]  # no temp file left
    assert not any((tmp_path / RA.RESOURCES_DIR / "tmp").iterdir())
    doc = RA.build_manifest(tmp_path, {})
    assert [(r["kind"], r["name"]) for r in doc["resources"]] == [("chargers", C.OUTPUT_NAME)]
    # status is persisted and survives a restart
    ref2 = make(tmp_path, src)
    await ref2.start()
    await ref2.stop()
    assert ref2.status()["rows"] == 2 and not ref2.due()


async def test_unchanged_source_keeps_the_file(tmp_path, src):
    ref = make(tmp_path, src)
    await ref.refresh()
    before = ref.output.read_bytes()
    st = await ref.refresh()  # server answers 304 (same ETag)
    assert st["unchanged"] and st["lastError"] is None and ref.output.read_bytes() == before
    src.etag = '"v2"'  # new validators, same register date and count: still kept (no needless new hash)
    st = await ref.refresh()
    assert st["unchanged"] and ref.output.read_bytes() == before


async def test_new_register_replaces_the_old_one(tmp_path, src):
    ref = make(tmp_path, src)
    await ref.refresh()
    src.csv, src.etag = NEWER.encode("utf-8-sig"), '"v2"'
    st = await ref.refresh()
    assert st["rows"] == 3 and not st["unchanged"]
    r, m = rows(ref.output)
    assert set(r) == {"1", "2", "9"} and m["source_updated"] == "2026-09-08"


async def test_failure_keeps_the_old_file_and_says_why(tmp_path, src):
    ref = make(tmp_path, src)
    await ref.refresh()
    good = ref.output.read_bytes()
    # HTML instead of CSV
    src.csv, src.ctype, src.etag = b"<html>" + b"x" * 2000, "text/html", '"v2"'
    st = await ref.refresh()
    assert "web page instead of the CSV" in st["lastError"] and ref.output.read_bytes() == good
    # a CSV whose columns changed
    src.ctype = "text/csv"
    src.csv = ("Ladeeinrichtungs-ID;Betreiber\n" + "1;a\n" * 400).encode()
    st = await ref.refresh()
    assert "format" in st["lastError"] and ref.output.read_bytes() == good
    # far fewer rows than before
    src.csv = SAMPLE.split("\n1;")[0].encode() + b"\n1;OpA;A;In Betrieb;Normal;2;22;48,5;12,1;AC Typ 2 Steckdose;22;;\n" + b";" * 2000
    ref.min_rows = 1
    st = await ref.refresh()
    assert "refused" in st["lastError"] and ref.output.read_bytes() == good
    # server error
    src.status = 500
    st = await ref.refresh()
    assert "HTTP 500" in st["lastError"] and ref.output.read_bytes() == good
    assert st["lastSuccess"] is not None  # the earlier success is still reported next to the error
    assert [p.name for p in ref.folder.iterdir()] == [C.OUTPUT_NAME]
    # recovers: error cleared after a good run
    src.status = 200
    src.csv, src.etag = NEWER.encode("utf-8-sig"), '"v3"'
    ref.min_rows = 2
    st = await ref.refresh()
    assert st["lastError"] is None and st["rows"] == 3


async def test_no_internet_message(tmp_path):
    ref = C.ChargerRefresher(tmp_path, 7, "http://127.0.0.1:9/x.csv", min_rows=2, backoff=(0, 0),
                             session_factory=lambda: C.aiohttp.ClientSession(timeout=C.aiohttp.ClientTimeout(total=1, connect=0.2)))
    st = await ref.refresh()
    err = st["lastError"]
    assert "no connection to the source" in err and "online" in err
    # stage, host, elapsed seconds, bytes so far, attempt and exception class are all named
    assert "CSV download (127.0.0.1) failed after " in err and " s, 0 bytes received (attempt 3 of 3)" in err
    assert "ConnectionTimeoutError" in err or "ClientConnectorError" in err
    assert st["lastStage"] == "CSV download"
    assert not ref.output.exists()
    assert not ref.due()  # the first failure retries after 15 minutes, not at once


async def test_page_without_csv_link(tmp_path, src):
    ref = C.ChargerRefresher(tmp_path, 7, "", landing_page=src.base + "/Ladesaeulenregister_BNetzA_2026-09-01.csv",
                             min_rows=2, probe_days=0, backoff=(0, 0))
    st = await ref.refresh()
    assert "charger_source_url" in st["lastError"] and "landing page (127.0.0.1)" in st["lastError"]
    assert "SourceFormatError" in st["lastError"]


async def test_only_one_build_runs(tmp_path, src):
    gate = asyncio.Event()
    orig = C.build_from_file

    def slow(a, b):
        asyncio.run_coroutine_threadsafe(asyncio.sleep(0), loop).result()
        return orig(a, b)

    loop = asyncio.get_running_loop()
    ref = make(tmp_path, src)
    first = asyncio.create_task(ref.refresh())
    await asyncio.sleep(0)
    assert ref.status()["running"]
    assert ref.trigger() is False
    second = await ref.refresh()  # returns at once with the running status
    assert second["running"]
    await first
    assert src.hits == 1 and ref.trigger() is True
    await ref.stop()
    gate.set()


async def test_loop_does_not_block_and_schedule(tmp_path, src):
    now = [1_000_000.0]
    ref = make(tmp_path, src, clock=lambda: now[0])
    assert ref.due()
    await ref.refresh()
    assert not ref.due()
    now[0] += 6 * 86400
    assert not ref.due()
    now[0] += 2 * 86400
    assert ref.due()
    off = C.ChargerRefresher(tmp_path, 0, "", min_rows=2)
    assert not off.due() and off.status()["enabled"] is False and off.status()["nextRun"] is None
    await off.start()
    assert off._task is None  # 0 = no scheduler
    await off.stop()


async def test_start_runs_the_first_build_by_itself(tmp_path, src):
    ref = make(tmp_path, src)
    await ref.start()
    for _ in range(100):
        if ref.output.exists():
            break
        await asyncio.sleep(0.05)
    await ref.stop()
    assert ref.output.exists()


async def test_leftovers_are_removed_at_start(tmp_path, src):
    ref = make(tmp_path, src)
    ref.folder.mkdir(parents=True)
    (ref.folder / ".build-1.sqlite").write_bytes(b"x")
    (ref.resources / "tmp").mkdir()
    (ref.resources / "tmp" / ".build-1.csv").write_bytes(b"x")
    ref.days = 0
    await ref.start()
    assert not list(ref.folder.iterdir()) and not list((ref.resources / "tmp").iterdir())


# ------------------------------------------------------------------ robustness: stages, retries, fallbacks

async def test_stalled_landing_page_names_the_stage(tmp_path, src):
    src.page_mode = "stall"
    ref = make(tmp_path, src, probe_days=0)
    st = await ref.refresh()
    err = st["lastError"]
    assert src.page_hits == 3  # three attempts, then give up
    assert "landing page (127.0.0.1) failed after " in err and "(attempt 3 of 3)" in err
    assert "12 bytes received" in err  # the part that did arrive
    assert "Timeout" in err and "SocketTimeoutError" in err
    assert st["lastStage"] == "landing page" and src.hits == 0 and not ref.output.exists()


async def test_landing_page_is_read_to_eof(tmp_path, src):
    src.page_mode = "chunks"
    st = await make(tmp_path, src).refresh()
    assert st["lastError"] is None and st["rows"] == 2


async def test_http_steps_are_retried_but_not_a_404(tmp_path, src):
    src.page_mode = "flaky:2"  # 503, 503, then fine
    st = await make(tmp_path, src).refresh()
    assert st["lastError"] is None and src.page_hits == 3
    src.page_mode, src.page_hits = "status:404", 0
    st = await make(tmp_path / "b", src, probe_days=0).refresh()
    assert src.page_hits == 1 and "HTTP 404" in st["lastError"] and "(attempt 1 of 3)" in st["lastError"]
    src.page_mode, src.page_hits = "status:500", 0
    st = await make(tmp_path / "c", src, probe_days=0).refresh()
    assert src.page_hits == 3 and "HTTP 500" in st["lastError"]


async def test_csv_download_is_retried(tmp_path, src):
    src.status = 500
    st = await make(tmp_path, src).refresh()
    assert src.hits == 3 and "CSV download (127.0.0.1)" in st["lastError"] and st["lastStage"] == "CSV download"


async def test_request_headers_and_ipv4(tmp_path, src):
    await make(tmp_path, src).refresh()
    h = src.csv_headers[0]
    assert h["User-Agent"].startswith("Mozilla/5.0") and h["Accept-Encoding"] == "identity" and "text/csv" in h["Accept"]
    ref = C.ChargerRefresher(tmp_path, 7, "")
    async with ref._session_factory() as session:
        assert session.connector._family == socket.AF_INET
        assert session.headers["User-Agent"].startswith("Mozilla/5.0")


async def test_option_url_never_touches_the_landing_page(tmp_path, src):
    src.page_mode = "stall"
    ref = C.ChargerRefresher(tmp_path, 7, src.base + "/Ladesaeulenregister_BNetzA_2026-09-01.csv", min_rows=2,
                             landing_page=src.base + "/page.html", backoff=(0, 0))
    st = await ref.refresh()
    assert st["lastError"] is None and src.page_hits == 0


async def test_remembered_url_is_used_when_the_page_is_down(tmp_path, src):
    ref = make(tmp_path, src, probe_days=0)
    await ref.refresh()
    assert ref._state["url"].endswith("2026-09-01.csv")
    src.page_mode, src.csv, src.etag = "stall", NEWER.encode("utf-8-sig"), '"v2"'
    st = await ref.refresh()
    assert st["lastError"] is None and st["rows"] == 3 and src.page_hits == 1 + 3


async def test_date_pattern_is_the_last_resort_newest_first(tmp_path, src):
    now = 1_800_000_000.0
    today = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).date()

    def day(k):
        return (today - datetime.timedelta(days=k)).isoformat()

    src.have_dates = {day(5), day(9)}
    src.page_mode = "status:404"
    ref = make(tmp_path, src, clock=lambda: now)
    st = await ref.refresh()
    assert st["lastError"] is None and st["rows"] == 2
    assert src.probed == [day(k) for k in range(6)]  # newest first, stopped at the first hit
    assert ref._state["url"].endswith(f"reg_{day(5)}.csv")
    # nothing found in the window: the discovery error plus what the fallbacks said
    src.have_dates = set()
    src.probed.clear()
    st = await make(tmp_path / "b", src, clock=lambda: now, probe_days=4).refresh()
    assert "landing page (127.0.0.1)" in st["lastError"] and "HTTP 404" in st["lastError"]
    assert "fallbacks failed" in st["lastError"] and "no file found for the last 4 days" in st["lastError"]
    assert len(src.probed) == 4


async def test_probe_gives_up_quickly_without_network(tmp_path):
    quick = C.aiohttp.ClientTimeout(total=1, connect=0.2)  # a refused connect takes ~2 s on Windows
    ref = C.ChargerRefresher(tmp_path, 7, "", landing_page="http://127.0.0.1:9/p.html", min_rows=2, backoff=(0, 0),
                             url_pattern="http://127.0.0.1:9/reg_{date}.csv", probe_days=60,
                             page_timeout=quick, probe_timeout=quick)
    st = await ref.refresh()
    assert "landing page (127.0.0.1)" in st["lastError"] and "date-pattern probe" in st["lastError"]


async def test_convert_and_validate_stages_are_named(tmp_path, src):
    src.csv = ("Ladeeinrichtungs-ID;Betreiber\n" + "1;a\n" * 400).encode()
    st = await make(tmp_path, src).refresh()
    assert "convert (local) failed after" in st["lastError"] and "SourceFormatError" in st["lastError"]
    assert st["lastStage"] == "convert"
    src.csv, src.etag = SAMPLE.encode("utf-8-sig"), '"v2"'
    st = await make(tmp_path / "b", src, min_rows=50).refresh()
    assert "validate (local) failed after" in st["lastError"] and st["lastStage"] == "validate"


async def test_retry_schedule_15_minutes_then_6_hours(tmp_path, src):
    now = [1_000_000.0]
    src.status = 500
    ref = make(tmp_path, src, clock=lambda: now[0])
    await ref.refresh()  # first failure
    assert not ref.due()
    now[0] += 14 * 60
    assert not ref.due()
    now[0] += 2 * 60
    assert ref.due() and 30 <= ref._sleep_for() <= C.CHECK_EVERY
    await ref.refresh()  # second failure in a row
    now[0] += 3600
    assert not ref.due()
    now[0] += 5.1 * 3600
    assert ref.due()
    src.status = 200  # a success resets the counter
    await ref.refresh()
    assert ref._state["fail_count"] == 0 and ref.status()["lastStage"] is None


async def test_phase_shows_attempt_and_host(tmp_path, src):
    src.page_mode = "stall"
    ref = make(tmp_path, src, probe_days=0)
    task = asyncio.create_task(ref.refresh())
    await asyncio.sleep(0.15)
    assert ref.status()["phase"].startswith("landing page 127.0.0.1, attempt 1/3")
    await task


# ------------------------------------------------------------------ Regions page API and options

async def page_client(ref):
    app = web.Application()
    register_region_pages(app, None, ref)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def test_button_endpoint_and_status(tmp_path, src):
    ref = make(tmp_path, src)
    client = await page_client(ref)
    try:
        r = await client.post("/regions/api/chargers/refresh")  # no CSRF header
        assert r.status == 400
        r = await client.post("/regions/api/chargers/refresh", headers={"X-Requested-With": HEADER})
        body = await r.json()
        assert r.status == 200 and body["started"] is True
        await ref._run_task
        assert ref.status()["rows"] == 2
        page = await (await client.get("/regions")).text()
        assert 'id="ch-refresh"' in page and "Refresh now" in page
    finally:
        await client.close()
        await ref.stop()


def test_options(tmp_path):
    base = {"inbox_dir": "/share/car_media_hub/inbox", "staging_dir": "/media/.car_media_hub/staging",
            "library_dir": "/media/music"}
    p = tmp_path / "options.json"
    p.write_text(json.dumps(base), encoding="utf-8")
    cfg = load_config(p)
    assert cfg.charger_refresh_days == 7 and cfg.charger_source_url == ""
    p.write_text(json.dumps({**base, "charger_refresh_days": 0, "charger_source_url": "https://x.example/a.csv"}), encoding="utf-8")
    cfg = load_config(p)
    assert cfg.charger_refresh_days == 0 and cfg.charger_source_url == "https://x.example/a.csv"
    for bad in ({"charger_refresh_days": -1}, {"charger_refresh_days": 400}, {"charger_source_url": "ftp://x/a"}):
        p.write_text(json.dumps({**base, **bad}), encoding="utf-8")
        with pytest.raises(ValueError):
            load_config(p)


def test_config_yaml_and_translations_list_the_options():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for rel in ("config.yaml", "translations/en.yaml", "translations/de.yaml"):
        text = (root / rel).read_text(encoding="utf-8")
        assert "charger_refresh_days" in text and "charger_source_url" in text, rel
    assert "charger_refresh_days: int(0,365)" in (root / "config.yaml").read_text(encoding="utf-8")
