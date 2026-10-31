"""Common Crawl WAT importer (backlink bootstrap). Streaming, bounded, resumable.

WAT files are concatenated gzip members (one WARC record each) whose JSON payload lists page links
(Envelope.Payload-Metadata.HTTP-Response-Metadata.HTML-Metadata.Links). We keep only links whose target
registrable domain is in `targets` (or ends with a target TLD suffix like ".tr").
Resume: the checkpoint stores the compressed byte offset of the last complete gzip member; a resumed
download starts there with an HTTP Range request. Usage is subject to the Common Crawl Terms of Use
(https://commoncrawl.org/terms-of-use) — see docs/data-licensing.md.
"""

import json
import zlib
from collections.abc import Iterable, Iterator

import httpx

from app.normalize.domains import registrable_domain
from app.services.crawler.urls import normalize_url

CC_HOST = "data.commoncrawl.org"
MAX_RECORD_BYTES = 20 * 1024 * 1024


def iter_members(chunks: Iterable[bytes], start_offset: int = 0) -> Iterator[tuple[bytes, int]]:
    """Yield (decompressed member, compressed offset right after that member) for multi-member gzip streams.
    Offsets advance only at member boundaries, so they are safe resume points."""
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    buf = bytearray()
    offset, member_in = start_offset, 0
    for chunk in chunks:
        data = chunk
        while data:
            buf += d.decompress(data, MAX_RECORD_BYTES + 1 - len(buf))
            if len(buf) > MAX_RECORD_BYTES:
                raise ValueError("WAT record exceeds size limit")
            if d.eof:
                member_in += len(data) - len(d.unused_data)
                offset += member_in
                yield bytes(buf), offset
                buf.clear()
                member_in = 0
                data = d.unused_data
                d = zlib.decompressobj(16 + zlib.MAX_WBITS)
            elif d.unconsumed_tail:
                member_in += len(data) - len(d.unconsumed_tail)
                data = d.unconsumed_tail
            else:
                member_in += len(data)
                data = b""
    # a trailing partial member is discarded; resuming restarts at its first byte


def _matches(domain: str, targets: set[str]) -> bool:
    return domain in targets or any(t.startswith(".") and domain.endswith(t) for t in targets)


def links_from_record(record: bytes, targets: set[str]) -> list[dict]:
    head, _, body = record.partition(b"\r\n\r\n")
    if b"application/json" not in head:
        return []
    try:
        env = json.loads(body.strip().split(b"\r\n\r\n")[0])["Envelope"]
    except (ValueError, KeyError):
        return []
    src = env.get("WARC-Header-Metadata", {}).get("WARC-Target-URI")
    meta = env.get("Payload-Metadata", {}).get("HTTP-Response-Metadata", {}).get("HTML-Metadata", {})
    date = (env.get("WARC-Header-Metadata", {}).get("WARC-Date") or "")[:10] or None
    if not src or not meta:
        return []
    src_dom = registrable_domain((src.split("/") + ["", "", ""])[2]) or ""
    out = []
    for link in meta.get("Links", [])[:5000]:
        if not str(link.get("path", "")).startswith(("A@/href", "IMG@/src")) or not link.get("url"):
            continue
        tgt = normalize_url(link["url"], base=src)
        if not tgt:
            continue
        td = registrable_domain(tgt.split("/")[2]) or ""
        if td == src_dom or not _matches(td, targets):
            continue
        out.append({"source_url": src, "target_url": tgt, "anchor": link.get("text") or link.get("alt") or "",
                    "rel": link.get("rel") or "", "is_image": link["path"].startswith("IMG"), "first_seen": date,
                    "last_seen": date, "link_position": "unknown"})
    return out


