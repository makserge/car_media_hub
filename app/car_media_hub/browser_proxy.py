"""A small local filtering proxy for the embedded browser.

Chromium is started with ``--proxy-server`` pointing here (and with ``<-loopback>`` so that not even
localhost bypasses it). Every request, from the page, its frames and its scripts, therefore passes
:func:`~car_media_hub.netguard.resolve_public`: the name is resolved HERE, every address is checked
(loopback, link-local, private, Home Assistant's 172.30.32.0/23 ...), and the connection goes to the checked
address, so DNS rebinding cannot swap the answer afterwards. Plain ``http://`` requests are forwarded ONE per
connection: the proxy sends ``Connection: close`` upstream and to the browser, forwards only that request (its head
and its ``Content-Length`` body) and closes the connection when anything else arrives (pipelining), so every
request is checked and a kept-alive socket can never carry a second request to another site. ``Upgrade`` and
chunked request bodies are not proxied this way. ``https://`` uses CONNECT and is tunnelled (never decrypted).
"""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlsplit

from .netguard import BlockedURL, Resolver, authority_split, resolve_public, system_resolver

log = logging.getLogger(__name__)

MAX_HEAD = 64 * 1024
HEAD_TIMEOUT = 30.0
CONNECT_TIMEOUT = 15.0
IDLE_TIMEOUT = 180.0
MAX_CONNECTIONS = 96
_HOP_HEADERS = {"proxy-connection", "proxy-authorization", "proxy-authenticate", "keep-alive", "te", "trailer",
                "upgrade", "connection", "expect"}
MAX_BODY = 256 * 1024 * 1024  # a request body (an upload) larger than this is refused


def request_body_length(lines: list[str]) -> int:
    """The length of the request body announced by the head (``Content-Length``), 0 when there is none. Raises
    :class:`BlockedURL` for ``Transfer-Encoding`` (chunked bodies), a repeated or bad length or one that is too large."""
    length: int | None = None
    for line in lines[1:]:
        key, _, value = line.partition(":")
        key = key.strip().lower()
        if key == "transfer-encoding":
            raise BlockedURL("chunked request bodies are not proxied")
        if key == "content-length":
            value = value.strip()
            if length is not None or not value.isascii() or not value.isdigit() or len(value) > 12:
                raise BlockedURL("bad Content-Length")
            length = int(value)
    if length is not None and length > MAX_BODY:
        raise BlockedURL("the request body is too large")
    return length or 0


def rewrite_request_head(lines: list[str]) -> tuple[str, int | None, bytes, str]:
    """Turn the head of an absolute-form proxy request (``GET http://h/p HTTP/1.1`` + headers) into
    ``(host, port, rewritten head, url)``. Raises :class:`BlockedURL` for anything else."""
    try:
        method, target, version = lines[0].split(" ", 2)
    except ValueError:
        raise BlockedURL("bad request line") from None
    if not target.lower().startswith("http://"):
        raise BlockedURL("only http:// addresses are proxied this way")
    parts = urlsplit(target)
    try:
        host, port = parts.hostname or "", parts.port or 80
    except ValueError:
        raise BlockedURL("bad address") from None
    if parts.username is not None or parts.password is not None:
        raise BlockedURL("addresses with a user name or password are not opened")
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    headers: list[str] = []
    for line in lines[1:]:
        key = line.split(":", 1)[0].strip().lower()
        if key in _HOP_HEADERS or key == "host":
            continue
        headers.append(line)
    headers.append("Connection: close")  # one request per connection, always
    netloc = parts.netloc.rsplit("@", 1)[-1]
    head = "\r\n".join([f"{method} {path} {version}", f"Host: {netloc}", *headers]) + "\r\n\r\n"
    return host, port, head.encode("latin-1", "replace"), target


