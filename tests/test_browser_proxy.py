import asyncio

import pytest

from car_media_hub import browser_proxy as bpx
from car_media_hub.netguard import BlockedURL


def head(*lines):
    return list(lines)


def test_rewrite_request_head_makes_origin_form_and_closes():
    host, port, raw, url = bpx.rewrite_request_head(head(
        "GET http://example.com:8080/a/b?x=1 HTTP/1.1", "Host: wrong.example", "Proxy-Connection: keep-alive",
        "Connection: keep-alive", "Accept: */*"))
    assert (host, port, url) == ("example.com", 8080, "http://example.com:8080/a/b?x=1")
    text = raw.decode()
    assert text.startswith("GET /a/b?x=1 HTTP/1.1\r\nHost: example.com:8080\r\n")
    assert "wrong.example" not in text and "Proxy-Connection" not in text and "keep-alive" not in text
    assert "Connection: close" in text and text.endswith("\r\n\r\n")


def test_rewrite_never_keeps_a_connection_open_or_upgrades_it():
    _, _, raw, _ = bpx.rewrite_request_head(head(
        "GET http://example.com/ws HTTP/1.1", "Connection: Upgrade", "Upgrade: websocket", "Expect: 100-continue"))
    assert b"Upgrade" not in raw and b"Expect" not in raw and raw.count(b"Connection:") == 1 and b"Connection: close" in raw


@pytest.mark.parametrize("line", ["GET /relative HTTP/1.1", "GET https://example.com/ HTTP/1.1", "GARBAGE",
                                  "GET ftp://example.com/ HTTP/1.1", "GET http://u:p@example.com/ HTTP/1.1"])
def test_rewrite_refuses_other_forms(line):
    with pytest.raises(BlockedURL):
        bpx.rewrite_request_head([line])


async def ask(port: int, payload: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(payload)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(65536), 5)
    writer.close()
    return data


async def test_proxy_blocks_internal_targets_without_connecting():
    async def resolver(host, port):
        return {"rebind.example.com": ["192.168.1.20"], "mixed.example.com": ["93.184.216.34", "127.0.0.1"]}[host]

    proxy = bpx.FilteringProxy(resolver)
    port = await proxy.start()
    try:
        cases = [
            b"CONNECT 127.0.0.1:8099 HTTP/1.1\r\nHost: x\r\n\r\n",
            b"CONNECT localhost:443 HTTP/1.1\r\n\r\n",
            b"CONNECT supervisor:80 HTTP/1.1\r\n\r\n",
            b"CONNECT 172.30.32.2:80 HTTP/1.1\r\n\r\n",
            b"CONNECT [::1]:80 HTTP/1.1\r\n\r\n",
            b"CONNECT rebind.example.com:443 HTTP/1.1\r\n\r\n",
            b"CONNECT mixed.example.com:443 HTTP/1.1\r\n\r\n",
            b"GET http://127.0.0.1:8100/api/status HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n",
            b"GET http://rebind.example.com/ HTTP/1.1\r\n\r\n",
            b"GET http://2130706433/ HTTP/1.1\r\n\r\n",
            b"CONNECT example.com:25 HTTP/1.1\r\n\r\n",
        ]
        for payload in cases:
            assert (await ask(port, payload)).startswith(b"HTTP/1.1 403"), payload
        assert proxy.blocked == len(cases)
    finally:
        await proxy.stop()


async def test_proxy_forwards_to_the_checked_address_not_the_name(monkeypatch):
    seen = []

    async def echo(reader, writer):
        data = await reader.readuntil(b"\r\n\r\n")
        seen.append(data)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    target_port = server.sockets[0].getsockname()[1]
    resolved = []

    async def fake_resolve(host, port, resolver=None):
        resolved.append((host, port))
        return "127.0.0.1"  # stands in for a public address in this test

    monkeypatch.setattr(bpx, "resolve_public", fake_resolve)
    proxy = bpx.FilteringProxy()
    port = await proxy.start()
    try:
        reply = await ask(port, f"GET http://files.example.com:{target_port}/x?y=1 HTTP/1.1\r\nHost: files.example.com\r\n"
                                "Proxy-Connection: keep-alive\r\n\r\n".encode())
    finally:
        await proxy.stop()
        server.close()
    assert reply.endswith(b"ok")
    assert resolved == [("files.example.com", target_port)]
    assert seen[0].startswith(b"GET /x?y=1 HTTP/1.1\r\nHost: files.example.com:%d\r\n" % target_port)
    assert b"Proxy-Connection" not in seen[0]


async def test_proxy_tunnels_connect_to_the_checked_address(monkeypatch):
    async def echo(reader, writer):
        writer.write(await reader.read(100))
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    target_port = server.sockets[0].getsockname()[1]

    async def fake_resolve(host, port, resolver=None):
        return "127.0.0.1"

    monkeypatch.setattr(bpx, "resolve_public", fake_resolve)
    proxy = bpx.FilteringProxy()
    port = await proxy.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(f"CONNECT tls.example.com:{target_port} HTTP/1.1\r\n\r\n".encode())
        assert (await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)).startswith(b"HTTP/1.1 200")
        writer.write(b"hello")
        assert await asyncio.wait_for(reader.read(100), 5) == b"hello"
        writer.close()
    finally:
        await proxy.stop()
        server.close()
