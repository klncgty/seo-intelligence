"""Logging that never emits tokens, secrets, emails, IPs, or sensitive query-string values."""

import json
import logging
import re

_PATTERNS = [
    (re.compile(r"(?i)(access_token|refresh_token|client_secret|code|state|password|api_key|key|token)=([^&\s\"']+)"), r"\1=***"),
    (re.compile(r"(?i)(authorization:\s*)(bearer|basic)\s+\S+"), r"\1\2 ***"),
    (re.compile(r"\bsgi_[A-Za-z0-9_\-]{8,}\b"), "sgi_***"),
    (re.compile(r"ya29\.[\w\-.]+"), "ya29.***"),
    (re.compile(r"1//[\w\-]{20,}"), "1//***"),
    (re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"), "***@***"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "***.***.***.***"),
]


def redact(text: str) -> str:
    for pat, repl in _PATTERNS:
        text = pat.sub(repl, text)
    return text


def format_json_record(record: logging.LogRecord) -> str:
    from app.infra import observability as obs
    ctx = obs.current_trace()
    payload = {
        "level": record.levelname,
        "logger": record.name,
        "message": redact(record.getMessage()),
        "trace_id": ctx[0] if ctx else None,
        "span_id": ctx[1] if ctx else None,
    }
    return json.dumps(payload, ensure_ascii=False)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        from app.infra import observability as obs
        ctx = obs.current_trace()
        record.trace_id = ctx[0] if ctx else "-"
        record.msg = redact(record.getMessage())
        record.args = None
        return True


def setup_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if any(isinstance(f, RedactingFilter) for h in root.handlers for f in h.filters):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter())
    root.addHandler(handler)
    root.setLevel(level)
    # httpx logs full request URLs at INFO; keep it quiet.
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
