"""Builds the charger database for the car app by itself, on a schedule.

Source: the Bundesnetzagentur Ladesaeulenregister CSV (CC BY 4.0). The converter is a port of the app repo's
``tools/build_charger_db.py`` (same rows, same SQLite schema, so ``ChargerStore`` in the app reads it unchanged).

Flow of one run (:meth:`ChargerRefresher.refresh`): find the CSV address (``charger_source_url`` or the newest CSV link
on the Bundesnetzagentur page; if the page cannot be used: the address remembered from the last run, then the dated
file-name pattern for the last 60 days) -> stream the download into a temp file -> convert in a worker thread into a temp
SQLite file -> validate (schema, row count above a sanity minimum, not far smaller than the current file) -> atomic
``os.replace`` onto ``<region_storage_dir>/.resources/chargers/chargers-de.sqlite``. On any failure the old file stays.
The sync API's manifest (``resources_api``) hashes the new file and the app pulls it the next time it checks.
Only one run at a time (a lock); the CPU/IO work never runs on the event loop.
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import csv
import datetime
import json
import logging
import os
import re
import socket
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator
from urllib.parse import urljoin, urlsplit

import aiohttp

from .resources_api import RESOURCES_DIR

log = logging.getLogger(__name__)

SOURCE = "bnetza-ladesaeulenregister"
OUTPUT_NAME = "chargers-de.sqlite"
STATUS_FILE = "charger-status.json"  # in .resources/, not in chargers/ (that folder is served)
LANDING_PAGE = "https://www.bundesnetzagentur.de/DE/Fachthemen/ElektrizitaetundGas/E-Mobilitaet/Ladesaeulenkarte/start.html"
KEEP_STATUS = "In Betrieb"
LAT_RANGE, LON_RANGE = (47.0, 55.5), (5.5, 15.5)
ID_COL = "Ladeeinrichtungs-ID"
MIN_ROWS = 20000  # Germany has well over 100k operating charge points; fewer means a broken file
MIN_SHRINK = 0.6  # a new file below this share of the current one is refused
MAX_DOWNLOAD = 400 * 1024 * 1024
_WRITE_BATCH = 1 << 20  # downloaded bytes are written (in a worker thread) per MiB
MAX_PAGE = 4 * 1024 * 1024
STARTUP_DELAY = 120.0
CHECK_EVERY = 3600.0
RETRY_AFTER = 6 * 3600.0  # after the second and every later failure in a row
FIRST_RETRY_AFTER = 15 * 60.0  # after the first failure (a one-off network hiccup should not cost 6 hours)
ATTEMPTS = 3  # per HTTP step
BACKOFF = (5.0, 20.0)  # pause before attempt 2 and 3
PROBE_DAYS = 60
URL_PATTERN = ("https://data.bundesnetzagentur.de/Bundesnetzagentur/DE/Fachthemen/ElektrizitaetundGas/E-Mobilitaet/"
               "Ladesaeulenregister_BNetzA_{date}.csv")
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0.0.0 Safari/537.36")
PAGE_HEADERS = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "de-DE,de;q=0.9,en;q=0.8"}
CSV_HEADERS = {"Accept": "text/csv,application/octet-stream;q=0.9,*/*;q=0.8",
               "Accept-Language": "de-DE,de;q=0.9,en;q=0.8", "Accept-Encoding": "identity"}
BATCH = 5000



def _flush_and_sync(f: Any) -> None:
    """Flush and fsync an open file (blocking: call it from a worker thread)."""
    f.flush()
    os.fsync(f.fileno())


class ChargerError(Exception):
    """A failed run; the message is shown on the Regions page (the old database is kept)."""


class HttpStatusError(ChargerError):
    """The server answered, but not with 200 (5xx and 429 are retried)."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status

    @property
    def retryable(self) -> bool:
        return self.status >= 500 or self.status == 429


class SourceFormatError(ChargerError):
    """The download is not the register CSV any more (page layout, columns or file type changed)."""


# ------------------------------------------------------------------ converter (port of tools/build_charger_db.py)

