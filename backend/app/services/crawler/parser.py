"""HTML / robots / sitemap parsing with lxml. Never executes page JavaScript."""

import hashlib
import re
from dataclasses import dataclass, field
from urllib.robotparser import RobotFileParser

from lxml import etree, html

from app.services.crawler.urls import normalize_url

_WS = re.compile(r"\s+")


@dataclass
class ParsedLink:
    href: str  # normalized absolute
    anchor_text: str
    rel: str | None
    position: str = "content"  # content | nav | footer | header


@dataclass
class ParsedPage:
    titles: list[str] = field(default_factory=list)
    meta_descriptions: list[str] = field(default_factory=list)
    canonicals: list[str] = field(default_factory=list)
    meta_robots: str | None = None
    h1: list[str] = field(default_factory=list)
    hreflang: list[dict] = field(default_factory=list)
    links: list[ParsedLink] = field(default_factory=list)
    word_count: int = 0
    content_hash: str | None = None
    structured_data: list[dict] = field(default_factory=list)  # [{format, type, fields:[...], data?}]
    simhash: int | None = None
    template_sig: str | None = None
    images_missing_alt: int = 0
    stylesheets: list[str] = field(default_factory=list)
    scripts: list[str] = field(default_factory=list)
    unminified_resources: list[str] = field(default_factory=list)
    text: str = ""


def is_unminified_code(content: str) -> bool:
    """Heuristic for detecting unminified JavaScript or CSS source code."""
    content = content.strip()
    if len(content) < 300:
        return False
    lines = content.splitlines()
    if len(lines) <= 3:
        return False
    has_indent = any(line.startswith("  ") or line.startswith("\t") for line in lines[:30])
    has_comments = "/*" in content or "//" in content
    avg_line_len = len(content) / max(1, len(lines))
    return avg_line_len < 120 and (has_indent or has_comments)


def _text(el) -> str:
    return _WS.sub(" ", " ".join(el.itertext())).strip()


def _anchor_text(a) -> str:
    text = _text(a)
    if text:
        return text
    aria = (a.get("aria-label") or a.get("title") or "").strip()
    if aria:
        return aria
    alts = [img.get("alt").strip() for img in a.xpath(".//img[@alt]") if img.get("alt")]
    if alts:
        return " ".join(alts)
    return ""


_META_CHARSET = re.compile(rb"""<meta[^>]+charset=["']?([A-Za-z0-9_\-]+)""", re.I)


def decode_html(body: bytes | str, content_type: str | None = None) -> str:
    """Decode with the HTTP charset, then <meta charset>, then UTF-8 (lxml would otherwise guess latin-1 and
    garble Turkish characters on pages without a meta charset)."""
    if isinstance(body, str):
        return body
    enc = None
    if content_type and "charset=" in content_type.lower():
        enc = content_type.lower().split("charset=")[-1].split(";")[0].strip().strip('"\'')
    if not enc:
        m = _META_CHARSET.search(body[:4096])
        enc = m.group(1).decode("ascii", "ignore") if m else "utf-8"
    try:
        text = body.decode(enc, "replace")
    except LookupError:
        text = body.decode("utf-8", "replace")
    return re.sub(r"^\s*<\?xml[^>]*\?>", "", text)  # lxml rejects str input with an encoding declaration


