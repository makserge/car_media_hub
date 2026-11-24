"""The **Regions** page of the ingress UI and its admin API.

Everything lives below ``/regions`` (the page) and ``/regions/api/...``. Like the other admin POSTs, the API is only
reachable through ingress (``ip_middleware``) and needs the header ``X-Requested-With: car-media-hub`` (a cross-site
form cannot send it without a preflight), a JSON body where there is one, and no cross-site ``Sec-Fetch-Site`` / foreign
``Origin``. All URLs in the page are relative (it lives under the ingress path). Text from manifests is only put into
the page with ``textContent`` and never into markup.
"""
from __future__ import annotations

import html
import logging
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from aiohttp import web

from .build_info import build_info, footer_text
from .regions import FILE_NAMES, RegionError, RegionManager
from .trips_web import _page

log = logging.getLogger(__name__)

_MANAGER = web.AppKey("regions_web_manager", object)
_CHARGERS = web.AppKey("regions_web_chargers", object)
HEADER = "car-media-hub"
UPLOAD_CHUNK = 1 << 20


def _json(payload: dict[str, Any], status: int = 200) -> web.Response:
    return web.json_response(payload, status=status, headers={"Cache-Control": "no-store"})


def _fail(message: str, status: int) -> web.Response:
    return _json({"ok": False, "error": message}, status)


def _hostname(netloc: str) -> str:
    try:
        return (urlsplit("//" + netloc).hostname or "").lower()
    except ValueError:
        return ""


def check_admin_request(request: web.Request, *, json_body: bool = True) -> web.Response | None:
    """None when the request may change something, else the refusing response."""
    if request.headers.get("X-Requested-With") != HEADER:
        return _fail("expected a request from the page", 400)
    if json_body and request.content_type != "application/json":
        return _fail("expected a JSON body", 400)
    site = request.headers.get("Sec-Fetch-Site")
    if site == "cross-site":
        return _fail("cross-site requests are refused", 403)
    origin = request.headers.get("Origin")
    if origin and site not in ("same-origin", "same-site"):  # an old browser: compare the host names
        wanted = _hostname(urlsplit(origin).netloc)
        known = {_hostname(request.headers.get("X-Forwarded-Host", "").split(",")[0].strip()), _hostname(request.host or "")}
        if wanted and wanted not in known:
            return _fail("the Origin of the request does not match", 403)
    return None


