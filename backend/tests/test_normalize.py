import ipaddress

import pytest
from hypothesis import given, settings as hsettings
from hypothesis import strategies as st

from app.normalize.domains import public_suffix, registrable_domain
from app.normalize.locale import DEFAULT_DATABASE, SearchContext, get_locale
from app.normalize.text import (
    dedupe_keywords,
    detect_language,
    is_question,
    match_key,
    normalize_keyword,
    stem_tokens,
    tr_lower,
    tr_upper,
)
from app.services.crawler.ssrf import is_public_ip
from app.services.crawler.urls import normalize_url


@pytest.mark.parametrize("host,expected", [
    ("www.example.com", "example.com"), ("shop.example.com.tr", "example.com.tr"), ("example.com.tr", "example.com.tr"),
    ("a.b.c.example.org.tr", "example.org.tr"), ("foo.github.io", "foo.github.io"), ("bar.foo.github.io", "foo.github.io"),
    ("www.ck", "www.ck"), ("x.y.ck", "x.y.ck"), ("example.CO.UK.", "example.co.uk"), ("münchen.de", "xn--mnchen-3ya.de"),
    ("com.tr", None), ("com", None), ("192.168.1.1", None), ("", None), ("a..b.com", None),
    ("example.unknowntld", "example.unknowntld"),
])
def test_registrable_domain(host, expected):
    assert registrable_domain(host) == expected


def test_public_suffix():
    assert public_suffix("a.example.com.tr") == "com.tr" and public_suffix("x.bel.tr") == "bel.tr"


def test_turkish_casing():
    assert tr_lower("İSTANBUL IĞDIR") == "istanbul ığdır"
    assert "ISTANBUL".lower() == "istanbul" and tr_lower("ISTANBUL") == "ıstanbul"  # why str.lower() is wrong for TR
    assert tr_upper("istanbul ılık") == "İSTANBUL ILIK"
    assert match_key("Çağrı Şöför İĞNE Ölçü") == "cagri sofor igne olcu"
    assert match_key("Café  Crème", "en") == "cafe creme"


def test_keyword_normalization_dedupe_and_language():
    assert normalize_keyword("  KOŞU Ayakkabısı!!  ") == "koşu ayakkabısı"
    groups = dedupe_keywords(["Koşu ayakkabısı", "koşu ayakkabısı", "kosu ayakkabisi", "koşu ayakkabıları", "spor çanta"])
    assert len(groups) == 2
    variants = next(g for g in groups if g["canonical"] == "koşu ayakkabısı")["variants"]
    assert "kosu ayakkabisi" in variants and "koşu ayakkabıları" in variants
    assert stem_tokens("kitapları evlerimizden") == ["kitap", "ev"]
    assert stem_tokens("running shoes", "en") == ["run", "shoe"]
    assert detect_language("en iyi koşu ayakkabısı nasıl seçilir") == "tr"
    assert detect_language("how to choose the best running shoes") == "en"
    assert is_question("koşu ayakkabısı nasıl seçilir") and is_question("bu ürün iyi mi") and not is_question("koşu ayakkabısı")
    assert is_question("how to tie shoes", "en")


def test_locale_model():
    ctx = SearchContext(DEFAULT_DATABASE, "mobile", "Istanbul")
    assert ctx.locale.engine == "google.com.tr" and ctx.locale.currency == "TRY"
    with pytest.raises(ValueError):
        SearchContext("xx-XX:google.xx")
    with pytest.raises(ValueError):
        SearchContext(DEFAULT_DATABASE, "tablet")
    assert get_locale("en-US:google.com").country == "US"


@given(st.text(min_size=0, max_size=60))
@hsettings(max_examples=300)
def test_match_key_properties(s):
    k = match_key(s)
    assert match_key(k) == k  # idempotent
    assert not any(c in k for c in "çğıöşüÇĞİÖŞÜ")


@given(st.text(max_size=40))
@hsettings(max_examples=300)
def test_normalize_keyword_idempotent(s):
    n = normalize_keyword(s)
    assert normalize_keyword(n) == n


@given(st.from_regex(r"https?://[a-z]{1,10}\.(com|com\.tr|org)(/[a-z0-9._~-]{0,10}){0,3}(\?[a-z]{1,5}=[a-z0-9]{0,5})?", fullmatch=True))
@hsettings(max_examples=300)
def test_url_normalization_idempotent(url):
    n = normalize_url(url)
    assert n is not None and normalize_url(n) == n


@given(st.ip_addresses())
@hsettings(max_examples=500)
def test_ssrf_policy_never_allows_special_ranges(ip):
    if is_public_ip(ip):
        check_ip = ip.ipv4_mapped if (isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped) else ip
        assert not (check_ip.is_private or check_ip.is_loopback or check_ip.is_link_local or check_ip.is_multicast or check_ip.is_reserved)


@given(st.from_regex(r"([a-z0-9]{1,8}\.){0,3}[a-z0-9]{1,10}\.(com\.tr|org\.tr|com|co\.uk|de)", fullmatch=True))
@hsettings(max_examples=300)
def test_registrable_domain_properties(host):
    rd = registrable_domain(host)
    assert rd is not None and host.endswith(rd) and registrable_domain(rd) == rd
