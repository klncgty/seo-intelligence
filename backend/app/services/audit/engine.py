"""Audit engine: runs all rules, dedupes and sorts findings deterministically."""
from __future__ import annotations

import json

from app.services.audit.rules import RULES
from app.services.audit.types import AuditInput, Finding

SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}

__all__ = ["RULES", "SEVERITY_RANK", "run_audit"]


def _ev(f: Finding) -> str:
    return json.dumps(f.evidence, sort_keys=True, default=str)


def run_audit(inp: AuditInput) -> list[Finding]:
    """Run every registered rule; dedupe by (rule_id, url, evidence); sort stably."""
    seen: set[tuple[str, str | None, str]] = set()
    out: list[Finding] = []
    for rule in RULES:
        for f in rule(inp):
            key = (f.rule_id, f.url, _ev(f))
            if key not in seen:
                seen.add(key)
                out.append(f)
    out.sort(key=lambda f: (SEVERITY_RANK.get(f.severity, 99), f.rule_id, f.url or "", _ev(f)))
    return out
