"""URL and address checks for everything the embedded browser (and the feed links) may reach.

Pure functions, no I/O except :func:`resolve_public` (name resolution). Used by the browser's navigation
checks, by its filtering proxy (which resolves a name, checks every address and connects to the checked
address itself, so a DNS answer cannot change between the check and the connection) and by the feed page.
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from typing import Awaitable, Callable
from urllib.parse import unquote, urlsplit

ALLOWED_SCHEMES = ("http", "https")
# Home Assistant's internal network (Supervisor, core, add-ons): private anyway, named for clarity
HASSIO_NET = ipaddress.ip_network("172.30.32.0/23")
EXTRA_BLOCKED_NETS = (
    HASSIO_NET,
    ipaddress.ip_network("172.30.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),  # carrier-grade NAT / tailscale
    ipaddress.ip_network("64:ff9b::/96"),  # NAT64: embeds an IPv4 address
    ipaddress.ip_network("2002::/16"),  # 6to4: embeds an IPv4 address
    ipaddress.ip_network("::/96"),  # IPv4-compatible (::127.0.0.1, ::7f00:1), deprecated; Python calls some of them global
    ipaddress.ip_network("fec0::/10"),  # deprecated site-local
)
BLOCKED_NAMES = frozenset({
    "localhost", "supervisor", "hassio", "homeassistant", "home-assistant", "core-ssl",
    "metadata", "metadata.google.internal",
})
BLOCKED_SUFFIXES = (
    ".localhost", ".local", ".internal", ".lan", ".home", ".home.arpa", ".localdomain", ".intranet",
    ".corp", ".private", ".hassio", ".docker", ".onion", ".arpa",
)
# ports never reached through the browser (mail submission: spam relay)
ALWAYS_BLOCKED_PORTS = frozenset({25, 465, 587})
ADDON_PORTS = (8099, 8100)  # ingress and sync API of this add-on (loopback is blocked anyway)

_CTRL = re.compile(r"[\x00-\x20\x7f]")


class BlockedURL(ValueError):
    """The address must not be opened; the message says why (shown to the viewer)."""


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip in ipaddress.ip_network("64:ff9b::/96"):
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if ip.sixtofour is not None:
        return ip.sixtofour
    return None


def ip_is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for every address that is not a normal public internet address (also IPv4 hidden in IPv6)."""
    if isinstance(ip, ipaddress.IPv6Address):
        inner = _embedded_ipv4(ip)
        if inner is not None and ip_is_blocked(inner):
            return True
    if not ip.is_global or ip.is_multicast or ip.is_unspecified:
        return True
    return any(ip.version == net.version and ip in net for net in EXTRA_BLOCKED_NETS)