def num(s: str | None) -> float | None:
    s = (s or "").strip().replace(",", ".")
    if not s:
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return v if v == v and abs(v) != float("inf") else None


def norm_plug(name: str) -> str:
    n = name.strip()
    low = n.lower()
    if "combo" in low or "ccs" in low:
        return "CCS"
    if "chademo" in low:
        return "CHAdeMO"
    if "megawatt" in low or "mcs" in low:
        return "MCS"
    if "typ 2" in low or "type 2" in low:
        return "Type2"
    if "typ 1" in low or "type 1" in low:
        return "Type1"
    if "schuko" in low:
        return "Schuko"
    if "cee" in low:
        return "CEE"
    return re.sub(r"\s+", "", re.sub(r"^(AC|DC)\s+", "", n))[:12] or "Other"


def fmt_kw(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else ("%.1f" % v).rstrip("0").rstrip(".")


def parse_date(text: str) -> str | None:
    m = re.search(r"Letzte Aktualisierung[^0-9]*(\d{2})\.(\d{2})\.(\d{4})", text)
    return "%s-%s-%s" % (m.group(3), m.group(2), m.group(1)) if m else None


def convert_rows(lines: Iterable[str]) -> Iterator[tuple[str, Any]]:
    """Yield ('meta', updated_iso) first, then ('status', s), ('skip', reason), ('row', dict)."""
    reader = csv.reader(lines, delimiter=";")
    updated, header = None, None
    for n, row in enumerate(reader):
        if row and row[0].strip().lstrip("﻿") == ID_COL:
            header = [c.strip() for c in row]
            break
        updated = updated or parse_date(";".join(row))
        if n > 200:
            break
    if header is None:
        raise SourceFormatError(
            f"the file has no '{ID_COL}' header row in its first lines: the source changed its format "
            "(or this is not the register CSV)")
    ix = {n: i for i, n in enumerate(header)}
    need = [ID_COL, "Betreiber", "Status", "Breitengrad", "Längengrad", "Nennleistung Ladeeinrichtung [kW]"]
    missing = [c for c in need if c not in ix]
    if missing:
        raise SourceFormatError(f"the source changed its format: columns missing: {', '.join(missing)}")
    last_needed = max(ix[c] for c in need)
    yield ("meta", updated)

    def get(row: list[str], name: str) -> str:
        i = ix.get(name)
        return row[i].strip() if i is not None and i < len(row) else ""

    for row in reader:
        if not row or not any(c.strip() for c in row):
            continue
        if len(row) <= last_needed:
            yield ("skip", "broken_row")
            continue
        status = get(row, "Status")
        yield ("status", status)
        if status != KEEP_STATUS:
            yield ("skip", "status_not_in_betrieb")
            continue
        cid = get(row, ID_COL)
        if not cid:
            yield ("skip", "no_id")
            continue
        lat_s, lon_s = get(row, "Breitengrad"), get(row, "Längengrad")
        if not lat_s or not lon_s:
            yield ("skip", "no_coordinates")
            continue
        lat, lon = num(lat_s), num(lon_s)
        if lat is None or lon is None:
            yield ("skip", "bad_coordinates")
            continue
        if not (LAT_RANGE[0] <= lat <= LAT_RANGE[1] and LON_RANGE[0] <= lon <= LON_RANGE[1]):
            yield ("skip", "coordinates_outside_germany")
            continue
        station_kw = num(get(row, "Nennleistung Ladeeinrichtung [kW]"))
        conn: "collections.OrderedDict[str, float | None]" = collections.OrderedDict()
        plug_kws: list[float] = []
        for k in range(1, 7):
            types = get(row, "Steckertypen%d" % k)
            kw = num(get(row, "Nennleistung Stecker%d" % k))
            if kw is not None:
                plug_kws.append(kw)
            for t in types.split(";"):
                if t.strip():
                    p = norm_plug(t)
                    prev = conn.get(p)
                    if p not in conn or (kw is not None and (prev is None or prev < kw)):
                        conn[p] = kw if kw is not None else prev
        max_kw = max(plug_kws) if plug_kws else station_kw
        if max_kw is None or max_kw <= 0:
            yield ("skip", "no_power")
            continue
        connectors = ";".join("%s:%s" % (p, fmt_kw(kw)) if kw is not None else p for p, kw in conn.items())
        pts = num(get(row, "Anzahl Ladepunkte"))
        yield ("row", dict(
            id=cid, name=get(row, "Anzeigename (Karte)") or get(row, "Betreiber"), operator=get(row, "Betreiber"),
            lat=lat, lon=lon, max_kw=max_kw, points=int(pts) if pts is not None else len(conn),
            connectors=connectors))


SCHEMA = (
    "CREATE TABLE chargers(id TEXT PRIMARY KEY, name TEXT, operator TEXT, lat REAL, lon REAL,"
    " max_kw REAL, points INTEGER, connectors TEXT, source TEXT, updated TEXT);"
    "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);")


def build(lines: Iterable[str], db_path: Path | str) -> tuple[int, collections.Counter, collections.Counter]:
    """Convert the CSV lines into a new SQLite file (replaces ``db_path``). Rows are inserted in batches."""
    db_path = str(db_path)
    if os.path.exists(db_path):
        os.remove(db_path)
    con = sqlite3.connect(db_path)
    try:
        con.execute("PRAGMA journal_mode=OFF")
        con.execute("PRAGMA synchronous=OFF")
        con.executescript(SCHEMA)
        skips: collections.Counter = collections.Counter()
        statuses: collections.Counter = collections.Counter()
        seen: set[str] = set()
        batch: list[tuple] = []
        n = 0
        stream = convert_rows(lines)
        updated = next(stream)[1] or datetime.date.today().isoformat()
        for ev in stream:
            if ev[0] == "status":
                statuses[ev[1]] += 1
            elif ev[0] == "skip":
                skips[ev[1]] += 1
            elif ev[0] == "row":
                d = ev[1]
                if d["id"] in seen:
                    skips["duplicate_id"] += 1
                    continue
                seen.add(d["id"])
                batch.append((d["id"], d["name"], d["operator"], d["lat"], d["lon"], d["max_kw"],
                              d["points"], d["connectors"], SOURCE, updated))
                if len(batch) >= BATCH:
                    con.executemany("INSERT INTO chargers VALUES(?,?,?,?,?,?,?,?,?,?)", batch)
                    n += len(batch)
                    batch.clear()
        if batch:
            con.executemany("INSERT INTO chargers VALUES(?,?,?,?,?,?,?,?,?,?)", batch)
            n += len(batch)
        con.execute("CREATE INDEX idx_chargers_lat ON chargers(lat)")
        con.execute("CREATE INDEX idx_chargers_lon ON chargers(lon)")
        built = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        meta = [("schema_version", "1"), ("source", SOURCE), ("licence", "CC BY 4.0"),
                ("attribution", "Bundesnetzagentur.de"), ("built_at", built),
                ("row_count", str(n)), ("source_updated", updated)]
        meta += [("skipped_" + k, str(v)) for k, v in sorted(skips.items())]
        con.executemany("INSERT INTO meta VALUES(?,?)", meta)
        con.commit()
    finally:
        con.close()
    return n, skips, statuses


def _open_text(path: Path) -> Any:
    """The CSV as text: UTF-8 (with or without BOM), else Windows-1252 (an older export)."""
    with open(path, "rb") as f:
        head = f.read(1 << 16)
    enc = "utf-8-sig"
    try:
        head.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        if exc.start < len(head) - 4:  # not just a cut multi-byte character at the end
            enc = "cp1252"
    return open(path, encoding=enc, errors="replace", newline="")


def build_from_file(csv_path: Path, db_path: Path) -> tuple[int, dict[str, Any]]:
    with _open_text(csv_path) as f:
        n, skips, _ = build(f, db_path)
    return n, dict(skips)


def _ro(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True)


def read_meta(db_path: Path) -> dict[str, str]:
    """The meta table of a charger database; {} when unreadable."""
    try:
        con = _ro(db_path)
        try:
            return {str(k): str(v) for k, v in con.execute("SELECT key, value FROM meta")}
        finally:
            con.close()
    except (sqlite3.Error, OSError):
        return {}


def validate_db(db_path: Path, min_rows: int) -> int:
    """Check schema and content of a freshly built file; returns the row count or raises ChargerError."""
    try:
        con = _ro(db_path)
        try:
            cols = {r[1] for r in con.execute("PRAGMA table_info(chargers)")}
            if not {"id", "lat", "lon", "max_kw", "points", "connectors", "name", "operator"} <= cols:
                raise ChargerError("the built database has the wrong schema")
            if con.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ChargerError("the built database failed the integrity check")
            n = con.execute("SELECT COUNT(*) FROM chargers").fetchone()[0]
            bad = con.execute("SELECT COUNT(*) FROM chargers WHERE lat IS NULL OR lon IS NULL").fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error as exc:
        raise ChargerError(f"the built database is unreadable: {exc}") from exc
    if bad:
        raise ChargerError("the built database has rows without a position")
    if n < min_rows:
        raise SourceFormatError(
            f"only {n} usable charge points found (expected at least {min_rows}): the source probably changed "
            "its format; the previous database was kept")
    return int(n)



# ------------------------------------------------------------------ download

_CSV_LINK = re.compile(r"href\s*=\s*[\"']([^\"'<>]*Ladesaeulenregister[^\"'<>]*?\.csv(?:\?[^\"'<>]*)?)[\"']", re.I)


def find_csv_link(page: str, base: str) -> str:
    """The newest register CSV link on the Bundesnetzagentur page (links carry the date in the file name)."""
    links = sorted({urljoin(base, m.replace("&amp;", "&")) for m in _CSV_LINK.findall(page)})
    links = [u for u in links if urlsplit(u).scheme in ("http", "https")]
    links.sort(key=lambda u: ((re.findall(r"\d{4}-\d{2}-\d{2}", urlsplit(u).path) or [""])[-1], u))
    if not links:
        raise SourceFormatError(
            "no register CSV link found on the Bundesnetzagentur page (its layout changed); "
            "set the add-on option charger_source_url to the CSV address")
    return links[-1]


class _Stage:
    """One step of a run (landing page, CSV download, convert, validate): what the error message reports."""

    def __init__(self, name: str, url: str | None, attempts: int = ATTEMPTS):
        self.name = name
        self.host = (urlsplit(url).hostname if url else None) or "local"
        self.attempts = attempts
        self.attempt = 1
        self.got = 0  # bytes received in the current attempt
        self.t0 = time.monotonic()

    def failure(self, exc: BaseException, net: bool | None = None) -> ChargerError:
        """The message for the Regions page: stage, host, seconds, bytes so far, attempt, exception class."""
        if net is None:
            net = not isinstance(exc, ChargerError)
        detail = str(exc).strip()
        tries = f" (attempt {self.attempt} of {self.attempts})" if self.attempts > 1 else ""
        msg = (f"{self.name} ({self.host}) failed after {time.monotonic() - self.t0:.1f} s, "
               f"{self.got} bytes received{tries}: {type(exc).__name__}{': ' + detail if detail else ''}")
        if net:
            msg = "no connection to the source (is the add-on online?) - " + msg
        out: ChargerError = SourceFormatError(msg) if isinstance(exc, SourceFormatError) else ChargerError(msg)
        out.stage = self.name  # type: ignore[attr-defined]
        return out


def _is_csv_response(resp: aiohttp.ClientResponse) -> bool:
    return "html" not in resp.headers.get("Content-Type", "").lower()


# ------------------------------------------------------------------ status + scheduling

def _iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ChargerRefresher:
    def __init__(self, root: Path, refresh_days: int = 7, source_url: str = "", *, min_rows: int = MIN_ROWS,
                 landing_page: str = LANDING_PAGE, clock: Callable[[], float] = time.time,
                 startup_delay: float = STARTUP_DELAY, session_factory: Callable[[], aiohttp.ClientSession] | None = None,
                 page_timeout: aiohttp.ClientTimeout | None = None, probe_timeout: aiohttp.ClientTimeout | None = None,
                 backoff: tuple[float, ...] = BACKOFF, url_pattern: str = URL_PATTERN, probe_days: int = PROBE_DAYS):
        self.root = Path(root)
        self.days = refresh_days
        self.source_url = source_url
        self.min_rows = min_rows
        self.landing_page = landing_page
        self.page_timeout = page_timeout or aiohttp.ClientTimeout(total=90, connect=20, sock_read=30)
        self.probe_timeout = probe_timeout or aiohttp.ClientTimeout(total=15, connect=5, sock_read=8)
        self.backoff = backoff
        self.url_pattern = url_pattern
        self.probe_days = probe_days
        self._clock = clock
        self._startup_delay = startup_delay
        # IPv4 only: Docker hosts often have a broken IPv6 route that stalls instead of failing over
        self._session_factory = session_factory or (lambda: aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=3600, connect=30, sock_read=90),
            headers={"User-Agent": BROWSER_UA}, connector=aiohttp.TCPConnector(family=socket.AF_INET)))
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._run_task: asyncio.Task[Any] | None = None
        self._phase = ""
        self._state: dict[str, Any] = {}
        self._pending: dict[str, str] = {}

    # paths
    @property
    def resources(self) -> Path:
        return self.root / RESOURCES_DIR

    @property
    def folder(self) -> Path:
        return self.resources / "chargers"

    @property
    def output(self) -> Path:
        return self.folder / OUTPUT_NAME

    def _load_state(self) -> None:
        try:
            data = json.loads((self.resources / STATUS_FILE).read_text(encoding="utf-8"))
            self._state = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            self._state = {}

    def _save_state(self) -> None:
        try:
            self.resources.mkdir(parents=True, exist_ok=True)
            tmp = self.resources / (STATUS_FILE + ".tmp")
            tmp.write_text(json.dumps(self._state), encoding="utf-8")
            os.replace(tmp, self.resources / STATUS_FILE)
        except OSError:
            log.warning("could not save the charger refresh status", exc_info=True)

    def status(self) -> dict[str, Any]:
        s = self._state
        return {
            "enabled": self.days > 0, "refreshDays": self.days, "running": self._lock.locked(), "phase": self._phase,
            "lastSuccess": s.get("last_success"), "rows": s.get("rows"), "sourceUpdated": s.get("source_updated"),
            "lastAttempt": s.get("last_attempt"), "lastError": s.get("last_error"), "lastStage": s.get("last_stage"),
            "unchanged": bool(s.get("unchanged")), "nextRun": self._next_run_iso(),
        }

    def _retry_after(self) -> float:
        """15 minutes after the first failure, 6 hours after later ones in a row."""
        n = self._state.get("fail_count")
        return FIRST_RETRY_AFTER if not isinstance(n, int) or n <= 1 else RETRY_AFTER

    def _next_run_iso(self) -> str | None:
        if self.days <= 0:
            return None
        base = self._state.get("last_success_ts")
        due = (base + self.days * 86400) if isinstance(base, (int, float)) else self._clock()
        att = self._state.get("last_attempt_ts")
        if self._state.get("last_error") and isinstance(att, (int, float)):
            due = max(due, att + self._retry_after())
        return _iso(due)

    def due(self) -> bool:
        if self.days <= 0:
            return False
        now = self._clock()
        last = self._state.get("last_success_ts")
        if isinstance(last, (int, float)) and last <= now < last + self.days * 86400 and self.output.exists():
            return False
        att = self._state.get("last_attempt_ts")
        if self._state.get("last_error") and isinstance(att, (int, float)) and att <= now < att + self._retry_after():
            return False
        return True

    def _sleep_for(self) -> float:
        """Hourly check, but wake up when a (15 minute) retry is due sooner."""
        att = self._state.get("last_attempt_ts")
        if self._state.get("last_error") and isinstance(att, (int, float)):
            return min(CHECK_EVERY, max(30.0, att + self._retry_after() - self._clock()))
        return CHECK_EVERY

    # lifecycle
    async def start(self) -> None:
        await asyncio.to_thread(self._cleanup_leftovers)
        await asyncio.to_thread(self._load_state)
        if self.days > 0 and self._task is None:
            self._task = asyncio.create_task(self._loop(), name="charger-refresh")

    async def stop(self) -> None:
        tasks = [t for t in (self._task, self._run_task) if t is not None]
        for t in tasks:
            if not t.done():
                t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._task = self._run_task = None

    def _cleanup_leftovers(self) -> None:
        for folder in (self.resources / "tmp", self.folder):
            try:
                for e in os.scandir(folder):
                    if e.is_file(follow_symlinks=False) and e.name.startswith(".build-"):
                        with contextlib.suppress(OSError):
                            os.remove(e.path)
            except OSError:
                pass

    async def _loop(self) -> None:
        await asyncio.sleep(self._startup_delay)
        while True:
            try:
                if self.due():
                    await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - never end the schedule
                log.exception("charger refresh loop failed")
            await asyncio.sleep(self._sleep_for())

    def trigger(self) -> bool:
        """Start a run in the background (the 'Refresh now' button); False when one is already running."""
        if self._lock.locked():
            return False
        self._run_task = asyncio.create_task(self.refresh(), name="charger-refresh-now")
        return True

    # the run
    async def refresh(self) -> dict[str, Any]:
        if self._lock.locked():
            return self.status()
        async with self._lock:
            if not self._state:
                await asyncio.to_thread(self._load_state)
            self._state["last_attempt_ts"] = self._clock()
            self._state["last_attempt"] = _iso(self._clock())
            tmp_csv = self.resources / "tmp" / f".build-{os.getpid()}.csv"
            tmp_db = self.folder / f".build-{os.getpid()}.sqlite"
            try:
                result = await self._run(tmp_csv, tmp_db)
            except asyncio.CancelledError:
                raise
            except ChargerError as exc:
                self._fail(str(exc), getattr(exc, "stage", None))
            except Exception as exc:  # noqa: BLE001
                log.exception("charger refresh failed")
                self._fail(f"unexpected error: {type(exc).__name__}: {exc}", None)
            else:
                self._state.update(result)
                self._state["last_error"] = None
                self._state["last_stage"] = None
                self._state["fail_count"] = 0
                self._state["last_success_ts"] = self._clock()
                self._state["last_success"] = _iso(self._clock())
                log.info("charger database: %s", result.get("message"))
            finally:
                self._phase = ""
                for p in (tmp_csv, tmp_db):
                    with contextlib.suppress(OSError):
                        os.remove(p)
                await asyncio.shield(asyncio.to_thread(self._save_state))
        return self.status()

    def _fail(self, message: str, stage: str | None) -> None:
        log.warning("charger refresh failed: %s", message)
        self._state["last_error"] = message
        self._state["last_stage"] = stage
        n = self._state.get("fail_count")
        self._state["fail_count"] = (n if isinstance(n, int) else 0) + 1

    async def _local(self, name: str, fn: Callable[..., Any], *args: Any) -> Any:
        """A worker-thread step (convert, validate); failures are reported with the stage like network ones."""
        self._phase = name
        st = _Stage(name, None, 1)
        try:
            return await asyncio.to_thread(fn, *args)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise st.failure(exc, net=False) from exc

    async def _run(self, tmp_csv: Path, tmp_db: Path) -> dict[str, Any]:
        await asyncio.to_thread(lambda: (tmp_csv.parent.mkdir(parents=True, exist_ok=True),
                                         self.folder.mkdir(parents=True, exist_ok=True)))
        self._pending = {}
        async with self._session_factory() as session:
            changed = await self._acquire(session, tmp_csv)
        if not changed:
            return {"unchanged": True, "message": "source unchanged since the last download"}
        await self._local("convert", build_from_file, tmp_csv, tmp_db)
        with contextlib.suppress(OSError):
            os.remove(tmp_csv)  # free the space before validating
        n = await self._local("validate", validate_db, tmp_db, self.min_rows)
        old = await asyncio.to_thread(read_meta, self.output)
        old_n = int(old["row_count"]) if old.get("row_count", "").isdigit() else 0
        new_meta = await asyncio.to_thread(read_meta, tmp_db)
        if old_n and n < old_n * MIN_SHRINK:
            raise _Stage("validate", None, 1).failure(SourceFormatError(
                f"the new file has {n} charge points, the current one {old_n}: "
                "refused as probably broken; the previous database was kept"))
        if old_n == n and old.get("source_updated") == new_meta.get("source_updated") and self.output.exists():
            return {**self._pending, "unchanged": True, "rows": n, "source_updated": new_meta.get("source_updated"),
                    "message": "source has the same date as the current database; kept it"}
        await asyncio.to_thread(self._replace, tmp_db)
        return {**self._pending, "unchanged": False, "rows": n, "source_updated": new_meta.get("source_updated"),
                "message": f"{n} charge points (source dated {new_meta.get('source_updated')})"}

    def _replace(self, tmp_db: Path) -> None:
        with open(tmp_db, "rb+") as f:
            os.fsync(f.fileno())
        os.replace(tmp_db, self.output)  # atomic: the manifest sees the old or the new file, never half of one

    # ---- HTTP steps
    def _set_phase(self, st: _Stage) -> None:
        self._phase = (f"{st.name} {st.host}, attempt {st.attempt}/{st.attempts}"
                       + (f", {st.got / 1048576:.1f} MB" if st.got else ""))

    async def _retrying(self, st: _Stage, op: Callable[[_Stage], Any]) -> Any:
        """Run one HTTP step up to ``st.attempts`` times (pauses from ``self.backoff``); errors name the stage."""
        last: BaseException | None = None
        for attempt in range(1, st.attempts + 1):
            st.attempt, st.got = attempt, 0
            self._set_phase(st)
            try:
                return await op(st)
            except asyncio.CancelledError:
                raise
            except HttpStatusError as exc:
                if not exc.retryable:
                    raise st.failure(exc) from exc
                last = exc
            except ChargerError as exc:
                raise st.failure(exc) from exc
            except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as exc:
                last = exc
            log.warning("charger refresh: %s", st.failure(last))
            if attempt < st.attempts:
                await asyncio.sleep(self.backoff[min(attempt - 1, len(self.backoff) - 1)] if self.backoff else 0)
        assert last is not None
        raise st.failure(last) from last

    async def _acquire(self, session: aiohttp.ClientSession, tmp_csv: Path) -> bool:
        """Find the CSV address and download it. False = unchanged (304)."""
        if self.source_url:
            return await self._download(session, self.source_url, tmp_csv)
        try:
            url = await self._discover(session)
        except ChargerError as first:
            discovery_error = first
        else:
            return await self._download(session, url, tmp_csv)
        # the page is unusable: the address of the last run, then the dated file-name pattern
        notes: list[str] = []
        stored = self._state.get("url")
        tried: set[str] = set()
        if isinstance(stored, str) and stored:
            tried.add(stored)
            log.warning("charger refresh: landing page unusable, trying the address from the last run: %s", stored)
            try:
                return await self._download(session, stored, tmp_csv)
            except ChargerError as exc:
                notes.append(f"remembered address: {exc}")
        found, note = await self._probe(session, tried)
        if found:
            try:
                return await self._download(session, found, tmp_csv)
            except ChargerError as exc:
                notes.append(f"date-pattern address: {exc}")
        elif note:
            notes.append(note)
        out = type(discovery_error)(f"{discovery_error} | fallbacks failed: {' | '.join(notes)}")
        out.stage = getattr(discovery_error, "stage", None)  # type: ignore[attr-defined]
        raise out from discovery_error

    async def _discover(self, session: aiohttp.ClientSession) -> str:
        st = _Stage("landing page", self.landing_page)

        async def op(st: _Stage) -> bytes:
            async with session.get(self.landing_page, headers=PAGE_HEADERS, timeout=self.page_timeout) as resp:
                if resp.status != 200:
                    raise HttpStatusError(f"the Bundesnetzagentur page answered HTTP {resp.status}", resp.status)
                buf = bytearray()
                while True:  # read() may return partial data: until EOF
                    chunk = await resp.content.read(1 << 16)
                    if not chunk:
                        break
                    buf += chunk
                    st.got = len(buf)
                    if st.got > MAX_PAGE:
                        raise SourceFormatError("the page is larger than 4 MB: the source changed")
            return bytes(buf)

        raw = await self._retrying(st, op)
        try:
            return find_csv_link(raw.decode("utf-8", errors="replace"), self.landing_page)
        except ChargerError as exc:
            raise st.failure(exc) from exc

    async def _probe(self, session: aiohttp.ClientSession, skip: set[str]) -> tuple[str | None, str]:
        """Last resort: the CSV file name carries its date; try the last ``probe_days`` days, newest first."""
        if self.probe_days <= 0:
            return None, ""
        today = datetime.datetime.fromtimestamp(self._clock(), datetime.timezone.utc).date()
        t0 = time.monotonic()
        net_fails, last_exc = 0, None
        for k in range(self.probe_days):
            day = (today - datetime.timedelta(days=k)).isoformat()
            url = self.url_pattern.replace("{date}", day)
            if url in skip:
                continue
            self._phase = f"looking for the file by date ({day})"
            try:
                async with session.head(url, headers=CSV_HEADERS, timeout=self.probe_timeout) as resp:
                    status, ok = resp.status, _is_csv_response(resp)
                if status in (403, 405, 501):  # HEAD refused: a GET that is dropped unread
                    async with session.get(url, headers={**CSV_HEADERS, "Range": "bytes=0-0"},
                                           timeout=self.probe_timeout) as resp:
                        status, ok = resp.status, _is_csv_response(resp)
                net_fails = 0
                if status in (200, 206) and ok:
                    log.warning("charger refresh: found the register by its date pattern: %s", url)
                    return url, ""
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as exc:
                last_exc = exc
                net_fails += 1
                if net_fails >= 3:  # no network at all: do not wait for 60 timeouts
                    break
        why = (f"{type(last_exc).__name__}: {last_exc}" if last_exc and net_fails >= 3
               else f"no file found for the last {self.probe_days} days")
        return None, f"date-pattern probe ({urlsplit(self.url_pattern).hostname}) after {time.monotonic() - t0:.1f} s: {why}"

    async def _download(self, session: aiohttp.ClientSession, url: str, dest: Path) -> bool:
        """Stream ``url`` into ``dest``. False when the server says it is unchanged (304)."""
        headers: dict[str, str] = dict(CSV_HEADERS)
        if self._state.get("url") == url and self.output.exists():
            if self._state.get("etag"):
                headers["If-None-Match"] = self._state["etag"]
            if self._state.get("last_modified"):
                headers["If-Modified-Since"] = self._state["last_modified"]
        st = _Stage("CSV download", url)

        async def op(st: _Stage) -> bool:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 304:
                    return False
                if resp.status != 200:
                    raise HttpStatusError(f"the source answered HTTP {resp.status} for {url}", resp.status)
                if not _is_csv_response(resp):
                    raise SourceFormatError(f"the source returned a web page instead of the CSV ({url}); it moved or changed")
                # every write, the flush and the fsync run in a worker thread: the event loop (and with it every sync
                # stream) never waits for the disk
                f = await asyncio.to_thread(open, dest, "wb")
                try:
                    pending = bytearray()
                    async for chunk in resp.content.iter_chunked(1 << 17):
                        st.got += len(chunk)
                        if st.got > MAX_DOWNLOAD:
                            raise SourceFormatError("the download is larger than 400 MB: the source changed")
                        pending += chunk
                        if len(pending) >= _WRITE_BATCH:
                            await asyncio.to_thread(f.write, bytes(pending))
                            pending.clear()
                        self._set_phase(st)
                    if pending:
                        await asyncio.to_thread(f.write, bytes(pending))
                    await asyncio.to_thread(_flush_and_sync, f)
                finally:
                    await asyncio.to_thread(f.close)
                if st.got < 200:
                    raise SourceFormatError("the download is empty or tiny: the source changed")
                # remembered only when the whole run succeeds (else the next run would get a 304 for a file we never used)
                self._pending = {"url": url, "etag": resp.headers.get("ETag", ""),
                                 "last_modified": resp.headers.get("Last-Modified", "")}
                return True

        return bool(await self._retrying(st, op))
