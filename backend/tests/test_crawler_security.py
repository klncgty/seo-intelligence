import ipaddress
import socket

import httpx
import pytest

from app.services.crawler.fetcher import FetchPolicy, build_client, safe_get
from app.services.crawler.parser import parse_html, parse_sitemap
from app.services.crawler.ssrf import BlockedDestination, SafeNetworkBackend, check_url, is_public_ip, resolve_public
from app.services.crawler.urls import normalize_url, redact_url


@pytest.mark.parametrize("raw,expected", [
    ("HTTP://Example.COM", "http://example.com/"),
    ("https://example.com:443/a/../b?utm_source=x&id=5#frag", "https://example.com/b?id=5"),
    ("https://example.com/p?gclid=1&fbclid=2&page=2", "https://example.com/p?page=2"),
    ("https://example.com/%7Euser", "https://example.com/~user"),
    ("http://example.com:8080/x", "http://example.com:8080/x"),
    ("javascript:alert(1)", None), ("mailto:a@b.c", None), ("ftp://example.com/", None),
    ("https://user:pw@example.com/", None),
])
def test_normalize(raw, expected):
    assert normalize_url(raw) == expected


def test_relative_and_redact():
    assert normalize_url("../x?q=1", base="https://e.com/a/b/") == "https://e.com/a/x?q=1"
    assert redact_url("https://e.com/cb?code=SECRET&state=abc") == "https://e.com/cb?code=***&state=***"


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.1", "172.16.5.4", "192.168.1.1", "169.254.169.254", "0.0.0.0",
                                "100.64.0.1", "224.0.0.1", "255.255.255.255", "::1", "fe80::1", "fc00::1", "fd00:ec2::254",
                                "::ffff:127.0.0.1", "::ffff:169.254.169.254", "2002:7f00:1::", "64:ff9b::a00:1", "::"])
def test_non_public_ips(ip):
    assert not is_public_ip(ipaddress.ip_address(ip))


@pytest.mark.parametrize("ip", ["93.184.216.34", "8.8.8.8", "2606:4700::1111"])
def test_public_ips(ip):
    assert is_public_ip(ipaddress.ip_address(ip))


@pytest.mark.parametrize("url", ["http://localhost/", "http://127.0.0.1/", "http://169.254.169.254/latest/meta-data/",
                                 "http://metadata.google.internal/", "http://[::1]/", "http://printer.local/",
                                 "http://example.com:22/", "file:///etc/passwd", "gopher://example.com/"])
def test_check_url_blocks(url):
    from app.services.crawler.urls import host_of
    with pytest.raises(BlockedDestination):
        check_url(url, [host_of(url) or "x"])


def test_check_url_allowlist():
    check_url("https://example.com/a", ["example.com"])
    with pytest.raises(BlockedDestination):
        check_url("https://evil.com/a", ["example.com"])


@pytest.mark.parametrize("host", ["2130706433", "0x7f000001", "017700000001", "127.1"])
async def test_alternate_ip_representations_resolve_and_block(host):
    with pytest.raises(BlockedDestination):
        await resolve_public(host, 80)


async def test_dns_rebinding_mixed_answers_blocked(monkeypatch):
    async def fake_getaddrinfo(host, port, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port))]
    import asyncio
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(BlockedDestination):
        await resolve_public("rebind.example", 80)