def parse_html(body: bytes | str, page_url: str, content_type: str | None = None) -> ParsedPage:
    out = ParsedPage()
    text_in = decode_html(body, content_type)
    if not text_in.strip():
        return out
    try:
        doc = html.fromstring(text_in)
    except (etree.ParserError, ValueError):
        return out
    base = page_url
    base_el = doc.find(".//base[@href]")
    if base_el is not None:
        base = normalize_url(base_el.get("href"), base=page_url) or page_url

    out.titles = [_text(t) for t in doc.xpath("//title")]
    for m in doc.xpath("//meta[@name]"):
        name = (m.get("name") or "").strip().lower()
        content = (m.get("content") or "").strip()
        if name == "description":
            out.meta_descriptions.append(content)
        elif name in ("robots", "googlebot"):
            out.meta_robots = ", ".join(filter(None, [out.meta_robots, content.lower()]))
    for link in doc.xpath("//link[@rel][@href]"):
        rels = (link.get("rel") or "").lower().split()
        href = normalize_url(link.get("href"), base=base)
        if "canonical" in rels:
            out.canonicals.append(href or link.get("href"))
        if "alternate" in rels and link.get("hreflang"):
            out.hreflang.append({"lang": link.get("hreflang").strip(), "href": href or link.get("href")})
        if "stylesheet" in rels and href:
            out.stylesheets.append(href)
    out.h1 = [_text(h) for h in doc.xpath("//h1")]
    for a in doc.xpath("//a[@href]"):
        raw = a.get("href").strip()
        if raw.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
            continue
        href = normalize_url(raw, base=base)
        if href:
            out.links.append(ParsedLink(href=href, anchor_text=_anchor_text(a)[:500], rel=(a.get("rel") or None),
                                        position=_link_position(a)))

    out.structured_data = extract_structured_data(doc)
    imgs = doc.xpath("//img")
    out.images_missing_alt = sum(1 for i in imgs if not (i.get("alt") or "").strip() and i.get("role") != "presentation")
    out.template_sig = template_signature(doc)

    for sc in doc.xpath("//script"):
        src = sc.get("src")
        if src:
            norm_src = normalize_url(src, base=base)
            if norm_src:
                out.scripts.append(norm_src)
        else:
            stype = (sc.get("type") or "").lower()
            if not stype or "javascript" in stype or "ecmascript" in stype:
                if is_unminified_code(sc.text or ""):
                    out.unminified_resources.append("inline:script")

    for st in doc.xpath("//style"):
        if is_unminified_code(st.text or ""):
            out.unminified_resources.append("inline:style")

    for bad in doc.xpath("//script | //style | //noscript | //template"):
        bad.drop_tree()
    body_el = doc.find(".//body")
    text = _text(body_el if body_el is not None else doc)
    out.word_count = len(text.split())
    out.content_hash = hashlib.sha256(text.encode()).hexdigest()
    out.simhash = simhash(text)
    out.text = text[:20000]
    return out


MAX_JSONLD_BYTES = 200_000


def validate_schema_node(node: dict) -> list[str]:
    """Validates schema.org entity according to Google Rich Result & Schema.org standards."""
    errors = []
    t = node.get("@type")
    if t == "Product":
        offers = node.get("offers")
        if offers:
            off_list = offers if isinstance(offers, list) else [offers]
            for o in off_list:
                if isinstance(o, dict):
                    seller = o.get("seller")
                    if seller and isinstance(seller, dict):
                        if not seller.get("@type") and not seller.get("name"):
                            errors.append("Offer seller has no '@type' or 'name'")
                    ship = o.get("shippingDetails")
                    if ship and isinstance(ship, dict):
                        rate = ship.get("shippingRate")
                        if rate and isinstance(rate, dict):
                            if rate.get("@type") != "MonetaryAmount":
                                errors.append(f"shippingRate @type '{rate.get('@type')}' is invalid; must be 'MonetaryAmount'")
    return errors


def extract_structured_data(doc) -> list[dict]:
    """JSON-LD (parsed, size-limited), Microdata itemtypes and RDFa typeof. Never executes scripts."""
    import json
    items: list[dict] = []
    for s in doc.xpath("//script[@type='application/ld+json']")[:20]:
        raw = (s.text or "").strip()
        if not raw or len(raw) > MAX_JSONLD_BYTES:
            items.append({"format": "json-ld", "type": None, "error": "empty or too large"})
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            items.append({"format": "json-ld", "type": None, "error": "invalid JSON"})
            continue
        nodes = data if isinstance(data, list) else (data.get("@graph") if isinstance(data, dict) and "@graph" in data else [data])
        for n in nodes[:50]:
            if isinstance(n, dict):
                t = n.get("@type")
                val_errors = validate_schema_node(n)
                err_msg = "; ".join(val_errors) if val_errors else None
                items.append({
                    "format": "json-ld",
                    "type": t if isinstance(t, str) else (t[0] if isinstance(t, list) and t else None),
                    "fields": sorted(k for k in n if not k.startswith("@"))[:100],
                    "data": _shallow(n),
                    "error": err_msg,
                })
    for el in doc.xpath("//*[@itemscope][@itemtype]")[:50]:
        t = el.get("itemtype", "").rstrip("/").split("/")[-1]
        props = sorted({p.get("itemprop") for p in el.xpath(".//*[@itemprop]") if p.get("itemprop")})[:100]
        items.append({"format": "microdata", "type": t, "fields": props})
    for el in doc.xpath("//*[@typeof]")[:50]:
        props = sorted({p.get("property") for p in el.xpath(".//*[@property]") if p.get("property")})[:100]
        items.append({"format": "rdfa", "type": el.get("typeof").split(":")[-1], "fields": props})
    return items