def import_stream(ch, chunks: Iterable[bytes], targets: set[str], *, start_offset: int = 0, max_records: int = 1_000_000,
                  checkpoint=None, batch: int = 5000) -> dict:
    from app.services import backlinks
    pending, records, links, offset = [], 0, 0, start_offset
    for rec, offset in iter_members(chunks, start_offset):
        records += 1
        pending += links_from_record(rec, targets)
        if len(pending) >= batch:
            links += backlinks.ingest(ch, pending, "common_crawl", "OBSERVED")
            pending = []
            if checkpoint:
                checkpoint({"offset": offset, "records": records, "links": links})
        if records >= max_records:
            break
    if pending:
        links += backlinks.ingest(ch, pending, "common_crawl", "OBSERVED")
    if checkpoint:
        checkpoint({"offset": offset, "records": records, "links": links, "done": records < max_records})
    return {"records": records, "links": links, "offset": offset}


def http_chunks(path: str, start_offset: int = 0, max_bytes: int = 2 * 1024 ** 3, timeout: float = 120) -> Iterator[bytes]:
    """Stream a Common Crawl file (only data.commoncrawl.org), resuming with a Range request."""
    if path.startswith("http"):
        if httpx.URL(path).host != CC_HOST:
            raise ValueError("only data.commoncrawl.org is allowed")
        url = path
    else:
        url = f"https://{CC_HOST}/{path.lstrip('/')}"
    headers = {"User-Agent": "SEOGrowthIntelligenceBot/0.2 (+backlink bootstrap)"}
    if start_offset:
        headers["Range"] = f"bytes={start_offset}-"
    read = 0
    with httpx.stream("GET", url, headers=headers, timeout=timeout, follow_redirects=False, trust_env=False) as r:
        r.raise_for_status()
        for chunk in r.iter_raw(1 << 20):
            read += len(chunk)
            if read > max_bytes:
                raise ValueError(f"download exceeds {max_bytes} bytes limit")
            yield chunk


def unreverse_domain(rev: str) -> str:
    """Convert Common Crawl web-graph reverse domain notation (``tr.com.ornek``) to normal domain (``ornek.com.tr``)."""
    parts = [p.strip() for p in rev.strip().lower().split(".") if p.strip()]
    return ".".join(reversed(parts))


def import_webgraph(ch, vertices_lines: Iterable[str], edges_lines: Iterable[str], targets: set[str], *,
                    max_vertices: int = 2_000_000, max_edges: int = 5_000_000, batch: int = 5000) -> dict:
    """Stream Common Crawl domain/host web-graph (vertices + edges TSV) and ingest edges pointing to ``targets``."""
    from app.services import backlinks

    nodes: dict[int, str] = {}
    for line in vertices_lines:
        if len(nodes) >= max_vertices:
            break
        parts = line.strip().split("\t")
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        dom = registrable_domain(unreverse_domain(parts[1]))
        if dom:
            nodes[int(parts[0])] = dom

    pending: list[dict] = []
    edges_read = links_ingested = 0
    for line in edges_lines:
        if edges_read >= max_edges:
            break
        parts = line.strip().split("\t")
        if len(parts) < 2 or not (parts[0].isdigit() and parts[1].isdigit()):
            continue
        edges_read += 1
        src_dom = nodes.get(int(parts[0]))
        tgt_dom = nodes.get(int(parts[1]))
        if not src_dom or not tgt_dom or src_dom == tgt_dom or not _matches(tgt_dom, targets):
            continue
        pending.append({
            "source_url": f"https://{src_dom}/",
            "target_url": f"https://{tgt_dom}/",
            "anchor": "",
            "rel": "",
            "is_image": False,
            "link_position": "webgraph",
        })
        if len(pending) >= batch:
            links_ingested += backlinks.ingest(ch, pending, "common_crawl", "OBSERVED")
            pending = []
    if pending:
        links_ingested += backlinks.ingest(ch, pending, "common_crawl", "OBSERVED")
    return {"vertices_loaded": len(nodes), "edges_scanned": edges_read, "links_ingested": links_ingested}