class FilteringProxy:
    def __init__(self, resolver: Resolver = system_resolver) -> None:
        self._resolver = resolver
        self._server: asyncio.base_events.Server | None = None
        self._conns: set[asyncio.Task] = set()
        self.port = 0
        self.blocked = 0

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            self._server = None
        for task in list(self._conns):
            task.cancel()
        self._conns.clear()

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task:
            self._conns.add(task)
        try:
            if len(self._conns) > MAX_CONNECTIONS:
                await self._reply(writer, 503, "too many connections")
                return
            await self._serve(reader, writer)
        except (asyncio.CancelledError, ConnectionError, asyncio.TimeoutError, OSError):
            pass
        except Exception:  # noqa: BLE001 - one bad connection must not stop the proxy
            log.debug("browser proxy: connection failed", exc_info=True)
        finally:
            if task:
                self._conns.discard(task)
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, status: int, text: str) -> None:
        body = text.encode()
        writer.write(f"HTTP/1.1 {status} {text}\r\nContent-Type: text/plain\r\nContent-Length: {len(body)}\r\n"
                     "Connection: close\r\n\r\n".encode() + body)
        try:
            await writer.drain()
        except ConnectionError:
            pass

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            return
        if len(raw) > MAX_HEAD:
            await self._reply(writer, 431, "request too large")
            return
        lines = raw.decode("latin-1").split("\r\n")[:-2]
        first = lines[0].split(" ")
        pending = b""
        body_length = 0
        try:
            if len(first) == 3 and first[0].upper() == "CONNECT":
                host, port = authority_split(first[1], 443)
                tunnel = True
            else:
                host, port, pending, _url = rewrite_request_head(lines)
                body_length = request_body_length(lines)
                tunnel = False
            address = await resolve_public(host or "", port or 80, self._resolver)
        except BlockedURL as exc:
            self.blocked += 1
            log.info("browser proxy: blocked %s (%s)", lines[0][:200], exc)
            await self._reply(writer, 403, f"blocked: {exc}")
            return
        try:
            up_reader, up_writer = await asyncio.wait_for(asyncio.open_connection(address, port), CONNECT_TIMEOUT)
        except (OSError, asyncio.TimeoutError):
            await self._reply(writer, 502, "could not connect")
            return
        try:
            if tunnel:
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await writer.drain()
                await asyncio.gather(self._pipe(reader, up_writer), self._pipe(up_reader, writer))
            else:
                await self._exchange(reader, writer, up_reader, up_writer, pending, body_length)
        finally:
            up_writer.close()

    async def _exchange(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, up_reader: asyncio.StreamReader,
                        up_writer: asyncio.StreamWriter, head: bytes, body_length: int) -> None:
        """ONE request and its response. Whatever the browser sends after the request (a pipelined or "kept-alive"
        follow-up request that was never checked) ends the connection instead of being forwarded."""
        up_writer.write(head)
        remaining = body_length
        while remaining > 0:
            chunk = await asyncio.wait_for(reader.read(min(65536, remaining)), IDLE_TIMEOUT)
            if not chunk:
                return
            remaining -= len(chunk)
            up_writer.write(chunk)
            await up_writer.drain()
        await up_writer.drain()
        response = asyncio.ensure_future(self._relay_response(up_reader, writer))
        extra = asyncio.ensure_future(reader.read(1))  # more bytes (or the end of the stream) from the browser
        try:
            await asyncio.wait({response, extra}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (response, extra):
                if not task.done():
                    task.cancel()
            await asyncio.gather(response, extra, return_exceptions=True)
            if extra.done() and not extra.cancelled() and extra.exception() is None and extra.result():
                self.blocked += 1
                log.info("browser proxy: a second request on one connection was refused")

    @staticmethod
    async def _relay_response(up_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Pass the response on with ``Connection: close`` (the browser must not reuse the socket); the upstream
        server was asked to close as well. Interim (1xx) answers are passed on, a protocol switch (101) is refused."""
        while True:
            try:
                raw = await asyncio.wait_for(up_reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
                await FilteringProxy._reply(writer, 502, "bad response")
                return
            lines = raw.decode("latin-1").split("\r\n")[:-2]
            parts = lines[0].split(" ", 2) if lines else []
            status = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0
            if not parts or not parts[0].startswith("HTTP/") or status < 100:
                await FilteringProxy._reply(writer, 502, "bad response")
                return
            if status == 101:
                await FilteringProxy._reply(writer, 502, "protocol switch refused")
                return
            if status < 200:
                writer.write(raw)
                await writer.drain()
                continue
            kept = [h for h in lines[1:] if h.split(":", 1)[0].strip().lower() not in ("connection", "keep-alive", "proxy-connection", "upgrade")]
            writer.write(("\r\n".join([lines[0], *kept, "Connection: close"]) + "\r\n\r\n").encode("latin-1", "replace"))
            await writer.drain()
            break
        await FilteringProxy._pipe(up_reader, writer)

    @staticmethod
    async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
        try:
            while True:
                data = await asyncio.wait_for(src.read(65536), IDLE_TIMEOUT)
                if not data:
                    break
                dst.write(data)
                await dst.drain()
        except (ConnectionError, asyncio.TimeoutError, OSError):
            pass
        finally:
            try:
                dst.close()
            except Exception:  # noqa: BLE001
                pass
