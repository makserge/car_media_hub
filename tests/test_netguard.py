import ipaddress

import pytest

from car_media_hub.config import ARCHIVE_EXTS
from car_media_hub.netguard import (
    BlockedURL, authority_split, check_navigation_url, host_problem, ip_is_blocked, resolve_public, safe_download_name,
    safe_http_url,
)


@pytest.mark.parametrize("url", [
    "https://example.com/a", "http://example.com:8080/x?y=1", "HTTPS://Music.Example.org/", "http://93.184.216.34/",
    "https://[2606:4700:4700::1111]/",
])
def test_public_urls_are_allowed(url):
    assert check_navigation_url(url) == url


@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "file:///etc/passwd", "ftp://example.com/x", "data:text/html,hi", "chrome://settings",
    "about:blank", "view-source:https://example.com", "blob:https://example.com/x", "//example.com/", "example.com",
    "",
])
def test_other_schemes_are_refused(url):
    with pytest.raises(BlockedURL):
        check_navigation_url(url)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://127.0.0.1:8099/", "http://127.1/", "http://2130706433/", "http://0x7f.0.0.1/",
    "http://0177.0.0.1/", "http://localhost/", "http://LOCALHOST./", "http://foo.localhost/", "http://[::1]/",
    "http://[::ffff:127.0.0.1]/", "http://[::ffff:7f00:1]/", "http://10.0.0.5/", "http://192.168.1.10/",
    "http://172.16.0.1/", "http://172.30.32.2/", "http://172.30.33.5:80/", "http://169.254.169.254/latest/meta-data/",
    "http://100.64.0.1/", "http://0.0.0.0/", "http://[fe80::1]/", "http://[fd00::1]/", "http://[64:ff9b::7f00:1]/",
    "http://[2002:7f00:1::]/", "http://supervisor/", "http://supervisor./info", "http://homeassistant:8123/",
    "http://hassio/", "http://nas/", "http://router.local/", "http://printer.lan/", "http://x.internal/",
    "http://core-ssl/", "http://metadata.google.internal/", "http://a.home.arpa/",
])
def test_internal_targets_are_refused(url):
    with pytest.raises(BlockedURL):
        check_navigation_url(url)


@pytest.mark.parametrize("url", [
    "http://user:pw@example.com/", "https://example.com:25/", "https://example.com:8100/", "http://example.com:99999/",
    "http://exa mple.com/", "http://example.com/a b", r"http://example.com\@127.0.0.1/", "http://example.com/a\nb",
])
def test_credentials_bad_ports_and_control_chars_are_refused(url):
    with pytest.raises(BlockedURL):
        check_navigation_url(url)


def test_ip_blocking_covers_the_hassio_network_and_embedded_v4():
    assert ip_is_blocked(ipaddress.ip_address("172.30.32.1"))
    assert ip_is_blocked(ipaddress.ip_address("::ffff:172.30.32.1"))
    assert not ip_is_blocked(ipaddress.ip_address("8.8.8.8"))
    assert not ip_is_blocked(ipaddress.ip_address("2606:4700:4700::1111"))


def test_host_problem_basic():
    assert host_problem("") is not None
    assert host_problem("example.com") is None
    assert host_problem("exämple.com") is not None  # only the ascii (punycode) form is accepted


@pytest.mark.parametrize("value, expected", [
    ("https://example.com/a", "https://example.com/a"),
    ("  http://example.com/a  ", "http://example.com/a"),
    ("/relative/page", "/relative/page"),
    ("page.html?x=1", "page.html?x=1"),
    ("//cdn.example.com/x", "//cdn.example.com/x"),
    ("javascript:alert(1)", ""),
    ("JaVaScRiPt:alert(1)", ""),
    ("  javascript:alert(1)", ""),
    ("java\tscript:alert(1)", ""),
    ("data:text/html;base64,AAAA", ""),
    ("vbscript:x", ""),
    ("file:///etc/passwd", ""),
    ("ftp://example.com/x", ""),
    ("http:///nohost", ""),
    ("http://exa mple.com", ""),
    ("https://example.com/\"onmouseover=\"x", "https://example.com/\"onmouseover=\"x"),  # kept: escaped when rendered
    ("", ""), (None, ""), (5, ""), (["x"], ""),
    ("x" * 5000, ""),
])
def test_safe_http_url(value, expected):
    assert safe_http_url(value) == expected


@pytest.mark.parametrize("text, default, expected", [
    ("example.com:443", None, ("example.com", 443)),
    ("example.com", 443, ("example.com", 443)),
    ("[::1]:8099", None, ("::1", 8099)),
    ("[2001:db8::1]", 443, ("2001:db8::1", 443)),
])
def test_authority_split(text, default, expected):
    assert authority_split(text, default) == expected


@pytest.mark.parametrize("text", ["example.com:abc", "[::1", "[::1]:x"])
def test_authority_split_rejects_garbage(text):
    with pytest.raises(BlockedURL):
        authority_split(text, 443)


def fake(mapping):
    async def resolver(host, port):
        if host not in mapping:
            raise OSError("no such host")
        return mapping[host]
    return resolver


async def test_resolve_public_returns_the_checked_address():
    assert await resolve_public("a.example.com", 443, fake({"a.example.com": ["93.184.216.34"]})) == "93.184.216.34"


async def test_resolve_public_refuses_names_that_point_inside():
    for answer in (["127.0.0.1"], ["192.168.1.1"], ["172.30.32.2"], ["::1"], ["169.254.169.254"], ["::ffff:10.0.0.1"]):
        with pytest.raises(BlockedURL):
            await resolve_public("evil.example.com", 80, fake({"evil.example.com": answer}))


async def test_resolve_public_refuses_a_name_with_any_blocked_address():
    # a rebinding style answer: one public, one private address
    with pytest.raises(BlockedURL):
        await resolve_public("mix.example.com", 80, fake({"mix.example.com": ["93.184.216.34", "10.0.0.7"]}))


async def test_resolve_public_unresolvable_and_literals():
    with pytest.raises(BlockedURL):
        await resolve_public("nx.example.com", 80, fake({}))
    with pytest.raises(BlockedURL):
        await resolve_public("127.0.0.1", 80, fake({}))
    with pytest.raises(BlockedURL):
        await resolve_public("0x7f.1", 80, fake({}))
    assert await resolve_public("8.8.8.8", 443, fake({})) == "8.8.8.8"
    with pytest.raises(BlockedURL):
        await resolve_public("example.com", 25, fake({"example.com": ["93.184.216.34"]}))


@pytest.mark.parametrize("name, expected", [
    ("Album.zip", "Album.zip"), ("../../etc/passwd.zip", "passwd.zip"), (r"a\b\evil.rar", "evil.rar"),
    ("x.tar.gz", "x.tar.gz"), ("Some Album (2024).7Z", "Some Album (2024).7z"), (".zip", "download.zip"),
    ("bad\x00name\n.zip", "bad_name_.zip"),
])
def test_safe_download_name_keeps_archives(name, expected):
    assert safe_download_name(name, ARCHIVE_EXTS) == expected


@pytest.mark.parametrize("name", ["virus.exe", "page.html", "song.mp3", "archive.zip.exe", "", "noext", "zip"])
def test_safe_download_name_refuses_everything_else(name):
    assert safe_download_name(name, ARCHIVE_EXTS) is None


def test_safe_download_name_is_length_limited():
    assert len(safe_download_name("a" * 500 + ".zip", ARCHIVE_EXTS)) <= 120
