"""Home Assistant client: service calls (REST) and event subscription (WebSocket)."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

import aiohttp

log = logging.getLogger(__name__)

EventHandler = Callable[[str, dict], Awaitable[None]]


class HAClient:
    """Thin Home Assistant API client that never lets notification errors escape."""

    reconnect_min = 1.0
    reconnect_max = 60.0

    def __init__(
        self,
        session: aiohttp.ClientSession,
        token: str,
        base_url: str = "http://supervisor/core/api",
        ws_url: str = "ws://supervisor/core/websocket",
    ) -> None:
        self._session = session
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._ws_url = ws_url

    # ------------------------------------------------------------------ REST
    async def call_service(self, domain: str, service: str, data: dict) -> None:
        """Call a HA service; HTTP and network errors are logged and swallowed."""
        url = f"{self._base_url}/services/{domain}/{service}"
        try:
            async with self._session.post(
                url,
                json=data,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status >= 400:
                    body = (await resp.text())[:200]
                    log.warning("HA service %s.%s returned HTTP %s: %s", domain, service, resp.status, body)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            log.warning("HA service %s.%s failed: %s", domain, service, exc)

    async def set_state(self, entity_id: str, state: Any, attributes: dict | None = None) -> None:
        """Publish an entity state (REST ``POST /states/<entity_id>``); errors are logged and swallowed.

        Home Assistant does not store such states: they are gone after its restart, so the caller
        publishes them again at start-up.
        """
        url = f"{self._base_url}/states/{entity_id}"
        payload = {"state": state, "attributes": attributes or {}}
        try:
            async with self._session.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status >= 400:
                    body = (await resp.text())[:200]
                    log.warning("HA state %s returned HTTP %s: %s", entity_id, resp.status, body)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            log.warning("HA state %s failed: %s", entity_id, exc)

    async def persistent_create(self, notification_id: str, title: str, message: str) -> None:
        await self.call_service(
            "persistent_notification",
            "create",
            {"notification_id": notification_id, "title": title, "message": message},
        )

    async def persistent_dismiss(self, notification_id: str) -> None:
        await self.call_service("persistent_notification", "dismiss", {"notification_id": notification_id})

    async def notify(self, service: str, title: str, message: str, data: dict | None = None) -> None:
        payload: dict[str, Any] = {"title": title, "message": message}
        if data:
            payload["data"] = data
        await self.call_service("notify", service, payload)

    # ------------------------------------------------------------- WebSocket
    async def _ws_auth(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Run the auth handshake on a fresh websocket; raises when it fails."""
        first = await ws.receive_json()
        if first.get("type") != "auth_required":
            raise RuntimeError(f"unexpected greeting: {first!r}")
        await ws.send_json({"type": "auth", "access_token": self._token})
        reply = await ws.receive_json()
        if reply.get("type") != "auth_ok":
            raise RuntimeError(f"authentication failed: {reply!r}")

    async def ws_command(self, command: dict, timeout: float = 10.0) -> Any:
        """One-shot websocket command: connect, authenticate, send, return ``result``, close.

        Raises on failure, an error reply or when ``timeout`` seconds pass.
        """

        async def run() -> Any:
            async with self._session.ws_connect(self._ws_url) as ws:
                await self._ws_auth(ws)
                await ws.send_json({"id": 1, **command})
                while True:
                    msg = await ws.receive_json()
                    if msg.get("id") != 1 or msg.get("type") != "result":
                        continue
                    if not msg.get("success"):
                        raise RuntimeError(f"command {command.get('type')} failed: {msg.get('error')!r}")
                    return msg.get("result")

        return await asyncio.wait_for(run(), timeout=timeout)

    async def listen_events(
        self,
        event_types: list[str],
        handler: EventHandler,
        stop: asyncio.Event,
    ) -> None:
        """Subscribe to events and dispatch them to ``handler`` until ``stop`` is set."""
        backoff = self.reconnect_min
        while not stop.is_set():
            try:
                authed = await self._listen_once(event_types, handler, stop)
                if authed:
                    backoff = self.reconnect_min
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("HA websocket error: %s", exc)
            if stop.is_set():
                break
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, self.reconnect_max)

    async def _listen_once(self, event_types: list[str], handler: EventHandler, stop: asyncio.Event) -> bool:
        """Run one websocket connection. Returns True once authenticated."""
        async with self._session.ws_connect(self._ws_url, heartbeat=30) as ws:
            await self._ws_auth(ws)
            log.info("Connected to Home Assistant websocket")
            for msg_id, event_type in enumerate(event_types, start=1):
                await ws.send_json({"id": msg_id, "type": "subscribe_events", "event_type": event_type})

            async def closer() -> None:
                await stop.wait()
                await ws.close()

            closer_task = asyncio.create_task(closer())
            try:
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    await self._dispatch(msg.data, handler)
            finally:
                closer_task.cancel()
            return True

    @staticmethod
    async def _dispatch(raw: str, handler: EventHandler) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            log.warning("Ignoring non-JSON websocket message")
            return
        if msg.get("type") != "event":
            return
        event = msg.get("event") or {}
        try:
            await handler(event.get("event_type", ""), event.get("data") or {})
        except Exception:
            log.exception("Event handler failed")
