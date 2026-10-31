"""Minimal WARC/1.1 (ISO 28500) response-record writer. One gzip member per record (standard .warc.gz layout)."""

import gzip
import hashlib
import uuid
from datetime import UTC, datetime

MAX_WARC_BODY = 2 * 1024 * 1024


def response_record(url: str, status: int | None, headers: dict[str, str], body: bytes, fetched_at: datetime | None = None) -> bytes:
    body = body[:MAX_WARC_BODY]
    reason = "OK" if status == 200 else ""
    http_head = f"HTTP/1.1 {status or 0} {reason}\r\n" + "".join(
        f"{k}: {v}\r\n" for k, v in headers.items() if k.lower() not in ("set-cookie", "authorization")) + "\r\n"
    payload = http_head.encode("latin-1", "replace") + body
    ts = (fetched_at or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    warc_head = (
        "WARC/1.1\r\n"
        "WARC-Type: response\r\n"
        f"WARC-Record-ID: <urn:uuid:{uuid.uuid4()}>\r\n"
        f"WARC-Date: {ts}\r\n"
        f"WARC-Target-URI: {url}\r\n"
        "Content-Type: application/http; msgtype=response\r\n"
        f"WARC-Payload-Digest: sha256:{hashlib.sha256(body).hexdigest()}\r\n"
        f"Content-Length: {len(payload)}\r\n\r\n")
    return gzip.compress(warc_head.encode() + payload + b"\r\n\r\n")


def parse_record(data: bytes) -> dict:
    raw = gzip.decompress(data)
    head, _, rest = raw.partition(b"\r\n\r\n")
    fields = dict(line.split(": ", 1) for line in head.decode().split("\r\n")[1:])
    n = int(fields["Content-Length"])
    return {"version": head.split(b"\r\n")[0].decode(), "fields": fields, "payload": rest[:n]}