async def _body(request: web.Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except ValueError:
        raise RegionError("invalid JSON body") from None
    if not isinstance(data, dict):
        raise RegionError("the body must be a JSON object")
    return data


def _manager(request: web.Request) -> RegionManager:
    manager = request.app.get(_MANAGER)
    if manager is None:
        raise RegionError("regions are not available", 503)
    return manager


Handler = Callable[[web.Request, RegionManager], Awaitable[dict[str, Any]]]


def _admin(handler: Handler, *, json_body: bool = True) -> Callable[[web.Request], Awaitable[web.Response]]:
    async def wrapped(request: web.Request) -> web.Response:
        refused = check_admin_request(request, json_body=json_body)
        if refused is not None:
            return refused
        try:
            return _json({"ok": True, **await handler(request, _manager(request))})
        except RegionError as exc:
            return _fail(str(exc), exc.status)
        except Exception:  # noqa: BLE001
            log.exception("Regions page: %s failed", request.path)
            return _fail("internal error (see the add-on log)", 500)

    return wrapped


def _fields(data: dict[str, Any]) -> dict[str, Any]:
    name = data.get("name")
    if name is None:
        name = {"de": data.get("name_de"), "en": data.get("name_en")}
    bounds = data.get("bounds")
    if isinstance(bounds, str):  # "west,south,east,north" typed into a form field
        try:
            bounds = [float(p) for p in bounds.replace(";", ",").split(",") if p.strip()]
        except ValueError:
            raise RegionError("bounds must be four numbers: west, south, east, north") from None
    return {
        "name": name, "bounds": bounds, "version": data.get("version"), "source": data.get("source"),
        "license": data.get("license"), "routingGroup": data.get("routingGroup"),
    }


async def _list(request: web.Request) -> web.Response:
    try:
        chargers = request.app.get(_CHARGERS)
        return _json({"ok": True, **await _manager(request).overview(), "build": build_info(),
                      "chargers": chargers.status() if chargers is not None else None})
    except RegionError as exc:
        return _fail(str(exc), exc.status)


async def _rescan(request: web.Request, manager: RegionManager) -> dict[str, Any]:
    await manager.rescan()
    return {}


async def _chargers_refresh(request: web.Request) -> web.Response:
    refused = check_admin_request(request, json_body=False)
    if refused is not None:
        return refused
    chargers = request.app.get(_CHARGERS)
    if chargers is None:
        return _fail("the charger refresh is not available", 503)
    started = chargers.trigger()
    return _json({"ok": True, "started": started, "chargers": chargers.status()})


async def _create(request: web.Request, manager: RegionManager) -> dict[str, Any]:
    data = await _body(request)
    manifest = await manager.create_region(str(data.get("id", "")), _fields(data))
    return {"manifest": manifest}


async def _edit(request: web.Request, manager: RegionManager) -> dict[str, Any]:
    manifest = await manager.update_manifest(request.match_info["id"], _fields(await _body(request)))
    return {"manifest": manifest}


async def _delete(request: web.Request, manager: RegionManager) -> dict[str, Any]:
    await manager.delete_region(request.match_info["id"])
    return {}


async def _delete_file(request: web.Request, manager: RegionManager) -> dict[str, Any]:
    await manager.delete_file(request.match_info["id"], request.match_info["kind"])
    return {}


async def _upload(request: web.Request, manager: RegionManager) -> dict[str, Any]:
    size = await manager.save_upload(
        request.match_info["id"], request.match_info["kind"], request.content.iter_chunked(UPLOAD_CHUNK), request.content_length)
    return {"size": size}


async def _import(request: web.Request, manager: RegionManager) -> dict[str, Any]:
    data = await _body(request)
    task = manager.start_import(
        request.match_info["id"], str(data.get("kind", "")), data.get("url"), str(data.get("sha256") or ""))
    return {"task": task.public()}


async def _discard_part(request: web.Request, manager: RegionManager) -> dict[str, Any]:
    await manager.discard_part(request.match_info["id"], request.match_info["kind"])
    return {}


async def _forget(request: web.Request, manager: RegionManager) -> dict[str, Any]:
    await manager.forget_device(request.match_info["did"])
    return {}


async def _forget_all(request: web.Request, manager: RegionManager) -> dict[str, Any]:
    return {"forgotten": await manager.forget_all_devices()}


async def _cancel(request: web.Request, manager: RegionManager) -> dict[str, Any]:
    return {"cancelled": manager.cancel_task(request.match_info["tid"])}


async def _wanted(request: web.Request, manager: RegionManager) -> dict[str, Any]:
    data = await _body(request)
    want = await manager.set_wanted(request.match_info["id"], data.get("wanted"), data.get("by"))
    return {"wanted": want["wanted"], "wantedAt": want["wantedAt"]}


async def _install_many(request: web.Request, manager: RegionManager) -> dict[str, Any]:
    """``{"ids": [...]}`` (in this order) or ``{"all": true}``: several install requests accepted at once."""
    data = await _body(request)
    if data.get("all") is True:
        return {"requested": await manager.install_all(data.get("by"))}
    return {"requested": await manager.set_wanted_many(data.get("ids"), data.get("by"))}


async def _remove(request: web.Request, manager: RegionManager) -> dict[str, Any]:
    data = await _body(request)
    want = await manager.request_remove(request.match_info["id"], data.get("by"))
    return {"removeRequested": want["removeRequested"], "removeAt": want["removeAt"]}


async def _page_handler(request: web.Request) -> web.Response:
    footer = f'<p class="sub" id="build" style="color:gray;font-size:.75rem">{html.escape(footer_text(build_info()))}</p>'
    body = _page("Regions", "./", "regions", _BODY + footer, head=f"<style>{_REGION_CSS}</style>", scripts=f"<script>{_JS}</script>")
    return web.Response(body=body.encode("utf-8"), content_type="text/html", charset="utf-8",
                        headers={"Cache-Control": "no-store"})


def register_region_pages(app: web.Application, manager: RegionManager | None, chargers: Any = None) -> None:
    if manager is not None:
        app[_MANAGER] = manager
    if chargers is not None:
        app[_CHARGERS] = chargers
    r = app.router
    r.add_get("/regions", _page_handler)
    r.add_get("/regions/api/list", _list)
    r.add_post("/regions/api/rescan", _admin(_rescan))
    r.add_post("/regions/api/chargers/refresh", _chargers_refresh)
    r.add_post("/regions/api/devices/forget-all", _admin(_forget_all))
    r.add_post("/regions/api/devices/{did}/forget", _admin(_forget))
    r.add_post("/regions/api/create", _admin(_create))
    r.add_post("/regions/api/{id}/manifest", _admin(_edit))
    r.add_post("/regions/api/{id}/delete", _admin(_delete))
    r.add_post("/regions/api/install", _admin(_install_many))
    r.add_post("/regions/api/{id}/wanted", _admin(_wanted))
    r.add_post("/regions/api/{id}/remove", _admin(_remove))
    r.add_post("/regions/api/{id}/files/{kind}/delete", _admin(_delete_file))
    r.add_put("/regions/api/{id}/upload/{kind}", _admin(_upload, json_body=False))
    r.add_post("/regions/api/{id}/parts/{kind}/discard", _admin(_discard_part))
    r.add_post("/regions/api/{id}/import", _admin(_import))
    r.add_post("/regions/api/tasks/{tid}/cancel", _admin(_cancel))


_REGION_CSS = """
.rg{background:var(--card);border-radius:10px;padding:10px 14px;margin:12px 0;box-shadow:0 1px 3px rgba(0,0,0,.15)}
.rg h3{margin:0 0 4px;font-size:1rem}.rg .meta{font-size:.8rem;opacity:.75;word-break:break-word}
.rg table{margin:8px 0}.rg td,.rg th{padding:4px 8px}
.rg input[type=text],.rg input:not([type]),.rg input[type=url]{font:inherit;font-size:.85rem;padding:4px 6px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg);max-width:100%}
.rg details{margin:6px 0}.rg summary{cursor:pointer;font-size:.9rem}
.rg .form{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:6px 12px;margin:8px 0}
.rg .form label{display:flex;flex-direction:column;font-size:.75rem;gap:2px}
.car{margin:8px 0;padding:6px 8px;border-left:3px solid var(--line);font-size:.85rem}
.car .ln{margin:2px 0;word-break:break-word}.car button{margin-right:6px}
.dev{font-size:.8rem}.dev td,.dev th{padding:2px 8px}
.prog{width:120px;height:8px;vertical-align:middle}
.err{color:var(--bad);font-size:.85rem;word-break:break-word}.okmsg{color:var(--ok);font-size:.85rem}
"""

_BODY = """
<p class="sub">Offline map regions for the car app. Files are served on the sync API (port 8100, same token) at
<code>/api/regions/catalogue</code>; the app downloads what you enable there. Prepare a pack on a PC
(<code>tools/build_region_pack.py</code>) and copy the folder to <code id="root">the regions folder</code>, or upload and import here.
Folders copied by hand appear after <b>Rescan</b> (automatic every 10 minutes); a file is offered once it has not changed for 30 seconds.
Unfinished uploads and imports older than 24 hours are deleted automatically.</p>
<div class="tools"><button id="rescan">Rescan</button><button id="installall" title="Request the install of every region that has a map; the car app installs one region at a time, in this order">Install all</button><span id="problem" class="err"></span><span id="status" class="sub"></span></div>
<details class="panel"><summary><b>New region</b></summary>
<div class="form">
<label>Id (a-z, 0-9, _ -)<input id="n-id"></label>
<label>Name (English)<input id="n-en"></label>
<label>Name (German)<input id="n-de"></label>
<label>Bounds west,south,east,north<input id="n-bounds" placeholder="11.6,48.8,13.0,50.0"></label>
<label>Version<input id="n-version" placeholder="2026-11"></label>
<label>Routing group (optional)<input id="n-group"></label>
<label>Source<input id="n-source" placeholder="Geofabrik oberpfalz-latest.osm.pbf"></label>
<label>License / attribution<input id="n-license" placeholder="Data (c) OpenStreetMap contributors, ODbL"></label>
</div><button id="create">Create region</button> <span id="create-msg" class="err"></span></details>
<div class="rg" id="chargers"><b>Charger database</b> <span id="ch-line" class="sub"></span> <button id="ch-refresh" title="Download the Bundesnetzagentur register now and rebuild the charger database for the car app">Refresh now</button>
<div id="ch-err" class="err"></div></div>
<div id="tasks"></div>
<details class="panel" id="devbox"><summary><b>Car app devices</b> <span id="devcount" class="sub"></span></summary>
<div id="devices"></div></details>
<div id="regions"><p class="empty">Loading...</p></div>
"""

_JS = r"""
(function(){
var HDR={'X-Requested-With':'car-media-hub'},timer=null,openForms={};
function $(id){return document.getElementById(id);}
function el(tag,attrs,text){var e=document.createElement(tag);if(attrs)for(var k in attrs)e.setAttribute(k,attrs[k]);if(text!=null)e.textContent=text;return e;}
function size(n){if(n==null)return'';var u=['B','KB','MB','GB'],i=0;while(n>=1024&&i<3){n/=1024;i++;}return(i?n.toFixed(1):n)+' '+u[i];}
function post(url,body){var h=Object.assign({'Content-Type':'application/json'},HDR);
return fetch(url,{method:'POST',headers:h,body:JSON.stringify(body||{})}).then(function(r){return r.json().catch(function(){return{ok:false,error:'HTTP '+r.status};});});}
function done(j,msgEl){if(!j.ok){if(msgEl)msgEl.textContent=j.error||'failed';else alert(j.error||'failed');}else if(msgEl)msgEl.textContent='';load();}
function stateText(f){if(!f.present)return'-';if(f.state==='ready')return'ready, sha256 '+f.sha256.slice(0,12)+'...';
if(f.state==='settling')return'waiting for the copy to finish';return'hashing '+Math.round((f.progress||0)*100)+'%';}
function gzText(f){var g=f.transfer;if(!g)return'';if(g.state==='ready')return', compressed '+size(g.size);if(g.state==='building')return', compressing '+Math.round((g.progress||0)*100)+'%';return', sent uncompressed (does not compress)';}
var DE=/^de/i.test(navigator.language||'');
var T=DE?{moment:'gerade eben',min:function(n){return'vor '+n+' Min.';},h:function(n){return'vor '+n+' Std.';},days:function(n){return'vor '+n+' Tagen';},
paused:function(a,x){return'pausiert – zuletzt gesehen '+a+(x?', '+x:'')+' (läuft weiter, sobald die Auto-App wieder läuft)';},
cancelled:'Installation abgebrochen',cancelWait:function(a){return'Installation abgebrochen (die App hat sich noch nicht gemeldet, zuletzt gesehen '+a+')';},
of:function(a,b){return size(a)+' von '+size(b);},seen:'zuletzt gesehen ',
cancelNote:'Installation abgebrochen: die Auto-App beendet sie beim nächsten Melden; fertige Dateien bleiben.',
count:function(n,m){return n+' von '+m+' Geräten installiert';},
pos:function(n){return'Platz '+n+' in der Warteschlange';},
doneLine:function(n,m){return'Auf der Auto-App installiert ('+n+' von '+m+' Geräten)';},
updLine:function(n){return'Update ausstehend ('+T.pos(n)+')';},cancelReq:'Anfrage zurücknehmen',
updDev:function(n){return'Update ausstehend'+(n?' ('+T.pos(n)+')':'');},
waitShared:function(a,b){return'wartet auf die gemeinsame Routing-Datei ('+a+' von '+b+' geladen)';},mn:'Min.',hr:'Std.',
left:function(m){return'noch ca. '+m;},starts:function(m,p){return'wartet'+(p?' (Platz '+p+')':'')+', startet in ca. '+m+' (nach den vorherigen)';},
sum:function(n,m,g,r){return'alle '+n+' Regionen in der Warteschlange: etwa '+m+' ('+g+' übrig bei '+r+')';},
measuring:'misst die Geschwindigkeit …',idleSum:'nichts zu installieren',pausedSum:'pausiert',
note:'Schätzung, hängt vom WLAN ab und davon, dass das Auto wach ist.',run:'läuft: ',queue:'wartet: ',nothing:'keine Installationen offen',installAll:'Alle Regionen mit Karte zur Installation anfordern?'}:
{moment:'a moment ago',min:function(n){return n+' min ago';},h:function(n){return n+' h ago';},days:function(n){return n+' days ago';},
paused:function(a,x){return'paused — last seen '+a+(x?', '+x:'')+' (continues when the car app runs again)';},
cancelled:'install cancelled',cancelWait:function(a){return'install cancelled (the app has not checked in yet, last seen '+a+')';},
of:function(a,b){return size(a)+' of '+size(b);},seen:'last seen ',
cancelNote:'Install cancelled: the car app stops it the next time it checks in; completed files stay.',
count:function(n,m){return n+' of '+m+' devices installed';},
pos:function(n){return'queue position '+n;},
doneLine:function(n,m){return'Installed on the car app ('+n+' of '+m+' devices)';},
updLine:function(n){return'Update pending ('+T.pos(n)+')';},cancelReq:'Cancel request',
updDev:function(n){return'update pending'+(n?' ('+T.pos(n)+')':'');},
waitShared:function(a,b){return'waiting for the shared routing file ('+a+' of '+b+' done)';},mn:'min',hr:'h',
left:function(m){return'about '+m+' left';},starts:function(m,p){return'queued'+(p?' ('+T.pos(p)+')':'')+', ~'+m+' after the ones before';},
sum:function(n,m,g,r){return'all '+n+' queued regions: about '+m+' ('+g+' left at '+r+')';},
measuring:'measuring…',idleSum:'nothing to install',pausedSum:'paused',
note:'Estimate, depends on Wi-Fi and the car being awake.',run:'running: ',queue:'queued: ',nothing:'no installs pending',installAll:'Request the install of every region that has a map?'};
function dur(m){if(m<120)return m+' '+T.mn;var h=Math.floor(m/60),r=m%60;return h+' '+T.hr+(r?' '+r+' '+T.mn:'');}
function ago(s){if(s<90)return T.moment;if(s<5400)return T.min(Math.round(s/60));if(s<172800)return T.h(Math.round(s/3600));return T.days(Math.round(s/86400));}
function devName(d){return d.name||d.deviceId.slice(0,8);}
function devLine(r,d){var e=null;d.regions.forEach(function(x){if(x.id===r.id)e=x;});var who=devName(d)+': ';
var wait='waiting for the app to check in (last seen '+ago(d.ageSeconds)+')';
if(!e)return r.wanted||r.removeRequested?who+wait:null;
var ph=e.phase||e.state,part=e.totalBytes?T.of(e.doneBytes||0,e.totalBytes):'';
if(ph==='cancelled')return who+(e.cancelAcknowledged?T.cancelled:T.cancelWait(ago(e.ageSeconds)));
if(ph==='paused')return who+T.paused(ago(e.ageSeconds),part);
if(e.state==='installed')return who+'installed'+(e.version?' '+e.version:'')+(e.updatePending?', '+T.updDev(e.queuePosition):'');
if(e.state==='downloading'&&e.waitingForShared&&e.sharedRouting)return who+T.waitShared(size(e.sharedRouting.doneBytes),size(e.sharedRouting.bytes));
if(e.state==='downloading'){var x=[];if(e.totalBytes)x.push(T.of(e.doneBytes||0,e.totalBytes));
if(e.bytesPerSecond)x.push(size(e.bytesPerSecond)+'/s');if(e.etaMinutes)x.push(T.left(dur(e.etaMinutes)));
return who+'downloading '+Math.round(e.progress)+' %'+(x.length?' ('+x.join(', ')+')':'');}
if(e.state==='queued')return who+(e.startsInSeconds!=null&&e.queuePosition?T.starts(dur(Math.max(1,Math.round(e.startsInSeconds/60))),e.queuePosition):'queued'+(e.queuePosition?' ('+T.pos(e.queuePosition)+')':''));
if(e.state==='failed')return who+'failed'+(e.message?': '+e.message:'');
if(r.wanted)return who+wait;
if(r.removeRequested)return who+(e.state==='removed'?'removed':'not installed')+' (last seen '+ago(d.ageSeconds)+')';
return null;}
function carBox(r,devs,msg){var box=el('div',{'class':'car'});
if(r.removeRequested)box.appendChild(el('div',{'class':'ln'},'Removal from the car app requested'+(r.removeAt?' ('+r.removeAt.slice(0,16).replace('T',' ')+' UTC)':'')+'; the app removes it the next time it checks in.'));
else if(r.done)box.appendChild(el('div',{'class':'ln'},T.doneLine(r.devicesInstalled,r.devicesTotal)));
else if(r.wanted){box.appendChild(el('div',{'class':'ln'},r.updatePending?T.updLine(r.installOrder):'Wanted on the car app ('+T.pos(r.installOrder)+')'+(r.ready?'.':' (still hashing: offered to the app when ready).')));
if(devs.length)box.appendChild(el('div',{'class':'ln'},T.count(r.devicesInstalled,devs.length)));}
else if(r.cancelled)box.appendChild(el('div',{'class':'ln'},T.cancelNote));
var shown=0;devs.forEach(function(d){var t=devLine(r,d);if(t){box.appendChild(el('div',{'class':'ln'},t));shown++;}});
if((r.wanted||r.removeRequested)&&!devs.length)box.appendChild(el('div',{'class':'ln'},'No car app has checked in yet.'));
var tg=el('button',null,r.done?T.cancelReq:r.wanted?'Cancel install':'Install on car app');
if(!r.wanted&&!r.hasMap){tg.disabled=true;tg.title='the region has no map file yet';}
tg.onclick=function(){post('regions/api/'+encodeURIComponent(r.id)+'/wanted',{wanted:!r.wanted}).then(function(j){done(j,msg);});};
box.appendChild(tg);
var rm=el('button',{'class':'danger'},'Remove from car app');
rm.onclick=function(){if(confirm('Ask the car app to remove the region '+r.id+'? The files stay on the add-on.'))post('regions/api/'+encodeURIComponent(r.id)+'/remove').then(function(j){done(j,msg);});};
box.appendChild(rm);return box;}
function queueText(d){var t=[];if(d.running)t.push(T.run+d.running);
if(d.queue&&d.queue.length)t.push(T.queue+d.queue.map(function(q){return q.position+'. '+q.id;}).join(', '));return t.length?t.join('; '):T.nothing;}
function deviceTable(devs){var box=$('devices');box.textContent='';$('devcount').textContent='('+devs.length+')';
if(!devs.length){box.appendChild(el('p',{'class':'empty'},'No car app has reported yet.'));return;}
var t=el('table',{'class':'dev'}),hd=el('tr');['Name','Last seen','App version','Device id','Install',''].forEach(function(h){hd.appendChild(el('th',null,h));});t.appendChild(hd);
devs.forEach(function(d){var tr=el('tr');[devName(d),ago(d.ageSeconds)+' ('+d.lastSeen.slice(0,16).replace('T',' ')+' UTC)',d.appVersion||'-',d.deviceId.slice(0,8)+'...',queueText(d)].forEach(function(x){tr.appendChild(el('td',null,x));});
var td=el('td'),fb=el('button',null,'Forget');fb.title='Remove this device from the list (it appears again when the app reports)';
fb.onclick=function(){if(confirm('Forget the device '+devName(d)+'? It is listed again the next time the app reports.'))post('regions/api/devices/'+encodeURIComponent(d.deviceId)+'/forget').then(function(j){done(j);});};
td.appendChild(fb);tr.appendChild(td);t.appendChild(tr);});
box.appendChild(t);devs.forEach(function(d){var s=d.estimate;if(!s||s.state==='idle')return;
var txt=devName(d)+': '+(s.state==='eta'?T.sum(s.count,dur(s.etaMinutes),size(s.bytesLeft),size(s.bytesPerSecond)+'/s'):s.state==='measuring'?T.measuring:T.pausedSum)+(s.state==='eta'||s.state==='measuring'?' — '+T.note:'');
box.appendChild(el('div',{'class':'ln sub'},txt));});
var all=el('button',null,'Forget all devices');
all.onclick=function(){if(confirm('Forget all '+devs.length+' devices? Pending removals of regions then wait for a new report.'))post('regions/api/devices/forget-all').then(function(j){done(j);});};
box.appendChild(all);}
function upload(id,kind,file,msg,bar){var x=new XMLHttpRequest();x.open('PUT','regions/api/'+encodeURIComponent(id)+'/upload/'+kind);
x.setRequestHeader('X-Requested-With','car-media-hub');bar.hidden=false;bar.value=0;
x.upload.onprogress=function(e){if(e.lengthComputable)bar.value=e.loaded/e.total;};
x.onload=function(){bar.hidden=true;var j={};try{j=JSON.parse(x.responseText);}catch(e){j={ok:false,error:'HTTP '+x.status};}
if(j.ok)msg.textContent='';else msg.textContent=j.error||'upload failed';load();};
x.onerror=function(){bar.hidden=true;msg.textContent='upload failed (connection)';};x.send(file);}
function regionCard(r,devs){var m=r.manifest,card=el('div',{'class':'rg'});
card.appendChild(el('h3',null,m.name.en+(m.name.de&&m.name.de!==m.name.en?' / '+m.name.de:'')+'  ['+r.id+']'));
var meta=[];if(m.version)meta.push('version '+m.version);if(m.bounds)meta.push('bounds '+m.bounds.join(', '));
if(m.routingGroup)meta.push('routing group '+m.routingGroup);meta.push(size(r.totalBytes)+' in total');
card.appendChild(el('div',{'class':'meta'},meta.join('  |  ')));
var carMsg=el('div',{'class':'err'});card.appendChild(carBox(r,devs,carMsg));card.appendChild(carMsg);
if(m.license)card.appendChild(el('div',{'class':'meta'},'License: '+m.license));
if(r.manifestProblem)card.appendChild(el('div',{'class':'err'},'manifest: '+r.manifestProblem+' (edit and save it below)'));
r.problems.forEach(function(p){card.appendChild(el('div',{'class':'err'},p));});
var t=el('table'),hd=el('tr');['File','Size','State',''].forEach(function(h){hd.appendChild(el('th',null,h));});t.appendChild(hd);
var msg=el('div',{'class':'err'});
r.files.forEach(function(f){var tr=el('tr');tr.appendChild(el('td',null,f.kind+' ('+f.file+')'));tr.appendChild(el('td',null,f.present?size(f.size):''));
tr.appendChild(el('td',null,stateText(f)+gzText(f)+(f.busy?' - transfer running':'')));var td=el('td');
var inp=el('input',{type:'file'});inp.hidden=true;var bar=el('progress',{'class':'prog',max:'1'});bar.hidden=true;
var b=el('button',null,f.present?'Replace...':'Upload...');b.onclick=function(){inp.click();};
inp.onchange=function(){if(inp.files[0])upload(r.id,f.kind,inp.files[0],msg,bar);};
td.appendChild(b);td.appendChild(inp);td.appendChild(bar);
if(f.present){var d=el('button',null,'Remove');d.onclick=function(){if(confirm('Remove '+f.file+' of '+r.id+'?'))post('regions/api/'+encodeURIComponent(r.id)+'/files/'+f.kind+'/delete').then(function(j){done(j,msg);});};td.appendChild(d);}
tr.appendChild(td);t.appendChild(tr);});
card.appendChild(t);card.appendChild(msg);
(r.parts||[]).forEach(function(p){var ln=el('div',{'class':'meta'},(p.origin==='import'?'Partial import of ':'Unfinished upload of ')+p.kind+': '+size(p.size)+', '+ago(p.ageSeconds)+(p.running?' - transfer running':(p.origin==='import'?' - resumes with the same address, deleted after 24 h':' - deleted after 24 h'))+' ');
if(!p.running){var pb=el('button',null,'Discard');pb.onclick=function(){post('regions/api/'+encodeURIComponent(r.id)+'/parts/'+p.kind+'/discard').then(function(j){done(j,msg);});};ln.appendChild(pb);}
card.appendChild(ln);});
var imp=el('details'),key='imp-'+r.id;imp.open=!!openForms[key];imp.ontoggle=function(){openForms[key]=imp.open;};
imp.appendChild(el('summary',null,'Import from URL (https)'));var fm=el('div',{'class':'form'});
var sel=el('select');['map','routing','places'].forEach(function(k){sel.appendChild(el('option',{value:k},k));});
var url=el('input',{type:'url',placeholder:'https://example.org/oberpfalz.pmtiles'}),sha=el('input',{placeholder:'expected sha256 (optional)'});
[['Kind',sel],['Address',url],['SHA-256',sha]].forEach(function(p){var l=el('label',null,p[0]);l.appendChild(p[1]);fm.appendChild(l);});
imp.appendChild(fm);var ib=el('button',null,'Start import'),im=el('span',{'class':'err'});
ib.onclick=function(){post('regions/api/'+encodeURIComponent(r.id)+'/import',{kind:sel.value,url:url.value,sha256:sha.value}).then(function(j){done(j,im);});};
imp.appendChild(ib);imp.appendChild(im);card.appendChild(imp);
var ed=el('details'),ek='ed-'+r.id;ed.open=!!openForms[ek];ed.ontoggle=function(){openForms[ek]=ed.open;};ed.appendChild(el('summary',null,'Edit details'));
var ef=el('div',{'class':'form'}),vals={};
[['en','Name (English)',m.name.en],['de','Name (German)',m.name.de],['bounds','Bounds west,south,east,north',m.bounds?m.bounds.join(','):''],
['version','Version',m.version],['routingGroup','Routing group',m.routingGroup||''],['source','Source',m.source],['license','License / attribution',m.license]].forEach(function(p){
var i=el('input');i.value=p[2]||'';vals[p[0]]=i;var l=el('label',null,p[1]);l.appendChild(i);ef.appendChild(l);});
ed.appendChild(ef);var sb=el('button',null,'Save'),sm=el('span',{'class':'err'});
sb.onclick=function(){post('regions/api/'+encodeURIComponent(r.id)+'/manifest',{name_en:vals.en.value,name_de:vals.de.value,bounds:vals.bounds.value,
version:vals.version.value,routingGroup:vals.routingGroup.value,source:vals.source.value,license:vals.license.value}).then(function(j){done(j,sm);});};
ed.appendChild(sb);ed.appendChild(sm);card.appendChild(ed);
var del=el('button',{'class':'danger'},'Delete region');del.onclick=function(){if(confirm('Delete region '+r.id+' with all its files? This cannot be undone.'))post('regions/api/'+encodeURIComponent(r.id)+'/delete').then(function(j){done(j,msg);});};
card.appendChild(del);return card;}
function taskLine(t){var d=el('div',{'class':'rg'});var pct=t.total?Math.round(100*t.downloaded/t.total)+'%':size(t.downloaded);
d.appendChild(el('div',null,'Import '+t.region+'/'+t.kind+' from '+t.url+' - '+t.state+(t.state==='queued'&&t.queuePosition?' ('+T.pos(t.queuePosition)+')':'')+(t.state==='running'?' '+pct+' ('+size(t.downloaded)+(t.total?' of '+size(t.total):'')+')':'')+(t.resumedFrom?' (resumed at '+size(t.resumedFrom)+')':'')));
if(t.error)d.appendChild(el('div',{'class':'err'},t.error));
if(t.state==='running'||t.state==='queued'){var c=el('button',null,'Cancel');c.onclick=function(){post('regions/api/tasks/'+t.id+'/cancel').then(function(j){done(j);});};d.appendChild(c);}
return d;}
function load(){fetch('regions/api/list',{headers:HDR}).then(function(r){return r.json();}).then(function(j){
if(!j.ok){$('problem').textContent=j.error||'error';return;}
$('root').textContent=j.root;chargerLine(j.chargers);$('problem').textContent=j.problem||'';
var box=$('regions');var active=document.activeElement;
if(active&&active.tagName==='INPUT'&&box.contains(active)){schedule(j);return;}  // do not rebuild under the cursor
box.textContent='';if(!j.regions.length)box.appendChild(el('p',{'class':'empty'},'No regions yet.'));
j.regions.forEach(function(r){box.appendChild(regionCard(r,j.devices||[]));});deviceTable(j.devices||[]);
var tb=$('tasks');tb.textContent='';j.tasks.forEach(function(t){tb.appendChild(taskLine(t));});
$('status').textContent='Limit per file: '+size(j.maxFileBytes);schedule(j);}).catch(function(){schedule(null);});}
function schedule(j){clearTimeout(timer);var busy=j&&((j.chargers&&j.chargers.running)||j.tasks.some(function(t){return t.state==='running'||t.state==='queued';})||j.regions.some(function(r){return r.files.some(function(f){return f.busy||f.state==='hashing'||f.state==='settling'||(f.transfer&&f.transfer.state==='building');})||(r.parts||[]).some(function(p){return p.running;});}));
var devs=(j&&j.devices)||[];
var pending=j&&j.regions.some(function(r){if(r.removeRequested)return true;if(!r.wanted)return false;
return!devs.some(function(d){return d.regions.some(function(x){return x.id===r.id&&x.state==='installed';});});});
timer=setTimeout(load,busy?2000:(pending?5000:30000));}
$('installall').onclick=function(){if(confirm(T.installAll))post('regions/api/install',{all:true}).then(function(j){done(j);});};
function chargerLine(c){if(!c){$('chargers').style.display='none';return;}
var t=function(s){return s?s.replace('T',' ').replace('Z',' UTC'):'never';};
var s=c.running?'updating ('+(c.phase||'starting')+')...':(c.lastError&&c.lastStage?'failed at: '+c.lastStage+'; ':'')+'last success: '+t(c.lastSuccess)+(c.rows?', '+c.rows+' charge points':'')+(c.sourceUpdated?' (register dated '+c.sourceUpdated+')':'')+(c.enabled?'; next automatic run after '+t(c.nextRun):'; automatic refresh is off (charger_refresh_days = 0)');
$('ch-line').textContent=s;$('ch-err').textContent=c.lastError?'Last attempt failed ('+t(c.lastAttempt)+(c.lastStage?', stage: '+c.lastStage:'')+'): '+c.lastError+' The previous database is kept.':'';
$('ch-refresh').disabled=!!c.running;}
$('ch-refresh').onclick=function(){post('regions/api/chargers/refresh').then(function(j){if(j&&j.chargers)chargerLine(j.chargers);setTimeout(load,1500);});};
$('rescan').onclick=function(){post('regions/api/rescan').then(function(j){done(j);});};
$('create').onclick=function(){post('regions/api/create',{id:$('n-id').value,name_en:$('n-en').value,name_de:$('n-de').value,bounds:$('n-bounds').value,
version:$('n-version').value,routingGroup:$('n-group').value,source:$('n-source').value,license:$('n-license').value}).then(function(j){done(j,$('create-msg'));});};
load();
})();
"""