async def test_backend_connects_to_validated_ip_only(monkeypatch):
    """The socket goes to the address that was validated, never to a re-resolved one."""
    calls = []

    class Inner:
        async def connect_tcp(self, host, port, *a):
            calls.append(host)
            raise OSError("stop")

    async def fake_getaddrinfo(host, port, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
    import asyncio

    import httpcore
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(httpcore.ConnectError):
        await SafeNetworkBackend(inner=Inner()).connect_tcp("example.com", 443)
    assert calls == ["93.184.216.34"]


async def test_real_connection_to_loopback_blocked(fixture_site):
    """Even if the allowlist contains the host, the connection layer refuses loopback."""
    site = fixture_site({"/": (200, {"Content-Type": "text/html"}, "<title>x</title>")})
    policy = FetchPolicy(allowed_hosts=["127.0.0.1"], user_agent="t", allow_private=False)
    # bypass string check to prove the network layer blocks on its own
    from app.services.crawler import fetcher
    orig = fetcher.check_url
    fetcher.check_url = lambda *a, **k: None
    try:
        async with build_client(policy) as c:
            res = await safe_get(c, site.base + "/", policy)
    finally:
        fetcher.check_url = orig
    assert res.blocked and res.status_code is None and site.hits == []


def _mock(routes):
    def handler(req: httpx.Request):
        status, headers, body = routes[str(req.url)]
        return httpx.Response(status, headers=headers, content=body)
    return httpx.MockTransport(handler)


async def test_redirect_to_disallowed_host_blocked():
    t = _mock({"https://a.com/": (301, {"location": "http://169.254.169.254/latest"}, b"")})
    policy = FetchPolicy(allowed_hosts=["a.com"], user_agent="t")
    async with build_client(policy, t) as c:
        res = await safe_get(c, "https://a.com/", policy)
    assert res.blocked and res.redirect_chain[0]["status_code"] == 301


async def test_redirect_loop_and_limit():
    loop = _mock({"https://a.com/1": (302, {"location": "/2"}, b""), "https://a.com/2": (302, {"location": "/1"}, b"")})
    policy = FetchPolicy(allowed_hosts=["a.com"], user_agent="t", max_redirects=5)
    async with build_client(policy, loop) as c:
        res = await safe_get(c, "https://a.com/1", policy)
    assert res.error == "redirect loop"
    chain = {f"https://a.com/{i}": (302, {"location": f"/{i + 1}"}, b"") for i in range(10)}
    policy.max_redirects = 3
    async with build_client(policy, _mock(chain)) as c:
        res = await safe_get(c, "https://a.com/0", policy)
    assert "too many redirects" in res.error


async def test_response_size_limit_and_non_html():
    t = _mock({"https://a.com/big": (200, {"content-type": "text/html"}, b"x" * 5000),
               "https://a.com/f.pdf": (200, {"content-type": "application/pdf"}, b"%PDF")})
    policy = FetchPolicy(allowed_hosts=["a.com"], user_agent="t", max_bytes=1000)
    async with build_client(policy, t) as c:
        big = await safe_get(c, "https://a.com/big", policy)
        pdf = await safe_get(c, "https://a.com/f.pdf", policy)
    assert big.truncated and big.body == b""
    assert pdf.status_code == 200 and not pdf.is_html and pdf.body == b""


def test_parser_extracts_seo_fields():
    body = b"""<html><head><title>A</title><title>B</title><meta name="description" content="D">
    <meta name="robots" content="NOINDEX, follow"><link rel="canonical" href="/canon">
    <link rel="alternate" hreflang="de" href="https://e.com/de/"><script>var x='lots of words here';</script></head>
    <body><h1>One</h1><h1>Two</h1><a href="/in?utm_source=a">In</a><a href="https://other.com/" rel="nofollow">Out</a>
    <a href="#top">x</a><a href="javascript:void(0)">y</a><p>hello world again</p></body></html>"""
    p = parse_html(body, "https://e.com/page")
    assert p.titles == ["A", "B"] and p.meta_descriptions == ["D"] and p.meta_robots == "noindex, follow"
    assert p.canonicals == ["https://e.com/canon"] and p.hreflang == [{"lang": "de", "href": "https://e.com/de/"}]
    assert p.h1 == ["One", "Two"] and [l.href for l in p.links] == ["https://e.com/in", "https://other.com/"]
    assert p.links[1].rel == "nofollow" and p.word_count == 9  # One Two In Out x y hello world again; script text excluded


def test_sitemap_parser_is_xxe_safe():
    evil = b"""<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://e.com/&e;</loc></url></urlset>"""
    pages, _ = parse_sitemap(evil)
    assert all("root:" not in p for p in pages)
    idx = b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><sitemap><loc>https://e.com/s1.xml</loc></sitemap></sitemapindex>'
    assert parse_sitemap(idx) == ([], ["https://e.com/s1.xml"])


async def test_crawler_never_stores_or_sends_cookies():
    seen = []

    def handler(req: httpx.Request):
        seen.append(req.headers.get("cookie"))
        return httpx.Response(200, headers={"set-cookie": "session=1; Path=/", "content-type": "text/html"}, text="x")
    policy = FetchPolicy(allowed_hosts=["a.com"], user_agent="t")
    async with build_client(policy, httpx.MockTransport(handler)) as c:
        await safe_get(c, "https://a.com/", policy)
        await safe_get(c, "https://a.com/2", policy)
        assert list(c.cookies.jar) == []
    assert seen == [None, None]


def test_gzip_sitemap_and_decompression_bomb():
    import gzip

    from app.services.crawler.parser import DecompressionLimit, maybe_gunzip
    sm = b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://e.com/a</loc></url></urlset>'
    assert parse_sitemap(gzip.compress(sm)) == (["https://e.com/a"], [])
    bomb = gzip.compress(b"\0" * (20 * 1024 * 1024))  # ~20 KB compressed → 20 MB
    assert len(bomb) < 100_000
    with pytest.raises(DecompressionLimit):
        maybe_gunzip(bomb, limit=1024 * 1024)
    assert parse_sitemap(gzip.compress(b"<urlset>" + b" " * (60 * 1024 * 1024) + b"</urlset>")) == ([], [])  # > 50 MB refused


def test_billion_laughs_is_inert():
    lol = b"""<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
    <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;"><!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">]>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://e.com/&lol4;</loc></url></urlset>"""
    pages, _ = parse_sitemap(lol)
    assert all(len(p) < 100 for p in pages)


async def test_content_encoding_bomb_bounded():
    import gzip
    big = gzip.compress(b"<html>" + b"a" * (10 * 1024 * 1024))
    t = _mock({"https://a.com/": (200, {"content-type": "text/html", "content-encoding": "gzip"}, big)})
    policy = FetchPolicy(allowed_hosts=["a.com"], user_agent="t", max_bytes=1024 * 1024)
    async with build_client(policy, t) as c:
        res = await safe_get(c, "https://a.com/", policy)
    assert res.truncated and res.body == b""  # decoded size counted, not compressed size


def test_charset_decoding_turkish():
    body = "<html><head><title>Basınç ölçer — İstanbul</title></head><body><h1>Öğütücü</h1></body></html>".encode("utf-8")
    assert parse_html(body, "https://e.com/", "text/html; charset=utf-8").titles == ["Basınç ölçer — İstanbul"]
    assert parse_html(body, "https://e.com/").h1 == ["Öğütücü"]  # no charset anywhere → UTF-8 default, not latin-1
    win = "<html><head><meta charset='windows-1254'><title>Şeker</title></head></html>".encode("cp1254")
    assert parse_html(win, "https://e.com/").titles == ["Şeker"]
    xml_decl = b'<?xml version="1.0" encoding="utf-8"?><html><body><h1>\xc3\x87ay</h1></body></html>'
    assert parse_html(xml_decl, "https://e.com/").h1 == ["Çay"]
