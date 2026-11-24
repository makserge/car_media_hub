"""Trips pages of the ingress web UI: list with summary cards, trip detail with a map, CSV export, delete.

Pages read the trips straight from the database (no HTTP hop to the sync API). Every URL in the
pages is relative (never starts with "/"): behind Home Assistant ingress the UI lives at
/api/hassio_ingress/<token>/ and absolute paths would leave it. Trip text (vehicle, source, ids)
comes from the car app, so it is HTML-escaped on the server and never put into inline script.
"""
from __future__ import annotations

import asyncio
import csv
import html
import io
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import aiohttp
from aiohttp import web

from car_media_hub.session_guard import SESSION_GUARD_TAG
from car_media_hub.trips import compute_stats

log = logging.getLogger(__name__)

PAGE_SIZE = 50
CHART_TRIPS = 20
STATIC_DIR = Path(__file__).parent / "static"
_MONTH = re.compile(r"(\d{4})-(0[1-9]|1[0-2])\Z")
# sources that are not a real car: marked with a small badge
_TEST_SOURCE = re.compile(r"e2e|test|mock|demo|fake|sim|dummy|sample", re.IGNORECASE)
_CLOCK = web.AppKey("trips_web_clock", object)
_GET_DB = web.AppKey("trips_web_db", object)

# map tiles: fetched once by the add-on (identifying User-Agent, OSM tile policy), cached on disk
TILE_UPSTREAM = os.environ.get("TILE_UPSTREAM", "https://tile.openstreetmap.de/{z}/{x}/{y}.png")
TILE_USER_AGENT = "CarMediaAndTripsHub/0.3 (Home Assistant add-on; trip map tiles)"
_tile_gates: dict[Any, asyncio.Semaphore] = {}


def tile_cache_dir() -> Path:
    return Path(os.environ.get("TILE_CACHE_DIR", "/data/tile_cache"))


# ---------------------------------------------------------------- formatting


def _e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _num(value: float | None, digits: int = 1) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def duration_text(seconds: int | float | None) -> str:
    if seconds is None:
        return "-"
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes = rest // 60
    if hours:
        return f"{hours} h {minutes:02d} min"
    return f"{minutes} min" if minutes else f"{seconds} s"


def consumption(trip: dict[str, Any]) -> float | None:
    """kWh/100 km: the car's own figure, else energy over distance."""
    value = trip.get("avg_consumption_kwh_per_100km")
    if value is not None:
        return float(value)
    if trip.get("energy_kwh") is not None and trip.get("distance_m", 0) > 0:
        return trip["energy_kwh"] / (trip["distance_m"] / 1000.0) * 100.0
    return None


def is_test_source(source: str | None) -> bool:
    return bool(source) and bool(_TEST_SOURCE.search(source or ""))


def _time_html(iso: str) -> str:
    """UTC text; the page script swaps it for the viewer's local time (``data-t``)."""
    shown = iso.replace("T", " ").replace("Z", "")[:16] + " UTC"
    return f'<time datetime="{_e(iso)}" data-t>{_e(shown)}</time>'


def _coords(lat: float, lon: float) -> str:
    return f"{lat:.4f}, {lon:.4f}"


def _source_html(trip: dict[str, Any]) -> str:
    badge = ' <span class="badge test">test</span>' if is_test_source(trip.get("source")) else ""
    return _e(trip.get("source") or "") + badge


def _soc_html(trip: dict[str, Any]) -> str:
    if trip["soc_start"] is None and trip["soc_end"] is None:
        return "-"
    return f'{_num(trip["soc_start"], 0)} &rarr; {_num(trip["soc_end"], 0)} %'


def month_range(month: str) -> tuple[float, float] | None:
    m = _MONTH.match(month or "")
    if not m:
        return None
    year, mon = int(m.group(1)), int(m.group(2))
    start = datetime(year, mon, 1, tzinfo=timezone.utc)
    end = datetime(year + (mon == 12), mon % 12 + 1, 1, tzinfo=timezone.utc)
    return start.timestamp(), end.timestamp()


# ---------------------------------------------------------------- templates