def _shallow(n: dict) -> dict:
    out = {}
    for k, v in list(n.items())[:50]:
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v if not isinstance(v, str) else v[:300]
        elif isinstance(v, dict):
            out[k] = {"@type": v.get("@type")}
        elif isinstance(v, list):
            out[k] = f"list[{len(v)}]"
    return out


def simhash(text: str, bits: int = 64) -> int | None:
    """64-bit SimHash over word 3-shingles (signed to fit Postgres BIGINT)."""
    words = text.lower().split()
    if len(words) < 3:
        return None
    v = [0] * bits
    for i in range(len(words) - 2):
        h = int.from_bytes(hashlib.blake2b(" ".join(words[i:i + 3]).encode(), digest_size=8).digest(), "big")
        for b in range(bits):
            v[b] += 1 if h >> b & 1 else -1
    val = sum(1 << b for b in range(bits) if v[b] > 0)
    return val - (1 << 64) if val >= 1 << 63 else val


def hamming(a: int, b: int) -> int:
    return bin((a ^ b) & ((1 << 64) - 1)).count("1")


def template_signature(doc) -> str | None:
    """Hash of the DOM skeleton (tag paths to depth 4, ids/classes/text ignored) → groups pages built from one template."""
    body = doc.find(".//body")
    if body is None:
        return None
    paths = []

    def walk(el, path, depth):
        if depth > 4 or len(paths) > 300:
            return
        for child in el:
            if not isinstance(child.tag, str) or child.tag in ("script", "style", "noscript"):
                continue
            p = f"{path}/{child.tag}"
            paths.append(p)
            walk(child, p, depth + 1)
    walk(body, "body", 0)
    skeleton = "\n".join(sorted(set(paths)))
    return hashlib.sha256(skeleton.encode()).hexdigest()[:16]


def _link_position(el) -> str:
    for anc in el.iterancestors():
        tag = anc.tag if isinstance(anc.tag, str) else ""
        cls = f"{anc.get('class', '')} {anc.get('id', '')} {anc.get('role', '')}".lower()
        if tag == "footer" or "footer" in cls or "contentinfo" in cls:
            return "footer"
        if tag == "nav" or "navigation" in cls or "menu" in cls.split():
            return "nav"
        if tag == "header" or "banner" in cls:
            return "header"
    return "content"


def parse_robots(content: str, url: str) -> RobotFileParser:
    rp = RobotFileParser(url)
    rp.parse(content.splitlines())
    return rp


def robots_sitemaps(content: str) -> list[str]:
    return [line.split(":", 1)[1].strip() for line in content.splitlines() if line.lower().startswith("sitemap:")]


MAX_SITEMAP_DECOMPRESSED = 50 * 1024 * 1024  # sitemaps protocol limit is 50 MB uncompressed


class DecompressionLimit(ValueError):
    pass


def maybe_gunzip(body: bytes, limit: int = MAX_SITEMAP_DECOMPRESSED) -> bytes:
    """Decompress .xml.gz safely: stop as soon as output exceeds `limit` (gzip bomb protection)."""
    if not body.startswith(b"\x1f\x8b"):
        return body
    import zlib
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = bytearray()
    data = body
    while data:
        chunk = d.decompress(data, max(1, limit + 1 - len(out)))
        out += chunk
        if len(out) > limit:
            raise DecompressionLimit(f"decompressed sitemap exceeds {limit} bytes")
        data = d.unconsumed_tail
        if not chunk and not data:
            break
    return bytes(out)


def parse_sitemap(body: bytes) -> tuple[list[str], list[str]]:
    """Return (page_urls, child_sitemap_urls). Safe parser: no DTD/entities/network."""
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)
    try:
        root = etree.fromstring(maybe_gunzip(body), parser)
    except (etree.XMLSyntaxError, DecompressionLimit, OSError):
        return [], []
    locs = [(el.text or "").strip() for el in root.iter("{*}loc")]
    tag = etree.QName(root).localname.lower()
    return ([], locs) if tag == "sitemapindex" else (locs, [])
