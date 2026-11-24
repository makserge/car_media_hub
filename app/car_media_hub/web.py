"""Ingress web UI: status bar, review of pending releases, library browser."""
from __future__ import annotations

import dataclasses
import html
import inspect
import json
import logging
import os
import re
from datetime import datetime
from typing import Any, Awaitable, Callable

from aiohttp import web

from car_media_hub.browser import register_browser_pages
from car_media_hub.feed_web import register_feed_pages
from car_media_hub.diskspace import NoSpaceError
from car_media_hub.regions_web import register_region_pages
from car_media_hub.session_guard import SESSION_GUARD_TAG
from car_media_hub.trips_web import register_trip_pages

log = logging.getLogger(__name__)

INGRESS_IP = "172.30.32.2"
STATUSES = ("processing", "pending", "approved", "declined", "empty", "failed")
DECIDED_STATUSES = ["approved", "declined", "empty", "failed"]
_CONTROLLER = web.AppKey("controller", object)

_REMOVAL_LABELS = (
    ("removed_cyrillic_name", "Cyrillic file names"),
    ("removed_cyrillic_tags", "Cyrillic tags"),
    ("removed_mp3_due_to_flac", "MP3 (FLAC present)"),
    ("removed_duplicate_tracks", "duplicate tracks"),
    ("tags_stripped", "tags stripped"),
    ("removed_hidden", "hidden entries"),
)


def human_size(num: float) -> str:
    """Format a byte count, e.g. ``1.5 MB``."""
    value = float(num or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"  # pragma: no cover


def _fmt_time(ts: float | None) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _release_dict(release: Any) -> dict[str, Any]:
    return dataclasses.asdict(release) if dataclasses.is_dataclass(release) else dict(vars(release))


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


@web.middleware
async def ip_middleware(request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]):
    """Only the Supervisor ingress proxy may talk to us (unless disabled for dev/tests)."""
    if os.environ.get("CAR_MEDIA_HUB_ALLOW_ANY_IP") != "1" and request.remote != INGRESS_IP:
        log.warning("Rejected request from %s", request.remote)
        return web.Response(status=403, text="Forbidden")
    return await handler(request)


# ---------------------------------------------------------------- rendering


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _render_card(rel: Any, pending: bool) -> str:
    report = rel.report or {}
    e = html.escape
    kept = _as_list(report.get("kept_tracks"))
    others = _as_list(report.get("other_files"))
    renamed = [p for p in _as_list(report.get("renamed")) if isinstance(p, (list, tuple)) and len(p) == 2]
    unsyncable = _as_list(report.get("unsyncable"))
    removals = []
    for key, label in _REMOVAL_LABELS:
        items = _as_list(report.get(key))
        if items:
            removals.append(f"{len(items)} {label}")
    meta_bits = [
        e(_fmt_time(rel.created_at)),
        f"{len(kept)} tracks",
        e(human_size(report.get("total_bytes", 0))),
    ]
    if report.get("source"):
        meta_bits.append("from " + e(str(report["source"])))
    if report.get("creator"):
        meta_bits.append("by " + e(str(report["creator"])))
    extra = []
    url = str(report.get("url") or "")
    if url.startswith(("https://", "http://")):
        extra.append(f'<a href="{e(url, quote=True)}" target="_blank" rel="noopener noreferrer nofollow">{e(url)}</a>')
    lic = str(report.get("license") or "")
    if lic.startswith(("https://", "http://")):
        extra.append(f'License: <a href="{e(lic, quote=True)}" target="_blank" rel="noopener noreferrer nofollow">{e(lic)}</a>')
    elif lic:
        extra.append("License: " + e(lic))

    parts = [
        f'<section class="card" data-id="{e(rel.id, quote=True)}">',
        f'<h3>{e(rel.name)} <span class="status {e(rel.status, quote=True)}">{e(rel.status)}</span></h3>',
        f'<p class="meta">{" · ".join(meta_bits)}</p>',
    ]
    if extra:
        parts.append(f'<p class="meta">{" · ".join(extra)}</p>')
    if removals:
        parts.append(f'<p class="removed">Removed: {e(", ".join(removals))}</p>')
    if renamed:
        parts.append(f'<p class="removed">Renamed for the sync API: {len(renamed)}</p>')
    if unsyncable:
        parts.append(
            f'<p class="warn">{len(unsyncable)} audio file(s) in a format the sync API does not serve</p>'
        )
    if rel.error:
        parts.append(f'<p class="error">Error: {e(rel.error)}</p>')
    if kept or others:
        items = "".join(f"<li>{e(str(t[0]))}</li>" for t in kept)
        items += "".join(f'<li class="other">{e(str(o))}</li>' for o in others)
        parts.append(f"<details><summary>Files ({len(kept) + len(others)})</summary><ul>{items}</ul></details>")
    if renamed:
        items = "".join(f"<li>{e(str(old))} &rarr; {e(str(new))}</li>" for old, new in renamed)
        parts.append(f"<details><summary>Renamed ({len(renamed)})</summary><ul>{items}</ul></details>")
    if unsyncable:
        items = "".join(f"<li>{e(str(name))}</li>" for name in unsyncable)
        parts.append(f"<details><summary>Not syncable ({len(unsyncable)})</summary><ul>{items}</ul></details>")
    if pending:
        parts.append(
            '<div class="actions"><button class="approve">Approve</button>'
            '<button class="decline">Decline</button></div>'
        )
    parts.append("</section>")
    return "\n".join(parts)