_CSS = """
:root{--bg:#f4f4f6;--fg:#1c1c1e;--card:#fff;--line:rgba(128,128,128,.35);--ok:#27ae60;--bad:#c0392b;--info:#2980b9;--warn:#b9770e;--warnbg:#fdf2d9;--bar:#2980b9}
@media(prefers-color-scheme:dark){:root{--bg:#1c1c1e;--fg:#eee;--card:#2c2c2e;--warn:#f0b34a;--warnbg:#4a3a14;--bar:#5ab0ff}}
*{box-sizing:border-box}
body{font-family:system-ui,sans-serif;margin:0 auto;padding:16px;background:var(--bg);color:var(--fg);max-width:1100px}
h1{font-size:1.4rem;margin:0 0 8px}h2{font-size:1.05rem;margin:20px 0 8px}
nav{display:flex;gap:6px;margin:0 0 14px}
nav a{padding:6px 14px;border-radius:16px;text-decoration:none;color:var(--fg);background:var(--card);box-shadow:0 1px 3px rgba(0,0,0,.15);font-size:.9rem}
nav a.on{background:var(--info);color:#fff}
a{color:var(--info)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin:8px 0 14px}
.stat,.panel{background:var(--card);border-radius:10px;padding:10px 14px;box-shadow:0 1px 3px rgba(0,0,0,.15)}
.stat .v{font-size:1.35rem;font-weight:600}.stat .l,.sub{font-size:.8rem;opacity:.75}
.panel{margin:12px 0}
.tools{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:10px 0}
.tools .grow{flex:1}
select,.btn,button{font:inherit;font-size:.9rem;border-radius:6px;border:1px solid var(--line);padding:6px 12px;background:var(--card);color:var(--fg);text-decoration:none;cursor:pointer}
button.danger{background:var(--bad);color:#fff;border:0}button:disabled{opacity:.5}
.wrap{overflow-x:auto;background:var(--card);border-radius:10px;box-shadow:0 1px 3px rgba(0,0,0,.15)}
table{width:100%;border-collapse:collapse;font-size:.85rem}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
td.n,th.n{text-align:right}
tbody tr:last-child td{border-bottom:0}
.badge{font-size:.65rem;padding:1px 6px;border-radius:8px;background:#888;color:#fff;vertical-align:middle}
.badge.test{background:var(--warn);color:#000}
.empty{opacity:.65;padding:16px}
.pager{display:flex;justify-content:space-between;align-items:center;margin:10px 0;font-size:.9rem}
.off{opacity:.4;pointer-events:none}
svg.chart{width:100%;height:auto;display:block}
svg.chart text{fill:currentColor;font-size:10px}
svg.chart rect{fill:var(--bar)}
dl.facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:8px 16px;margin:0}
dl.facts div{min-width:0}dl.facts dt{font-size:.75rem;opacity:.7}dl.facts dd{margin:0;word-break:break-word}
#map{height:min(60vh,420px);border-radius:10px;background:var(--line);margin-top:8px}
.note{background:var(--warnbg);color:var(--warn);border-radius:8px;padding:8px 12px;margin:8px 0;font-size:.85rem}
.error{color:var(--bad);font-size:.85rem}
#overview{height:min(50vh,380px);border-radius:10px;background:var(--line)}
.legend{display:flex;align-items:center;gap:8px;font-size:.8rem;margin:6px 0}
.legend .bar{flex:0 1 160px;height:10px;border-radius:5px;background:linear-gradient(90deg,hsl(120,75%,42%),hsl(60,75%,42%),hsl(0,75%,42%))}
.legend .grey{width:10px;height:10px;border-radius:5px;background:#8a8a8a}
tr.hl td{background:rgba(41,128,185,.18)}tbody tr[data-id]{cursor:pointer}
.leaflet-container{font:inherit}
"""

# turns the UTC times into the viewer's local time (the server does not know the viewer's zone)
_TIME_JS = """
(function(){var t=document.querySelectorAll('time[data-t]');
for(var i=0;i<t.length;i++){var d=new Date(t[i].getAttribute('datetime'));
if(!isNaN(d)){t[i].textContent=d.toLocaleString([],{dateStyle:'medium',timeStyle:'short'});}}})();
"""


def _page(title: str, root: str, active: str, body: str, head: str = "", scripts: str = "") -> str:
    def link(name: str, href: str, label: str) -> str:
        cls = ' class="on"' if name == active else ""
        return f'<a href="{href}"{cls}>{label}</a>'

    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="color-scheme" content="light dark">\n'
        f"<title>{_e(title)}</title>\n<style>{_CSS}</style>\n{SESSION_GUARD_TAG}\n{head}</head><body>\n"
        "<h1>Car Media and Trips</h1>\n"
        f'<nav>{link("review", root, "Media")}{link("trips", root + "trips", "Trips")}{link("regions", root + "regions", "Regions")}</nav>\n'
        f"{body}\n<script>{_TIME_JS}</script>\n{scripts}</body></html>\n"
    )