def _literal_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The address written in ``host`` (also the old forms such as 2130706433, 0x7f.1 or 127.1), else None."""
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    try:  # what a browser or getaddrinfo would read as an IPv4 address
        return ipaddress.IPv4Address(socket.inet_aton(host))
    except (OSError, ValueError, UnicodeError):
        return None


def _decodable(host: str) -> bool:
    try:
        unquote(host, errors="strict")
        return True
    except UnicodeDecodeError:
        return False


def host_problem(host: str) -> str | None:
    """Why ``host`` (a name or an address, no port) must not be reached, or None. No name resolution."""
    host = host.strip().rstrip(".").lower()
    if "%" in host:  # percent-encoded host (http://%31%32%37.0.0.1/): judge what it decodes to
        host = unquote(host, errors="strict") if _decodable(host) else host
        host = host.strip().rstrip(".").lower()
    if not host:
        return "the address has no host"
    ip = _literal_ip(host)
    if ip is not None:
        return "addresses of private, local and internal networks are blocked" if ip_is_blocked(ip) else None
    if _CTRL.search(host) or "\\" in host or not host.isascii():
        return "the host name is not valid"
    if "." not in host or host in BLOCKED_NAMES or host.endswith(BLOCKED_SUFFIXES):
        return "names of private, local and internal networks are blocked"
    return None


def port_problem(port: int | None, extra_blocked: tuple[int, ...] = ()) -> str | None:
    if port is None:
        return None
    if not 0 < port < 65536:
        return "the port is not valid"
    if port in ALWAYS_BLOCKED_PORTS or port in extra_blocked:
        return f"port {port} is blocked"
    return None


def check_navigation_url(url: str) -> str:
    """Return ``url`` when the browser may open it, else raise :class:`BlockedURL`.

    Only http and https, no credentials in the address, and a host that is not private, local or internal
    (the proxy additionally checks what the name resolves to).
    """
    url = (url or "").strip()
    if _CTRL.search(url) or "\\" in url:
        raise BlockedURL("the address contains spaces, backslashes or control characters")
    try:
        parts = urlsplit(url)
        port = parts.port
        host = parts.hostname or ""
    except ValueError as exc:
        raise BlockedURL(f"the address is not valid ({exc})") from None
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise BlockedURL("only http and https addresses can be opened")
    if parts.username is not None or parts.password is not None:
        raise BlockedURL("addresses with a user name or password are not opened")
    problem = host_problem(host) or port_problem(port, ADDON_PORTS)
    if problem:
        raise BlockedURL(problem)
    return url


def safe_http_url(value: object) -> str:
    """The link for an ``href`` (feed items), or "" when it is not safe: only absolute http(s) addresses, or
    relative ones (no scheme, no host), without control characters. ``javascript:``, ``data:``, ``file:`` ...
    and anything unparsable give ""."""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or len(text) > 4096 or _CTRL.search(text) or "\\" in text:
        return ""
    try:
        parts = urlsplit(text)
        _ = parts.port
    except ValueError:
        return ""
    if parts.scheme:
        return text if parts.scheme.lower() in ALLOWED_SCHEMES and parts.hostname else ""
    if text.startswith("//"):  # scheme-relative: a host the feed chose, on the page's own scheme
        return text if parts.hostname else ""
    return text  # a relative path or query


def authority_split(text: str, default_port: int | None = None) -> tuple[str, int | None]:
    """``host:port`` / ``[v6]:port`` into (host, port)."""
    text = text.strip()
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            raise BlockedURL("the address is not valid")
        host, rest = text[1:end], text[end + 1:]
        if rest.startswith(":"):
            if not rest[1:].isdigit():
                raise BlockedURL("the port is not valid")
            return host, int(rest[1:])
        return host, default_port
    if text.count(":") == 1:
        host, _, port_text = text.partition(":")
        if not port_text.isdigit():
            raise BlockedURL("the port is not valid")
        return host, int(port_text)
    return text, default_port


Resolver = Callable[[str, int], Awaitable[list[str]]]


async def system_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]).split("%")[0] for info in infos))


async def resolve_public(host: str, port: int, resolver: Resolver = system_resolver) -> str:
    """Resolve ``host`` and return the address to connect to; raise :class:`BlockedURL` when the name does not
    resolve or ANY of its addresses is blocked (a name that also points at the LAN is refused as a whole).
    The caller must connect to the returned address, not to the name again (DNS rebinding)."""
    problem = host_problem(host) or port_problem(port)
    if problem:
        raise BlockedURL(problem)
    literal = _literal_ip(host)
    if literal is not None:
        return str(literal)
    try:
        addresses = await resolver(host, port)
    except OSError as exc:
        raise BlockedURL(f"{host} could not be resolved") from exc
    if not addresses:
        raise BlockedURL(f"{host} could not be resolved")
    parsed = []
    for address in addresses:
        try:
            parsed.append(ipaddress.ip_address(address))
        except ValueError:
            raise BlockedURL(f"{host} resolved to something that is not an address") from None
    if any(ip_is_blocked(ip) for ip in parsed):
        raise BlockedURL(f"{host} points to a private, local or internal address")
    return str(parsed[0])


_NAME_BAD = re.compile(r"[^A-Za-z0-9 ._()\[\]+,&'-]")


def safe_download_name(suggested: str, archive_exts: tuple[str, ...], limit: int = 120) -> str | None:
    """A safe file name for a download, or None when it is not an archive. Directories, control and special
    characters and a leading dot are removed; the archive extension is kept."""
    name = str(suggested or "").replace("\\", "/").split("/")[-1].strip()
    lowered = name.lower()
    ext = next((e for e in sorted(archive_exts, key=len, reverse=True) if lowered.endswith(e)), None)
    if ext is None:
        return None
    stem = _NAME_BAD.sub("_", name[: len(name) - len(ext)]).strip(" .") or "download"
    return stem[: max(1, limit - len(ext))] + ext