def _render_row(rel: Any) -> str:
    """A decided release as one list row (like the entries of the library), not a card."""
    report = rel.report or {}
    e = html.escape
    kept = _as_list(report.get("kept_tracks"))
    bits = [e(_fmt_time(rel.created_at)), f"{len(kept)} tracks", e(human_size(report.get("total_bytes", 0)))]
    if report.get("source"):
        bits.append("from " + e(str(report["source"])))
    error = f'<div class="error">Error: {e(rel.error)}</div>' if rel.error else ""
    return (
        f'<div class="entry" data-id="{e(rel.id, quote=True)}">'
        f'<div class="name">{e(rel.name)} <span class="status {e(rel.status, quote=True)}">{e(rel.status)}</span>{error}</div>'
        f'<span class="sub">{" · ".join(bits)}</span></div>'
    )


# The page is one self-contained document. Dynamic data reaches the DOM only through
# textContent (client side) or html.escape (server side); markers are filled in a single pass.
_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Car Media and Trips</title>
<style>
:root{--bg:#f4f4f6;--fg:#1c1c1e;--card:#fff;--line:rgba(128,128,128,.35);--ok:#27ae60;--bad:#c0392b;--info:#2980b9;--warn:#b9770e;--warnbg:#fdf2d9}
@media(prefers-color-scheme:dark){:root{--bg:#1c1c1e;--fg:#eee;--card:#2c2c2e;--warn:#f0b34a;--warnbg:#4a3a14}}
body{font-family:system-ui,sans-serif;margin:0;padding:16px;background:var(--bg);color:var(--fg)}
h1{font-size:1.4rem;margin:0 0 12px}h2{font-size:1.1rem;margin-top:2rem}
.card,.panel{background:var(--card);border-radius:10px;padding:12px 16px;margin:12px 0;box-shadow:0 1px 3px rgba(0,0,0,.15)}
.card h3{margin:0 0 4px;font-size:1rem;word-break:break-word}
.meta,.removed{margin:2px 0;font-size:.85rem;opacity:.8}.error{color:var(--bad);font-size:.85rem}
.warn{color:var(--warn);font-size:.85rem;margin:2px 0}
.status{font-size:.7rem;padding:2px 6px;border-radius:8px;background:#888;color:#fff;vertical-align:middle}
.status.approved{background:var(--ok)}.status.declined{background:var(--bad)}.status.pending{background:var(--info)}
details ul{max-height:240px;overflow:auto;font-size:.8rem;padding-left:20px;word-break:break-word}li.other{opacity:.6}
.actions{margin-top:8px}
button{border:0;border-radius:6px;padding:8px 18px;margin-right:8px;font-size:.9rem;cursor:pointer;color:#fff;background:var(--info)}
button.small{padding:4px 10px;font-size:.8rem;margin:0}
.approve{background:var(--ok)}.decline,.danger{background:var(--bad)}button:disabled{opacity:.5;cursor:default}
.empty{opacity:.6}[hidden]{display:none!important}
.panel h2{margin:0 0 8px;font-size:1rem}
.row{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin:4px 0}
.row .grow{flex:1 1 200px;min-width:0;word-break:break-word}
progress{width:100%;height:14px;margin:6px 0}
.banner{background:var(--warnbg);color:var(--warn);border-radius:8px;padding:8px 12px;margin:8px 0;font-size:.9rem}
table.sources{width:100%;border-collapse:collapse;font-size:.85rem}
table.sources th,table.sources td{text-align:left;padding:4px 8px 4px 0;border-bottom:1px solid var(--line);vertical-align:top}
.sub{font-size:.8rem;opacity:.75}
.nav{display:flex;gap:6px;margin:0 0 12px}.nav a{padding:6px 14px;border-radius:16px;text-decoration:none;color:var(--fg);background:var(--card);box-shadow:0 1px 3px rgba(0,0,0,.15);font-size:.9rem}.nav a.on{background:var(--info);color:#fff}
.entry{display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--line)}
.entry .name{flex:1 1 220px;min-width:0;word-break:break-word}
.entry .name button{background:none;color:var(--info);padding:0;margin:0;text-align:left;font-size:.95rem}
.feedlink,.feedlink:visited{color:var(--info);word-break:break-word;text-decoration:none}.feedlink:hover{text-decoration:underline}
.feed-done{opacity:.55}
.cols{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:0 16px;align-items:start}
@media(max-width:900px){.cols{grid-template-columns:minmax(0,1fr)}.col-feed{order:-1}}
.panel .card{background:none;box-shadow:none;border-radius:0;margin:0;padding:8px 0;border-bottom:1px solid var(--line)}
.panel .card:last-child{border-bottom:0}
.lib-item .children{margin:0 0 6px 14px;padding-left:10px;border-left:2px solid var(--line)}
.entry .name button.toggle{cursor:pointer;font-weight:600}
</style>@@GUARD@@</head><body>
<h1>Car Media and Trips</h1>
<nav class="nav"><a class="on" href="./">Media</a><a href="trips">Trips</a><a href="regions">Regions</a></nav>

<div class="cols">
<div class="col-main">
<div class="panel" id="statusbar">
<h2>Status</h2>
<div id="activity-line">Loading status&hellip;</div>
<progress id="activity-progress" hidden></progress>
<div class="row"><span class="sub grow" id="activity-detail"></span><button class="danger small" id="stop-btn" hidden>Stop</button></div>
<div class="banner" id="space-warning" hidden></div>
<div class="sub" id="disk-line"></div>
<div id="sources"></div>
<div id="sync"></div>
<div class="error" id="status-error" hidden></div>
</div>

<div class="panel" id="dl-box" hidden>
<h2>Browser downloads</h2>
<div id="dl-list"></div>
</div>

<div class="panel" id="pending-box">
<h2>Pending review (<span id="npending">@@NPENDING@@</span>)</h2>
<div id="pending" data-count="@@NPENDING@@">
@@PENDING@@
</div>
</div>

<div class="panel" id="decided-box">
<h2>Recently decided</h2>
<div id="decided">
@@DECIDED@@
</div>
</div>

<div class="panel" id="library-box">
<h2>Library</h2>
<div id="library"></div>
<div class="error" id="library-error" hidden></div>
</div>
</div>

<div class="col-feed">
<div class="panel" id="feed-box">
<h2>RSS feed</h2>
<div class="row"><span class="sub grow" id="feed-line">Loading feed&hellip;</span><button class="small" id="feed-refresh">Refresh</button></div>
<div id="feed"></div>
</div>
</div>
</div>

<script>
(function(){
'use strict';
var FAST = 1500, SLOW = 8000;
var lastKeys = {}, lastLibRev = null, lastState = null, lastPending = null, timer = null, refreshing = false;

function $(id){ return document.getElementById(id); }
function el(tag, cls, text){
  var n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = String(text);
  return n;
}
function clear(n){ while (n.firstChild) n.removeChild(n.firstChild); }
function fmtBytes(n){
  if (n === null || n === undefined || isNaN(n)) return '?';
  var units = ['B','KB','MB','GB','TB'], v = Number(n), i = 0;
  while (v >= 1024 && i < units.length - 1){ v /= 1024; i++; }
  return (i === 0 ? String(Math.round(v)) : v.toFixed(1)) + ' ' + units[i];
}
function fmtTime(ts){ return ts ? new Date(ts * 1000).toLocaleString() : '-'; }
function changed(key, data){
  var s = JSON.stringify(data);
  if (lastKeys[key] === s) return false;
  lastKeys[key] = s;
  return true;
}
function api(url, opts){
  return fetch(url, opts).then(function(r){
    return r.json().catch(function(){ return {}; }).then(function(j){
      if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
      return j;
    });
  });
}
function post(url, body){
  var opts = {method: 'POST'};
  if (body !== undefined){
    opts.headers = {'Content-Type': 'application/json'};
    opts.body = JSON.stringify(body);
  }
  return api(url, opts);
}
function showError(id, err){
  var n = $(id);
  if (!err){ n.hidden = true; n.textContent = ''; return; }
  n.textContent = String(err.message || err);
  n.hidden = false;
}

/* ---------------------------------------------------------------- status */

function renderActivity(a){
  var line = 'Idle', detail = [], prog = $('activity-progress'), stop = $('stop-btn');
  var total = a.total, got = a.received;
  switch (a.state){
    case 'polling': line = 'Checking ' + (a.source || 'sources') + '...'; break;
    case 'downloading':
      line = 'Downloading' + (a.file ? ' ' + a.file : '') + (a.source ? ' from ' + a.source : '');
      break;
    case 'processing': line = 'Processing' + (a.release ? ' ' + a.release : '') + '...'; break;
    case 'indexing': line = 'Indexing the library (first start only)...'; break;
  }
  if (a.state === 'downloading' && a.release) detail.push(a.release);
  if (a.state === 'downloading' && got !== null && got !== undefined){
    var txt = fmtBytes(got);
    if (total){ txt += ' of ' + fmtBytes(total) + ' (' + Math.min(100, Math.round(got * 100 / total)) + '%)'; }
    detail.push(txt);
  }
  $('activity-line').textContent = line;
  $('activity-detail').textContent = detail.join(' - ');
  if (a.state === 'downloading' && total){
    prog.max = total; prog.value = Math.min(got || 0, total); prog.hidden = false;
  } else if (a.state === 'downloading' || a.state === 'indexing' || a.state === 'processing'){
    prog.removeAttribute('value'); prog.removeAttribute('max'); prog.hidden = false;
  } else {
    prog.hidden = true;
  }
  stop.hidden = a.state !== 'downloading';
}

function renderDisk(d){
  var line = 'Free space: ' + fmtBytes(d.free_bytes) + ' (minimum ' + fmtBytes(d.min_free_bytes) + ')';
  $('disk-line').textContent = line;
  var w = $('space-warning');
  if (d.waiting_for_space){
    var low = d.free_bytes === null || d.free_bytes === undefined || d.free_bytes < d.min_free_bytes;
    if (low){
      w.textContent = 'Waiting for free space: downloads are paused until at least ' +
        fmtBytes(d.min_free_bytes) + ' stay free.';
    } else {
      // enough is free for ordinary downloads: one big release waits for more, the others go on
      w.textContent = 'Waiting for free space: ' + (d.waiting_release ? d.waiting_release : 'a release') +
        ' needs ' + (d.needed_bytes ? 'at least ' + fmtBytes(d.needed_bytes) + ' free' : 'more free space') +
        ' before it is fetched; other releases continue.';
    }
    w.hidden = false;
  } else { w.hidden = true; }
}

function renderSources(list){
  var box = $('sources');
  clear(box);
  if (!list.length) return;
  var table = el('table', 'sources'), head = el('tr');
  ['Source', 'Last check', 'Next check', 'Result', ''].forEach(function(h){ head.appendChild(el('th', null, h)); });
  table.appendChild(head);
  list.forEach(function(s){
    var tr = el('tr'), first = el('td');
    first.appendChild(el('div', null, s.name));
    var sub = [s.type === s.name ? '' : s.type];  /* the type only when it says more than the name */
    if (s.auto_approve) sub.push('auto-approve');
    if (s.waiting_for_space) sub.push('waiting for space');
    var subText = sub.filter(Boolean).join(' - ');
    if (subText) first.appendChild(el('div', 'sub', subText));
    tr.appendChild(first);
    tr.appendChild(el('td', null, fmtTime(s.last_poll_at)));
    tr.appendChild(el('td', null, fmtTime(s.next_poll_at)));
    tr.appendChild(el('td', s.error ? 'error' : null, s.error || s.last_result || '-'));
    var act = el('td'), btn = el('button', 'small', 'Check now');
    btn.addEventListener('click', function(){
      btn.disabled = true;
      post('api/sources/' + encodeURIComponent(s.name) + '/poll')
        .then(function(){ showError('status-error', null); schedule(200); })
        .catch(function(err){ showError('status-error', err); btn.disabled = false; });
    });
    act.appendChild(btn);
    tr.appendChild(act);
    table.appendChild(tr);
  });
  box.appendChild(table);
}

function renderSync(s, problem){
  var box = $('sync');
  clear(box);
  var line;
  if (!s.enabled) line = 'Sync API: disabled';
  else if (s.listening) line = 'Sync API: listening on port ' + s.port + ', ' + (s.files || 0) + ' files';
  else line = 'Sync API: not listening' + (s.reason ? ' (' + s.reason + ')' : '');
  box.appendChild(el('div', 'sub', line));
  function confirmButton(question){
    var b = el('button', 'small', 'Confirm');
    b.addEventListener('click', function(){
      if (!confirm(question)) return;
      b.disabled = true;
      post('api/sync/confirm-empty')
        .then(function(){ showError('status-error', null); schedule(200); })
        .catch(function(err){ showError('status-error', err); b.disabled = false; });
    });
    return b;
  }
  var syncBanner = false;
  if (s.paused_empty){
    syncBanner = true;
    var w = el('div', 'banner');
    w.appendChild(el('div', null, 'The library is unexpectedly empty. The sync API is paused so clients do not ' +
      'delete their copies. If you emptied it on purpose, confirm.'));
    w.appendChild(confirmButton('Confirm that the library is empty on purpose? The sync API will resume and clients will remove their copies.'));
    box.appendChild(w);
  } else if (s.listening && s.error){
    syncBanner = true;
    var e = el('div', 'banner');
    e.appendChild(el('div', null, 'The sync API answers 503 (clients keep their copies): ' + s.error +
      '. Check library_dir and the storage behind it.'));
    // Confirm only helps when the folder is missing (new, or moved on purpose): it lifts the guard, so a
    // folder that cannot be read (share down, permission or I/O error) must be fixed, not confirmed.
    if (/No such file or directory|Not a directory|is not a folder/i.test(s.error)){
      e.appendChild(el('div', 'sub', 'If library_dir is right and the folder is new or meant to be empty, confirm: ' +
        'approvals then create and fill it, and clients remove copies of files that are not in it.'));
      e.appendChild(confirmButton('Is this library new or meant to be empty (' + s.error + ')? Approvals will then ' +
        'create and fill it, and the car app removes every synced file that is not in it. Do not confirm while ' +
        'a network share is only unmounted.'));
    } else {
      e.appendChild(el('div', 'sub', 'This is not fixed by confirming: restore access to the folder (the share, ' +
        'its credentials or permissions); the sync API recovers by itself.'));
    }
    box.appendChild(e);
  }
  if (problem && !syncBanner){
    // approvals are refused although the sync API shows nothing (disabled, not listening, or its listing
    // is not rebuilt yet): the user still needs to see why, and to be able to confirm
    var p = el('div', 'banner');
    p.appendChild(el('div', null, 'Approvals are on hold: ' + problem + '.'));
    if (!/cannot be read/i.test(problem)){
      p.appendChild(el('div', 'sub', 'If the library is new, moved or meant to be empty, confirm: approvals then ' +
        'create and fill it, and sync clients remove copies of files that are not in it.'));
      p.appendChild(confirmButton('Is this library new, moved or meant to be empty (' + problem + ')? Approvals ' +
        'will then fill it, and sync clients remove every synced file that is not in it. Do not confirm while ' +
        'a network share is only unmounted.'));
    } else {
      p.appendChild(el('div', 'sub', 'Restore access to the folder; approvals continue by themselves.'));
    }
    box.appendChild(p);
  }
  var rej = s.rejected || [];
  if (rej.length){
    var det = el('details'), total = s.rejected_total || rej.length;
    det.appendChild(el('summary', null, 'Names the sync API does not serve (' + total + ')'));
    var ul = el('ul');
    rej.forEach(function(r){ ul.appendChild(el('li', null, r.name + ' - ' + r.reason)); });
    if (total > rej.length) ul.appendChild(el('li', 'sub', '... and ' + (total - rej.length) + ' more'));
    det.appendChild(ul);
    box.appendChild(det);
  }
}

function renderStatus(s){
  var a = s.activity || {};
  renderActivity(a);
  if (changed('disk', s.disk || {})) renderDisk(s.disk || {});
  if (changed('sources', s.sources || [])) renderSources(s.sources || []);
  if (changed('sync', [s.sync || {}, s.library_problem || null])) renderSync(s.sync || {}, s.library_problem || null);
  showError('status-error', null);
  var pend = s.pending_count;
  if ((lastPending !== null && pend !== lastPending) || (lastState && lastState !== 'idle' && a.state === 'idle')){
    refreshReviews();
  }
  if (s.library_rev !== undefined && s.library_rev !== lastLibRev){ lastLibRev = s.library_rev; loadLibrary(); }  /* approval, deletion: list again */
  lastPending = pend;
  lastState = a.state;
  return a.state !== 'idle';
}

function schedule(ms){
  if (timer) clearTimeout(timer);
  timer = setTimeout(tick, ms);
}
function tick(){
  api('api/status').then(function(s){
    schedule(renderStatus(s) ? FAST : SLOW);
  }).catch(function(err){
    showError('status-error', err);
    schedule(SLOW);
  });
}

/* --------------------------------------------------------------- reviews */

function refreshReviews(){
  if (refreshing) return;
  refreshing = true;
  fetch('./').then(function(r){ return r.text(); }).then(function(text){
    var doc = new DOMParser().parseFromString(text, 'text/html');
    ['pending', 'decided'].forEach(function(id){
      var src = doc.getElementById(id), dst = $(id);
      if (!src || !dst) return;
      clear(dst);
      Array.prototype.slice.call(src.childNodes).forEach(function(node){
        dst.appendChild(document.importNode(node, true));
      });
    });
    var n = doc.getElementById('npending');
    if (n) $('npending').textContent = n.textContent;
  }).catch(function(){}).then(function(){ refreshing = false; });
}

document.addEventListener('click', function(ev){
  var btn = ev.target.closest ? ev.target.closest('.card .actions button') : null;
  if (!btn) return;
  var card = btn.closest('.card'), id = card.dataset.id;
  var action = btn.classList.contains('approve') ? 'approve' : 'decline';
  card.querySelectorAll('button').forEach(function(b){ b.disabled = true; });
  fetch('api/releases/' + encodeURIComponent(id) + '/' + action, {method: 'POST'}).then(function(r){
    if (r.ok) return null;
    return r.json().catch(function(){ return {}; }).then(function(j){ alert(j.error || ('HTTP ' + r.status)); });
  }).catch(function(err){ alert(err); }).then(function(){
    refreshing = false;
    refreshReviews();
    loadLibrary();
    schedule(200);
  });
});

$('stop-btn').addEventListener('click', function(){
  var b = $('stop-btn');
  b.disabled = true;
  post('api/stop').catch(function(err){ showError('status-error', err); }).then(function(){
    b.disabled = false;
    schedule(300);
  });
});

/* --------------------------------------------------------------- library */

var openLib = {}; /* folders that are open (path -> true): they stay open when the library is listed again */

function libInfo(e){
  return fmtBytes(e.size) + (e.is_dir ? ', ' + (e.files || 0) + ' audio files' : '') + ' - ' + fmtTime(e.mtime);
}

function renderEntries(box, entries, depth){
  entries.forEach(function(e){
    var item = el('div', 'lib-item');
    var row = el('div', 'entry');
    var name = el('div', 'name');
    var kids = null, toggle = null, loaded = false;
    if (e.is_dir){
      toggle = el('button', 'toggle');
      name.appendChild(toggle);
    } else {
      name.textContent = e.name;
    }
    row.appendChild(name);
    row.appendChild(el('span', 'sub', libInfo(e)));
    if (depth === 0){
      var del = el('button', 'danger small', 'Delete');
      del.addEventListener('click', function(){
        var msg = 'Delete "' + e.name + '"' + (e.is_dir ? ' and everything in it' : '') + '? This cannot be undone.';
        if (!confirm(msg)) return;
        del.disabled = true;
        post('api/library/delete', {path: e.path})
          .then(function(){ delete openLib[e.path]; loadLibrary(); schedule(300); })
          .catch(function(err){ showError('library-error', err); del.disabled = false; });
      });
      row.appendChild(del);
    }
    item.appendChild(row);
    if (e.is_dir){
      kids = el('div', 'children');
      item.appendChild(kids);
      var label = function(){ toggle.textContent = (openLib[e.path] ? '\u25BE ' : '\u25B8 ') + e.name; kids.hidden = !openLib[e.path]; };
      var load = function(){
        loaded = true;
        clear(kids);
        kids.appendChild(el('p', 'sub', 'Loading...'));
        api('api/library?path=' + encodeURIComponent(e.path)).then(function(d){
          clear(kids);
          var list = d.entries || [];
          if (!list.length) kids.appendChild(el('p', 'sub', 'Empty.'));
          renderEntries(kids, list, depth + 1);
        }).catch(function(err){
          loaded = false;
          clear(kids);
          kids.appendChild(el('div', 'error', 'Could not list the folder: ' + (err && err.message ? err.message : err)));
        });
      };
      toggle.addEventListener('click', function(){
        openLib[e.path] = !openLib[e.path];
        label();
        if (openLib[e.path] && !loaded) load();
      });
      label();
      if (openLib[e.path]) load();
    }
    box.appendChild(item);
  });
}

function renderLibrary(data){
  var box = $('library');
  clear(box);
  showError('library-error', null);
  var entries = data.entries || [];
  if (!entries.length) box.appendChild(el('p', 'empty', 'The library is empty.'));
  renderEntries(box, entries, 0);
}

function loadLibrary(){
  api('api/library?path=').then(renderLibrary).catch(function(err){ showError('library-error', err); });
}

/* --------------------------------------------------------------- rss feed */

var browserOn = false; /* the embedded browser is an option (browser_enabled): off = items open in your own browser */
/* only http(s) links (absolute, or relative to this page) are ever put into an href */
function safeHref(u){
  if (typeof u !== 'string' || !u) return '';
  try { var p = new URL(u, location.href); return (p.protocol === 'http:' || p.protocol === 'https:') ? p.href : ''; } catch (e) { return ''; }
}
function loadFeed(){
  api('api/feed').then(function(d){
    browserOn = !!d.browser;
    renderFeed(d.sources || []);
  }).catch(function(err){
    $('feed-line').textContent = 'Could not read the feed: ' + err.message;
  });
}
function renderFeed(sources){
  var box = $('feed'), total = 0, done = 0;
  clear(box);
  if (!sources.length){
    $('feed-line').textContent = 'No RSS feed is configured (rss_feed_url).';
    return;
  }
  sources.forEach(function(src){
    if (src.error){
      box.appendChild(el('div', 'error', src.source + ': ' + src.error));
      return;
    }
    src.items.forEach(function(it){
      total++;
      if (it.handled) done++;
      var row = el('div', 'entry' + (it.handled ? ' feed-done' : ''));
      var a = el('a', 'feedlink', it.title || it.id);
      a.className = 'feedlink name';
      var link = safeHref(it.link);
      if (link && !browserOn){
        a.href = link; a.target = '_blank'; a.rel = 'noopener noreferrer nofollow';
      } else if (link){
        var viewerHref = 'viewer?src=' + encodeURIComponent(src.source) + '&item=' + encodeURIComponent(it.id) + '&url=' + encodeURIComponent(link) + '&n=';
        a.href = viewerHref + Date.now();
        /* the number is the time of the click, not of the list: a list kept by the Back button must not look older than the item opened before */
        a.addEventListener('click', function(){ a.href = viewerHref + Date.now(); });
      }
      row.appendChild(a);
      if (link && browserOn){
        var ext = el('a', 'feedlink sub', '\u2197'); /* the same page in your own browser */
        ext.href = link; ext.target = '_blank'; ext.rel = 'noopener noreferrer nofollow'; ext.title = 'Open in your own browser';
        row.appendChild(ext);
      }
      if (it.handled) row.appendChild(el('span', 'sub', 'done'));  /* set when the download of the item is complete */
      box.appendChild(row);
    });
  });
  $('feed-line').textContent = total + ' items' + (done ? ', ' + done + ' done' : '');
}
/* downloads of the add-on browser, so their progress is visible without its window */
function fmtEta(sec){
  if (sec < 60) return sec + ' s';
  if (sec < 3600) return Math.round(sec / 60) + ' min';
  return Math.floor(sec / 3600) + ' h ' + Math.round((sec % 3600) / 60) + ' min';
}
var lastDoneIds = null;
function loadDownloads(){
  api('api/browser/downloads').then(function(d){
    var list = d.downloads || [], box = $('dl-list');
    var doneIds = list.filter(function(x){ return x.state === 'done'; }).map(function(x){ return x.id; }).join(',');
    if (lastDoneIds !== null && doneIds !== lastDoneIds) loadFeed();  /* a finished download marks its feed item as done */
    lastDoneIds = doneIds;
    $('dl-box').hidden = !list.length;
    clear(box);
    list.forEach(function(it){
      var row = el('div', 'entry');
      row.appendChild(el('span', 'grow', it.name));
      if (it.state === 'running'){
        var bar = el('progress'); bar.max = 100; bar.value = it.total ? Math.round(100 * it.received / it.total) : 0;
        if (!it.total) bar.removeAttribute('value');
        row.appendChild(bar);
        var line = it.total
          ? fmtBytes(it.received) + ' / ' + fmtBytes(it.total) + ' (' + Math.round(100 * it.received / it.total) + '%)'
          : fmtBytes(it.received) + ' (size unknown)';
        if (it.speed) line += ' \u00b7 ' + fmtBytes(it.speed) + '/s';
        if (it.eta !== undefined) line += ' \u00b7 about ' + fmtEta(it.eta) + ' left';
        else if (it.stalled) line += ' \u00b7 no progress for a while';
        row.appendChild(el('span', 'sub', line));
        var stop = el('button', 'small', 'Stop');
        stop.addEventListener('click', function(){
          stop.disabled = true;
          post('api/browser/downloads/cancel', {id: it.id}).then(loadDownloads).catch(function(){ stop.disabled = false; });
        });
        row.appendChild(stop);
      } else if (it.state === 'done'){
        row.appendChild(el('span', 'sub', fmtBytes(it.received) + ' - saved; waiting for your review (never approved automatically)'));
      } else {
        row.appendChild(el('span', 'sub', it.state));
      }
      if (it.note) row.appendChild(el('span', 'sub', it.note));
      box.appendChild(row);
    });
  }).catch(function(){});
}
setInterval(loadDownloads, 3000);
loadDownloads();
$('feed-refresh').addEventListener('click', loadFeed);

tick();
loadFeed();
})();
</script>
</body></html>
"""

_PAGE = _PAGE.replace("@@GUARD@@", SESSION_GUARD_TAG)  # first script of the page: wraps fetch before any other code runs
_MARKER = re.compile(r"@@([A-Z]+)@@")


def render_index(pending: list[Any], decided: list[Any]) -> str:
    """Render the review page. All release-derived text is HTML-escaped."""
    pending_html = "\n".join(_render_card(r, True) for r in pending) or '<p class="empty">Nothing waiting for review.</p>'
    decided_html = "\n".join(_render_row(r) for r in decided) or '<p class="empty">No history yet.</p>'
    values = {"NPENDING": str(len(pending)), "PENDING": pending_html, "DECIDED": decided_html}
    return _MARKER.sub(lambda m: values.get(m.group(1), m.group(0)), _PAGE)


# ------------------------------------------------------------------ routes


def _dumps(data: Any) -> str:
    return json.dumps(data, default=str)


def _json(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=_dumps)


def _error(message: str, status: int) -> web.Response:
    return _json({"ok": False, "error": message}, status)


async def _guarded(what: str, call: Callable[[], Awaitable[Any]]) -> web.Response:
    """Run ``call`` and map exceptions to HTTP errors (ValueError 400, KeyError/FileNotFoundError 404)."""
    try:
        return _json(await call())
    except ValueError as exc:
        return _error(str(exc) or "invalid request", 400)
    except KeyError as exc:
        return _error(f"not found: {exc.args[0] if exc.args else ''}", 404)
    except FileNotFoundError:
        return _error("not found", 404)
    except Exception as exc:  # noqa: BLE001
        log.exception("%s failed", what)
        return _error(str(exc) or type(exc).__name__, 500)


async def index(request: web.Request) -> web.Response:
    db = request.app[_CONTROLLER].db
    pending = db.list(["pending"], limit=100)
    decided = db.list(DECIDED_STATUSES, limit=20)
    # Names may contain lone surrogates (undecodable file-name bytes, older rows): never 500.
    body = render_index(pending, decided).encode("utf-8", "replace")
    return web.Response(body=body, content_type="text/html", charset="utf-8")


async def api_list(request: web.Request) -> web.Response:
    raw = request.query.get("status")
    statuses = None
    if raw:
        statuses = [s.strip() for s in raw.split(",") if s.strip()]
        bad = [s for s in statuses if s not in STATUSES]
        if bad:
            return web.json_response({"ok": False, "error": f"unknown status: {', '.join(bad)}"}, status=400)
    releases = request.app[_CONTROLLER].db.list(statuses, limit=100)
    return web.json_response([_release_dict(r) for r in releases])


def _decision(action: str):
    async def handler(request: web.Request) -> web.Response:
        controller = request.app[_CONTROLLER]
        release_id = request.match_info["id"]
        try:
            release = await _maybe_await(getattr(controller, action)(release_id))
        except KeyError:
            return web.json_response({"ok": False, "error": f"unknown release {release_id}"}, status=404)
        except ValueError as exc:  # pipeline.InvalidState
            return web.json_response({"ok": False, "error": str(exc)}, status=409)
        except NoSpaceError as exc:  # the copy into the library would not fit; nothing was changed
            return web.json_response(
                {"ok": False, "error": f"not enough free space to move the release into the library: {exc}"},
                status=507,
            )
        except Exception as exc:
            log.exception("%s failed for %s", action, release_id)
            return web.json_response({"ok": False, "error": str(exc)}, status=500)
        return web.json_response({"ok": True, "status": release.status})

    return handler


async def api_status(request: web.Request) -> web.Response:
    controller = request.app[_CONTROLLER]
    # status_async probes the free space off the event loop (bounded); older controllers lack it
    status = getattr(controller, "status_async", None) or controller.status
    return await _guarded("status", lambda: _maybe_await(status()))


async def api_stop(request: web.Request) -> web.Response:
    controller = request.app[_CONTROLLER]

    async def call() -> dict[str, Any]:
        return {"ok": True, "stopped": bool(await _maybe_await(controller.stop_current()))}

    return await _guarded("stop", call)


async def api_poll(request: web.Request) -> web.Response:
    controller = request.app[_CONTROLLER]
    name = request.match_info["name"]

    async def call() -> dict[str, Any]:
        polled = await _maybe_await(controller.request_poll(name))
        return {"ok": True, "sources": list(polled)}

    return await _guarded("poll request", call)


async def api_library(request: web.Request) -> web.Response:
    controller = request.app[_CONTROLLER]
    rel = request.query.get("path", "")
    return await _guarded("library listing", lambda: _maybe_await(controller.library_listing(rel)))


async def api_library_delete(request: web.Request) -> web.Response:
    controller = request.app[_CONTROLLER]
    # JSON only: a cross-site form post cannot send this content type without a preflight.
    if request.content_type != "application/json":
        return _error("expected a JSON body", 400)
    try:
        body = await request.json()
    except ValueError:
        return _error("invalid JSON body", 400)
    rel = body.get("path") if isinstance(body, dict) else None
    if not isinstance(rel, str):
        return _error("'path' must be a string", 400)
    return await _guarded("library delete", lambda: _maybe_await(controller.delete_library_path(rel)))


async def api_confirm_empty(request: web.Request) -> web.Response:
    controller = request.app[_CONTROLLER]
    return await _guarded("confirm empty library", lambda: _maybe_await(controller.confirm_empty_library()))


def create_app(controller: Any, regions: Any = None, chargers: Any = None) -> web.Application:
    """Build the aiohttp application serving the ingress UI (``regions``: the RegionManager of the Regions page)."""
    app = web.Application(middlewares=[ip_middleware])
    app[_CONTROLLER] = controller
    app.router.add_get("/", index)
    app.router.add_get("/api/releases", api_list)
    app.router.add_post("/api/releases/{id}/approve", _decision("approve"))
    app.router.add_post("/api/releases/{id}/decline", _decision("decline"))
    app.router.add_get("/api/status", api_status)
    app.router.add_post("/api/stop", api_stop)
    app.router.add_post("/api/sources/{name}/poll", api_poll)
    app.router.add_get("/api/library", api_library)
    app.router.add_post("/api/library/delete", api_library_delete)
    app.router.add_post("/api/sync/confirm-empty", api_confirm_empty)
    register_trip_pages(app, lambda: controller.db)
    register_feed_pages(app, controller)
    register_region_pages(app, regions, chargers)
    register_browser_pages(app, controller)
    return app