def chart_svg(trips: list[dict[str, Any]]) -> str:
    """Bar chart of kWh/100 km of the given trips (oldest first), plain inline SVG."""
    points = [(t, consumption(t)) for t in trips]
    points = [(t, c) for t, c in points if c is not None]
    if not points:
        return '<p class="empty">No consumption data yet.</p>'
    w, h, left, bottom, top = 600, 150, 34, 18, 12
    top_val = max(max(c for _, c in points), 1.0) * 1.1
    slot = (w - left) / len(points)
    bar = slot * 0.7
    parts = [f'<svg class="chart" viewBox="0 0 {w} {h}" role="img" aria-label="kWh per 100 km, last {len(points)} trips">']
    for frac in (0, 0.5, 1):
        y = top + (h - top - bottom) * (1 - frac)
        parts.append(
            f'<line x1="{left}" x2="{w}" y1="{y:.1f}" y2="{y:.1f}" stroke="currentColor" opacity=".2"/>'
            f'<text x="{left - 4}" y="{y + 3:.1f}" text-anchor="end">{top_val * frac:.0f}</text>'
        )
    for i, (trip, value) in enumerate(points):
        bh = max(1.0, (h - top - bottom) * max(value, 0) / top_val)
        x = left + i * slot + (slot - bar) / 2
        y = h - bottom - bh
        label = f'{trip["started_at"][:10]}: {value:.1f} kWh/100 km, {trip["distance_m"] / 1000:.1f} km'
        parts.append(
            f'<g><title>{_e(label)}</title><rect x="{x:.1f}" y="{y:.1f}" width="{bar:.1f}" height="{bh:.1f}" rx="2"/></g>'
        )
    parts.append(f'<text x="{left}" y="{h - 4}">{_e(points[0][0]["started_at"][:10])}</text>')
    parts.append(f'<text x="{w}" y="{h - 4}" text-anchor="end">{_e(points[-1][0]["started_at"][:10])}</text>')
    parts.append("</svg>")
    return "".join(parts)


