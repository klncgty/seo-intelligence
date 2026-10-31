"""Claude Code Native Subprocess Bridge.

Directly powers the AI layer using the local Claude Code CLI (`claude`),
eliminating the need for third-party API keys or external SDKs.

Directives enforced:
- Zero external API dependencies (uses local `claude` executable).
- Strict non-interactive print mode (`claude -p <prompt> --output-format json`).
- Structured status detection:
  * CLAUDE_CODE_READY: CLI available and responsive.
  * CLAUDE_CODE_RATE_LIMITED: Usage limit reached with reset timestamp captured.
  * CLAUDE_CODE_AUTH_REQUIRED: Authentication needed via `claude auth`.
  * CLAUDE_CODE_NOT_FOUND: CLI binary not installed on host.
- Graceful degradation: never crash calling services when quota is exhausted.
- Strict data provenance: numbers are only cited from trusted database facts.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from typing import Any

logger = logging.getLogger(__name__)

# Search candidates for Claude Code CLI
CLAUDE_SEARCH_PATHS = [
    os.environ.get("CLAUDE_BIN_PATH", ""),
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
    os.path.expanduser("~/.local/bin/claude"),
    os.path.expanduser("~/.npm-global/bin/claude"),
]


def find_claude_binary() -> str | None:
    """Locate the Claude Code CLI executable on the system."""
    for p in CLAUDE_SEARCH_PATHS:
        if p and os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    which_path = shutil.which("claude")
    if which_path and os.path.isfile(which_path) and os.access(which_path, os.X_OK):
        return which_path
    return None


def get_claude_code_status() -> dict[str, Any]:
    """Inspects local Claude Code CLI installation and quota readiness."""
    bin_path = find_claude_binary()
    if not bin_path:
        return {
            "installed": False,
            "binary_path": None,
            "version": None,
            "status": "CLAUDE_CODE_NOT_FOUND",
            "message": "Claude Code CLI ('claude') not found in PATH or standard directories.",
            "label": "AI motoru: Claude Code (Kurulum Bulunamadı)",
        }

    # Version check
    try:
        ver_res = subprocess.run(
            [bin_path, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        version_str = ver_res.stdout.strip() if ver_res.returncode == 0 else "Unknown"
    except Exception as exc:
        version_str = f"Error reading version: {exc}"

    # Non-interactive ping to check authentication & rate-limit status
    status = "CLAUDE_CODE_READY"
    message = "Claude Code CLI hazır ve aktif."
    quota_info = None

    try:
        ping_res = subprocess.run(
            [bin_path, "-p", "ping", "--output-format", "json"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        stdout = ping_res.stdout.strip()
        if stdout.startswith("{") and stdout.endswith("}"):
            try:
                data = json.loads(stdout)
                if data.get("api_error") == "usage_limit_reached" or "limit" in str(data.get("result", "")).lower():
                    status = "CLAUDE_CODE_RATE_LIMITED"
                    message = data.get("result") or "Weekly usage limit reached on Claude Code account."
                    quota_info = {
                        "api_error": data.get("api_error"),
                        "api_error_status": data.get("api_error_status"),
                        "raw_result": data.get("result"),
                    }
                elif data.get("is_error"):
                    status = "CLAUDE_CODE_ERROR"
                    message = data.get("result") or "Claude Code CLI returned an error."
            except json.JSONDecodeError:
                pass
        elif "hit your weekly limit" in ping_res.stderr or "hit your weekly limit" in stdout:
            status = "CLAUDE_CODE_RATE_LIMITED"
            message = stdout or ping_res.stderr.strip()
    except subprocess.TimeoutExpired:
        status = "CLAUDE_CODE_TIMEOUT"
        message = "Claude Code CLI ping timed out after 15 seconds."
    except Exception as exc:
        status = "CLAUDE_CODE_ERROR"
        message = str(exc)

    return {
        "installed": True,
        "binary_path": bin_path,
        "version": version_str,
        "status": status,
        "message": message,
        "quota_info": quota_info,
        "mode": "direct_cli_no_api",
        "label": f"AI motoru: Claude Code ({version_str}) — Doğrudan CLI Entegrasyonu (APIsiz)",
    }


def invoke_claude_cli(
    prompt: str,
    system_prompt: str | None = None,
    cwd: str | None = None,
    timeout: int = 60,
) -> dict[str, Any]:
    """Executes a prompt against the local Claude Code CLI in non-interactive print mode."""
    bin_path = find_claude_binary()
    if not bin_path:
        return {
            "success": False,
            "status": "CLAUDE_CODE_NOT_FOUND",
            "output": None,
            "error": "Claude Code CLI is not installed on this system.",
        }

    cmd = [bin_path, "-p", prompt, "--output-format", "json"]
    if system_prompt:
        cmd.extend(["--append-system-prompt", system_prompt])

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=timeout,
            check=False,
        )
        stdout = proc.stdout.strip()
        stderr = proc.stderr.strip()

        if stdout.startswith("{") and stdout.endswith("}"):
            try:
                data = json.loads(stdout)
                if data.get("api_error") == "usage_limit_reached" or "limit" in str(data.get("result", "")).lower():
                    return {
                        "success": False,
                        "status": "CLAUDE_CODE_RATE_LIMITED",
                        "output": None,
                        "error": data.get("result") or "Weekly usage limit reached.",
                        "raw_json": data,
                    }
                if data.get("is_error"):
                    return {
                        "success": False,
                        "status": "CLAUDE_CODE_ERROR",
                        "output": None,
                        "error": data.get("result") or "Claude Code execution error.",
                        "raw_json": data,
                    }
                # Success
                return {
                    "success": True,
                    "status": "SUCCESS",
                    "output": data.get("result", stdout),
                    "total_cost_usd": data.get("total_cost_usd", 0.0),
                    "raw_json": data,
                }
            except json.JSONDecodeError:
                pass

        if proc.returncode == 0:
            return {
                "success": True,
                "status": "SUCCESS",
                "output": stdout,
                "error": None,
            }

        return {
            "success": False,
            "status": "CLAUDE_CODE_ERROR",
            "output": None,
            "error": stderr or stdout or f"Exited with code {proc.returncode}",
        }

    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "status": "CLAUDE_CODE_TIMEOUT",
            "output": None,
            "error": f"Execution timed out after {timeout}s.",
        }
    except Exception as exc:
        return {
            "success": False,
            "status": "CLAUDE_CODE_ERROR",
            "output": None,
            "error": str(exc),
        }


def generate_claude_code_fix(
    file_path: str,
    original_snippet: str,
    issue_description: str,
    root_cause: str,
    cwd: str | None = None,
) -> dict[str, Any]:
    """Uses Claude Code CLI to analyze code and generate a recommended fix snippet and rationale."""
    prompt = (
        f"You are the autonomous SEO Code Fix Engine.\n"
        f"Target File: {file_path}\n"
        f"Issue: {issue_description}\n"
        f"Root Cause: {root_cause}\n"
        f"Original Snippet:\n```\n{original_snippet}\n```\n\n"
        f"Task:\n"
        f"1. Provide the exact corrected snippet to replace the original snippet.\n"
        f"2. Explain why this fix resolves the SEO issue.\n"
        f"Respond in pure JSON with keys: proposed_snippet, explanation, seo_benefit."
    )
    res = invoke_claude_cli(prompt, cwd=cwd, timeout=45)
    return res


def ask_claude_code_data(question: str, trusted_facts: dict[str, Any]) -> dict[str, Any]:
    """Queries Claude Code with validated semantic data facts to formulate an executive answer."""
    prompt = (
        f"User Question: {question}\n\n"
        f"Strict Rule: You must ONLY cite numbers present in TRUSTED_FACTS below. "
        f"Never extrapolate or guess numbers.\n\n"
        f"TRUSTED_FACTS:\n{json.dumps(trusted_facts, indent=2, ensure_ascii=False)}\n\n"
        f"Provide a concise, professional SEO insight answering the user's question based strictly on these facts."
    )
    res = invoke_claude_cli(prompt, timeout=45)
    return res
