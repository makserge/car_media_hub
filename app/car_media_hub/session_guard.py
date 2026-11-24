"""One small script shared by every page of the ingress UI: it notices that Home Assistant answered 401.

Behind ingress Home Assistant checks a short-lived ingress session cookie before a request reaches this add-on. When the
cookie has expired (idle tab, companion app, a bookmarked ``/api/hassio_ingress/<token>/`` link) the answer is a bare 401
from Home Assistant, never from the add-on (its middleware only sends 403). The script wraps ``fetch`` and
``XMLHttpRequest`` (relative URLs only, same origin only), shows a banner with a Reload button and stops further requests.
The page itself is left as it is, so text typed into a form is still there; only the Reload button reloads.
``window.cmhSession.probe(url)`` lets a WebSocket page ask after a close whether the cause was a 401.
"""
from __future__ import annotations

SESSION_GUARD_JS = r"""(function(){
if(window.cmhSession)return;
var S=window.cmhSession={dead:false};
function reload(){try{if(window.top&&window.top!==window){window.top.location.reload();return;}}catch(e){}location.reload();}
function banner(){
var b=document.createElement('div');b.id='cmh-session-expired';b.setAttribute('role','alert');
b.style.cssText='position:fixed;left:0;right:0;top:0;z-index:2147483647;padding:10px 12px;background:#b9770e;color:#fff;font:14px system-ui,sans-serif;display:flex;gap:12px;align-items:center;justify-content:center;box-shadow:0 2px 8px rgba(0,0,0,.4)';
var t=document.createElement('span');t.textContent='Session expired - reload this page';
var r=document.createElement('button');r.type='button';r.textContent='Reload';
r.style.cssText='font:inherit;padding:4px 14px;border:0;border-radius:4px;background:#fff;color:#000;cursor:pointer';
r.onclick=reload;b.appendChild(t);b.appendChild(r);(document.body||document.documentElement).appendChild(b);}
function expire(){if(S.dead)return;S.dead=true;
if(document.body)banner();else document.addEventListener('DOMContentLoaded',banner);}
S.expire=expire;S.reload=reload;
function same(u){try{return new URL(u,location.href).origin===location.origin;}catch(e){return true;}}
var F=window.fetch;
if(F)window.fetch=function(){
if(S.dead)return new Promise(function(){});  /* no more requests; the banner tells the user what to do */
return F.apply(this,arguments).then(function(r){
if(r&&r.status===401&&same(r.url)){expire();return new Promise(function(){});}
return r;});};
var X=window.XMLHttpRequest;
if(X&&X.prototype.send){var send=X.prototype.send;
X.prototype.send=function(){var x=this;
x.addEventListener('loadend',function(){if(x.status===401&&same(x.responseURL))expire();});
return send.apply(x,arguments);};}
/* after a WebSocket close: resolves true while the session is fine (or the check could not run), false after a 401 */
S.probe=function(url){return fetch(url,{cache:'no-store'}).then(function(){return true;},function(){return !S.dead;});};
})();"""

SESSION_GUARD_TAG = "<script>" + SESSION_GUARD_JS + "</script>"