def render_trips(stats: dict[str, Any], trips: list[dict[str, Any]], chart_trips: list[dict[str, Any]],
                 months: list[str], month: str, page: int, total: int) -> str:
    t, d = stats["total"], stats["last_30_days"]

    def card(value: str, label: str) -> str:
        return f'<div class="stat"><div class="v">{_e(value)}</div><div class="l">{_e(label)}</div></div>'

    cards = "".join([
        card(str(t["trips"]), "Trips"),
        card(f'{t["km"]:.1f}', "Total km"),
        card(f'{t["kwh"]:.1f}', "Total kWh"),
        card(_num(t["avg_kwh_per_100km"]), "Avg kWh/100 km"),
        card(f'{d["km"]:.0f} km', f'Last 30 days ({d["trips"]} trips)'),
    ])
    options = ['<option value="">All months</option>']
    for m in months:
        sel = " selected" if m == month else ""
        options.append(f'<option value="{_e(m)}"{sel}>{_e(m)}</option>')
    if month and month not in months:
        options.append(f'<option value="{_e(month)}" selected>{_e(month)}</option>')
    tools = (
        '<form class="tools" method="get" action="trips">'
        f'<label>Month <select name="month" onchange="this.form.submit()">{"".join(options)}</select></label>'
        '<noscript><button type="submit">Filter</button></noscript><span class="grow"></span>'
        '<a class="btn" href="trips.csv" download>CSV</a>'
        '<a class="btn" href="trips.csv?decimal=comma" download title="Decimal comma for German Excel">CSV (1,5)</a>'
        "</form>"
    )
    if trips:
        rows = []
        for trip in trips:
            rows.append(
                f'<tr data-id="{_e(trip["id"])}">'
                f'<td><a href="trips/{_e(trip["id"])}">{_time_html(trip["started_at"])}</a></td>'
                f'<td>{_e(_coords(trip["start_lat"], trip["start_lon"]))} &rarr; '
                f'{_e(_coords(trip["end_lat"], trip["end_lon"]))}</td>'
                f'<td class="n">{trip["distance_m"] / 1000:.1f} km</td>'
                f'<td class="n">{_e(duration_text(trip["duration_s"]))}</td>'
                f'<td class="n">{_num(trip["energy_kwh"], 2)} kWh</td>'
                f'<td class="n">{_num(consumption(trip))}</td>'
                f"<td>{_soc_html(trip)}</td>"
                f"<td>{_source_html(trip)}</td>"
                "</tr>"
            )
        table = (
            '<div class="wrap"><table><thead><tr><th>Start</th><th>From &rarr; to</th><th class="n">Distance</th>'
            '<th class="n">Duration</th><th class="n">Energy</th><th class="n">kWh/100 km</th><th>SOC</th>'
            f'<th>Source</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
        )
    else:
        table = '<div class="wrap"><p class="empty">No trips yet. Switch on trip upload in the car app.</p></div>'
    pages = max(1, -(-total // PAGE_SIZE))
    qs = f"&amp;month={_e(month)}" if month else ""
    prev_link = (f'<a class="btn" href="trips?page={page - 1}{qs}">&larr; Newer</a>' if page > 1
                 else '<span class="btn off">&larr; Newer</span>')
    next_link = (f'<a class="btn" href="trips?page={page + 1}{qs}">Older &rarr;</a>' if page < pages
                 else '<span class="btn off">Older &rarr;</span>')
    pager = f'<div class="pager">{prev_link}<span>Page {page} of {pages} ({total} trips)</span>{next_link}</div>'
    q_month = f"?month={_e(month)}" if month else ""
    overview = (
        f'<h2>Map {_e(month) if month else "(all trips)"}</h2>'
        f'<div id="overview" data-url="trips/overview.json{q_month}" data-tiles="tiles/{{z}}/{{x}}/{{y}}.png?v=1"></div>'
        '<div class="legend"><span id="overview-note" class="sub">Loading map...</span><span class="grow"></span>'
        '<span id="legend-lo">?</span><span class="bar"></span><span id="legend-hi">?</span>'
        '<span class="sub">kWh/100 km</span><span class="grey"></span><span class="sub">unknown</span></div>'
        '<div class="sub">Solid line: uploaded track. Dashed line: start to end only. Click a line or a row.</div>'
    )
    body = (
        f'<div class="cards">{cards}</div>{overview}'
        f'<h2>Consumption, last {CHART_TRIPS} trips (kWh/100 km)</h2><div class="panel">{chart_svg(chart_trips)}</div>'
        f"<h2>Trips</h2>{tools}{table}{pager}"
    )
    head = ('<link rel="stylesheet" href="static/leaflet/leaflet.min.css">\n'
            '<script src="static/leaflet/leaflet.min.js"></script>\n')
    return _page("Trips - Car Media and Trips", "./", "trips", body, head=head,
                 scripts=f"<script>{_OVERVIEW_JS}</script>")


_DETAIL_JS = """
(function(){
'use strict';
var box=document.getElementById('map');
function decode(s,p){var f=Math.pow(10,p),i=0,lat=0,lon=0,out=[];
while(i<s.length){var r=0,sh=0,b;do{b=s.charCodeAt(i++)-63;r|=(b&31)<<sh;sh+=5;}while(b>=32);lat+=(r&1)?~(r>>1):(r>>1);
r=0;sh=0;do{b=s.charCodeAt(i++)-63;r|=(b&31)<<sh;sh+=5;}while(b>=32);lon+=(r&1)?~(r>>1):(r>>1);
out.push([lat/f,lon/f]);}return out;}
window.decodePolyline=decode;
if(box&&typeof L!=='undefined'){
var a=[+box.dataset.startLat,+box.dataset.startLon],z=[+box.dataset.endLat,+box.dataset.endLon];
var pts=box.dataset.shape?decode(box.dataset.shape,6):[];
var map=L.map(box);
L.tileLayer(box.dataset.tiles,{maxZoom:19,
attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'}).addTo(map);
var layer;
if(pts.length>1){layer=L.polyline(pts,{color:'#2f8fe0',weight:4,opacity:.9}).addTo(map);a=pts[0];z=pts[pts.length-1];}
else{layer=L.polyline([a,z],{color:'#2f8fe0',weight:3,dashArray:'6 8'}).addTo(map);}
L.circleMarker(a,{radius:7,color:'#27ae60',fillColor:'#27ae60',fillOpacity:1}).bindTooltip('Start').addTo(map);
L.circleMarker(z,{radius:7,color:'#c0392b',fillColor:'#c0392b',fillOpacity:1}).bindTooltip('End').addTo(map);
map.fitBounds(layer.getBounds(),{padding:[24,24],maxZoom:16});
}
var del=document.getElementById('delete-btn');
if(del){del.addEventListener('click',function(){
if(!confirm('Delete this trip and its samples? This cannot be undone.'))return;
del.disabled=true;
fetch(del.dataset.url,{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'car-media-hub'},body:'{}'})
.then(function(r){return r.json().catch(function(){return{};}).then(function(j){if(!r.ok)throw new Error(j.error||('HTTP '+r.status));});})
.then(function(){location.href=del.dataset.back;})
.catch(function(e){var n=document.getElementById('del-error');n.textContent=String(e.message||e);n.hidden=false;del.disabled=false;});
});}
})();
"""


def render_trip(trip: dict[str, Any], sample_count: int) -> str:
    def fact(label: str, value: str) -> str:
        return f"<div><dt>{_e(label)}</dt><dd>{value}</dd></div>"

    facts = "".join([
        fact("Start", _time_html(trip["started_at"])),
        fact("End", _time_html(trip["ended_at"])),
        fact("From", _e(_coords(trip["start_lat"], trip["start_lon"]))),
        fact("To", _e(_coords(trip["end_lat"], trip["end_lon"]))),
        fact("Distance", f'{trip["distance_m"] / 1000:.2f} km'),
        fact("Duration", _e(duration_text(trip["duration_s"]))),
        fact("Energy", f'{_num(trip["energy_kwh"], 2)} kWh'),
        fact("Consumption", f'{_num(consumption(trip))} kWh/100 km'),
        fact("SOC", _soc_html(trip)),
        fact("Outside temperature", "-" if trip["ext_temp_c"] is None else f'{trip["ext_temp_c"]:.1f} &deg;C'),
        fact("Vehicle", _e(trip["vehicle"] or "-")),
        fact("Source", _source_html(trip)),
        fact("Samples", str(sample_count)),
        fact("Trip id", _e(trip["id"])),
    ])
    shape = trip.get("shape") or ""
    note = "" if shape else (
        '<div class="note">No track was uploaded; enable <b>Include route track</b> in the car app. '
        "The dashed line only connects start and end.</div>")
    head = ('<link rel="stylesheet" href="../static/leaflet/leaflet.min.css">\n'
            '<script src="../static/leaflet/leaflet.min.js"></script>\n')
    body = (
        '<p><a href="../trips">&larr; All trips</a></p>'
        f'<h2>Trip {_time_html(trip["started_at"])}</h2>'
        f'<div class="panel"><dl class="facts">{facts}</dl></div>'
        f"{note}"
        f'<div id="map" data-tiles="../tiles/{{z}}/{{x}}/{{y}}.png?v=1" data-shape="{_e(shape)}" '
        f'data-start-lat="{trip["start_lat"]}" data-start-lon="{trip["start_lon"]}" '
        f'data-end-lat="{trip["end_lat"]}" data-end-lon="{trip["end_lon"]}"></div>'
        '<p class="sub">Map data &copy; OpenStreetMap contributors. Without internet access on the Home '
        "Assistant host the route is drawn on a plain background.</p>"
        '<div class="tools"><button class="danger" id="delete-btn" '
        f'data-url="{_e(trip["id"])}/delete" data-back="../trips">Delete trip</button></div>'
        '<div class="error" id="del-error" hidden></div>'
    )
    return _page("Trip - Car Media and Trips", "../", "trips", body, head=head,
                 scripts=f"<script>{_DETAIL_JS}</script>")


# ---------------------------------------------------------------- overview map

OVERVIEW_MAX_TRIPS = 2000     # trips in one overview response (newest first); "truncated" says when cut
OVERVIEW_FULL_SHAPES = 200    # the newest trips keep their full track ...
OVERVIEW_THIN_POINTS = 40     # ... older ones are thinned to about this many points


def decode_polyline(text: str, precision: int = 6) -> list[tuple[float, float]]:
    factor = 10 ** precision
    points: list[tuple[float, float]] = []
    i = lat = lon = 0
    while i < len(text):
        pair = []
        for _ in range(2):
            result = shift = 0
            while True:
                b = ord(text[i]) - 63
                i += 1
                result |= (b & 31) << shift
                shift += 5
                if b < 32:
                    break
            pair.append(~(result >> 1) if result & 1 else result >> 1)
        lat += pair[0]
        lon += pair[1]
        points.append((lat / factor, lon / factor))
    return points


def encode_polyline(points: list[tuple[float, float]], precision: int = 6) -> str:
    factor = 10 ** precision
    out: list[str] = []
    prev_lat = prev_lon = 0
    for lat, lon in points:
        cur_lat, cur_lon = round(lat * factor), round(lon * factor)
        for delta in (cur_lat - prev_lat, cur_lon - prev_lon):
            value = ~(delta << 1) if delta < 0 else delta << 1
            while value >= 32:
                out.append(chr((32 | (value & 31)) + 63))
                value >>= 5
            out.append(chr(value + 63))
        prev_lat, prev_lon = cur_lat, cur_lon
    return "".join(out)


def thin_shape(shape: str, max_points: int = OVERVIEW_THIN_POINTS) -> str:
    """Every Nth point of a track (first and last always kept); bad input gives an empty string."""
    try:
        points = decode_polyline(shape)
    except (IndexError, ValueError):
        return ""
    if len(points) <= max_points:
        return shape
    step = -(-len(points) // max_points)
    kept = points[::step]
    if kept[-1] != points[-1]:
        kept.append(points[-1])
    return encode_polyline(kept)


def overview_data(trips: list[dict[str, Any]], truncated: bool = False) -> dict[str, Any]:
    items = []
    for i, trip in enumerate(trips):
        shape = trip.get("shape") or ""
        if shape and i >= OVERVIEW_FULL_SHAPES:
            shape = thin_shape(shape)
        c = consumption(trip)
        items.append({
            "id": trip["id"], "t": trip["started_at"], "km": round(trip["distance_m"] / 1000.0, 2),
            "s": trip["duration_s"], "c": None if c is None else round(c, 1),
            "a": [trip["start_lat"], trip["start_lon"]], "b": [trip["end_lat"], trip["end_lon"]],
            "p": shape or None,
        })
    return {"trips": items, "truncated": truncated}


async def trips_overview(request: web.Request) -> web.Response:
    db = _db(request)
    bounds = month_range(request.query.get("month", ""))
    since, until = bounds if bounds else (None, None)
    rows = db.query_trips(since, until, OVERVIEW_MAX_TRIPS + 1, 0, with_shape=True)
    truncated = len(rows) > OVERVIEW_MAX_TRIPS
    text = json.dumps(overview_data(rows[:OVERVIEW_MAX_TRIPS], truncated), separators=(",", ":"))
    # never let a "</script>" or markup in any text survive in the payload
    text = text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return web.Response(text=text, content_type="application/json", headers={"Cache-Control": "no-store"})


_OVERVIEW_JS = """
(function(){
'use strict';
var box=document.getElementById('overview');
if(!box||typeof L==='undefined')return;
function decode(s,p){var f=Math.pow(10,p),i=0,lat=0,lon=0,out=[];
while(i<s.length){var r=0,sh=0,b;do{b=s.charCodeAt(i++)-63;r|=(b&31)<<sh;sh+=5;}while(b>=32);lat+=(r&1)?~(r>>1):(r>>1);
r=0;sh=0;do{b=s.charCodeAt(i++)-63;r|=(b&31)<<sh;sh+=5;}while(b>=32);lon+=(r&1)?~(r>>1):(r>>1);
out.push([lat/f,lon/f]);}return out;}
var map=L.map(box);
L.tileLayer(box.dataset.tiles,{maxZoom:19,
attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'}).addTo(map);
map.setView([51,10],5);
var layers={},rows={},current=null;
function colour(c,lo,hi){if(c===null||c===undefined)return '#8a8a8a';
var f=hi>lo?Math.min(1,Math.max(0,(c-lo)/(hi-lo))):0;return 'hsl('+Math.round(120*(1-f))+',75%,42%)';}
function rowOf(id){return document.querySelector('tr[data-id="'+id+'"]');}
function hl(id,on){var l=layers[id],r=rowOf(id);
if(l){l.setStyle({weight:on?8:4,opacity:on?1:.8});if(on)l.bringToFront();}
if(r)r.classList.toggle('hl',on);}
function popup(t){var d=document.createElement('div');
function line(x){var p=document.createElement('div');p.textContent=x;d.appendChild(p);}
line(new Date(t.t).toLocaleString([],{dateStyle:'medium',timeStyle:'short'}));
line(t.km.toFixed(1)+' km, '+Math.round(t.s/60)+' min');
line(t.c===null?'consumption unknown':t.c.toFixed(1)+' kWh/100 km');
var a=document.createElement('a');a.href='trips/'+encodeURIComponent(t.id);a.textContent='Details';d.appendChild(a);return d;}
fetch(box.dataset.url).then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.json();}).then(function(data){
var ts=data.trips,vals=ts.map(function(t){return t.c;}).filter(function(c){return c!==null;});
var lo=vals.length?Math.min.apply(null,vals):0,hi=vals.length?Math.max.apply(null,vals):0,all=L.featureGroup();
ts.forEach(function(t){
var col=colour(t.c,lo,hi),pts=t.p?decode(t.p,6):[],layer;
if(pts.length>1){layer=L.polyline(pts,{color:col,weight:4,opacity:.8});}
else{pts=[t.a,t.b];layer=L.polyline(pts,{color:col,weight:3,opacity:.8,dashArray:'6 8'});}
var g=L.featureGroup([layer,
L.circleMarker(pts[0],{radius:5,color:'#27ae60',fillColor:'#27ae60',fillOpacity:1,weight:1}),
L.circleMarker(pts[pts.length-1],{radius:5,color:'#c0392b',fillColor:'#c0392b',fillOpacity:1,weight:1})]);
g.bindPopup(function(){return popup(t);});
g.on('mouseover',function(){hl(t.id,true);}).on('mouseout',function(){hl(t.id,false);});
g.addTo(all);layers[t.id]=layer;layer._g=g;});
all.addTo(map);
if(ts.length)map.fitBounds(all.getBounds(),{padding:[20,20],maxZoom:15});
var note=document.getElementById('overview-note');
note.textContent=ts.length?(ts.length+' trips'+(data.truncated?' (newest only)':'')):'No trips for this filter.';
var lg=document.getElementById('legend-lo');lg.textContent=vals.length?lo.toFixed(1):'?';
document.getElementById('legend-hi').textContent=vals.length?hi.toFixed(1):'?';
Array.prototype.forEach.call(document.querySelectorAll('tr[data-id]'),function(r){
var id=r.getAttribute('data-id');
r.addEventListener('mouseenter',function(){hl(id,true);});
r.addEventListener('mouseleave',function(){hl(id,false);});
r.addEventListener('click',function(e){if(e.target.closest('a'))return;var l=layers[id];if(!l)return;
map.fitBounds(l.getBounds(),{padding:[30,30],maxZoom:16});l._g.openPopup(l.getBounds().getCenter());
box.scrollIntoView({behavior:'smooth',block:'nearest'});});});
}).catch(function(e){document.getElementById('overview-note').textContent='Could not load the map data: '+(e.message||e);});
})();
"""


# ---------------------------------------------------------------- CSV

CSV_HEADER = ["id", "started_at", "ended_at", "start_lat", "start_lon", "end_lat", "end_lon", "distance_km",
              "duration_s", "energy_kwh", "soc_start", "soc_end", "consumption_kwh_per_100km", "ext_temp_c",
              "source", "vehicle", "has_track"]


def _csv_text(value: str | None) -> str:
    """Text cell; a leading = + - @ would be run as a formula by spreadsheets, so it is defused."""
    text = value or ""
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def build_csv(trips: list[dict[str, Any]], decimal_comma: bool = False) -> bytes:
    def n(value: float | None, digits: int) -> str:
        if value is None:
            return ""
        text = f"{value:.{digits}f}"
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text.replace(".", ",") if decimal_comma else text

    out = io.StringIO()
    w = csv.writer(out, delimiter=";", lineterminator="\r\n")
    w.writerow(CSV_HEADER)
    for t in trips:
        w.writerow([
            t["id"], t["started_at"], t["ended_at"],
            n(t["start_lat"], 6), n(t["start_lon"], 6), n(t["end_lat"], 6), n(t["end_lon"], 6),
            n(t["distance_m"] / 1000.0, 3), t["duration_s"], n(t["energy_kwh"], 3),
            n(t["soc_start"], 1), n(t["soc_end"], 1), n(consumption(t), 2), n(t["ext_temp_c"], 1),
            _csv_text(t["source"]), _csv_text(t["vehicle"]), "yes" if t.get("has_shape") else "no",
        ])
    return ("﻿" + out.getvalue()).encode("utf-8")


# ---------------------------------------------------------------- handlers


def _db(request: web.Request) -> Any:
    return request.app[_GET_DB]()


def _html(text: str) -> web.Response:
    return web.Response(body=text.encode("utf-8", "replace"), content_type="text/html", charset="utf-8",
                        headers={"Cache-Control": "no-store"})


def _int(value: str | None, default: int, lo: int, hi: int) -> int:
    try:
        return min(hi, max(lo, int(value))) if value is not None else default
    except ValueError:
        return default


async def trips_page(request: web.Request) -> web.Response:
    db = _db(request)
    now = request.app[_CLOCK]()
    month = request.query.get("month", "")
    bounds = month_range(month)
    if not bounds:
        month = ""
    page = _int(request.query.get("page"), 1, 1, 100_000)
    since, until = bounds if bounds else (None, None)
    total = db.trip_totals(since, until)["trips"]
    trips = db.query_trips(since, until, PAGE_SIZE, (page - 1) * PAGE_SIZE)
    chart = list(reversed(db.query_trips(None, None, CHART_TRIPS)))
    return _html(render_trips(compute_stats(db, now), trips, chart, db.trip_months(), month, page, total))


def _trip_id(request: web.Request) -> str | None:
    try:
        return str(uuid.UUID(request.match_info["id"]))
    except ValueError:
        return None


async def trip_page(request: web.Request) -> web.Response:
    db = _db(request)
    tid = _trip_id(request)
    trip = db.get_trip(tid) if tid else None
    if trip is None:
        return web.Response(status=404, text="Trip not found")
    return _html(render_trip(trip, len(db.list_trip_samples(tid))))


async def trips_csv(request: web.Request) -> web.Response:
    db = _db(request)
    total = db.trip_totals()["trips"]
    trips = db.query_trips(None, None, max(total, 1), 0)
    body = build_csv(trips, request.query.get("decimal") == "comma")
    return web.Response(body=body, content_type="text/csv", charset="utf-8", headers={
        "Content-Disposition": 'attachment; filename="trips.csv"', "Cache-Control": "no-store"})


async def trip_delete(request: web.Request) -> web.Response:
    # JSON content type plus a custom header: a cross-site form post can send neither without a preflight
    if request.content_type != "application/json" or request.headers.get("X-Requested-With") != "car-media-hub":
        return web.json_response({"ok": False, "error": "expected a JSON request from the page"}, status=400)
    tid = _trip_id(request)
    if tid is None or not _db(request).delete_trip(tid):
        return web.json_response({"ok": False, "error": "trip not found"}, status=404)
    return web.json_response({"ok": True, "deleted": tid})


async def _fetch_tile(url: str) -> bytes | None:
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": TILE_USER_AGENT}) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.read()
    except Exception as exc:  # noqa: BLE001 - offline host: the map just stays plain
        log.debug("tile fetch failed: %s", exc)
        return None
    return data if data.startswith(b"\x89PNG") else None


async def tile(request: web.Request) -> web.StreamResponse:
    try:
        z, x, y = (int(request.match_info[k]) for k in ("z", "x", "y"))
    except ValueError:
        return web.Response(status=400, text="invalid tile")
    if not (0 <= z <= 19 and 0 <= x < 2 ** z and 0 <= y < 2 ** z):
        return web.Response(status=400, text="invalid tile")
    cached = tile_cache_dir() / str(z) / str(x) / f"{y}.png"
    if not cached.exists():
        # at most 2 upstream fetches at once (OSM tile policy); one gate per event loop
        loop = asyncio.get_running_loop()
        gate = _tile_gates.setdefault(loop, asyncio.Semaphore(2))
        async with gate:
            if not cached.exists():
                data = await _fetch_tile(TILE_UPSTREAM.format(z=z, x=x, y=y))
                if data is None:
                    return web.Response(status=502, text="tile not available")
                try:
                    cached.parent.mkdir(parents=True, exist_ok=True)
                    tmp = cached.with_suffix(f".tmp-{os.getpid()}")
                    tmp.write_bytes(data)
                    os.replace(tmp, cached)
                except OSError:
                    return web.Response(body=data, content_type="image/png")
    return web.FileResponse(cached, headers={"Cache-Control": "public, max-age=2592000"})


def register_trip_pages(app: web.Application, get_db: Callable[[], Any],
                        clock: Callable[[], float] = time.time) -> None:
    app[_GET_DB] = get_db
    app[_CLOCK] = clock
    r = app.router
    r.add_get("/trips", trips_page)
    r.add_get("/trips.csv", trips_csv)
    r.add_get("/trips/overview.json", trips_overview)
    r.add_get("/trips/{id}", trip_page)
    r.add_post("/trips/{id}/delete", trip_delete)
    r.add_get("/tiles/{z}/{x}/{y}.png", tile)
    r.add_static("/static/", STATIC_DIR, follow_symlinks=False)
