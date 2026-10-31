"""Test suite for direct Claude Code CLI integration (Zero external API / SDK).

Verifies:
1. Claude Code CLI discovery and status reporting.
2. Graceful rate-limit and error handling (without application crash).
3. Code fix proposal generation via Claude Code CLI bridge.
4. Backward compatibility with AI assistant endpoints.
5. Strict adherence to zero-fake-data and approval gates.
"""

from unittest.mock import MagicMock, patch
import json
import pytest

from app.models import CodeRepository, Site
from app.services import claude_code_bridge as ccb


def test_claude_binary_discovery():
    bin_path = ccb.find_claude_binary()
    assert bin_path is not None
    assert "claude" in bin_path


def test_claude_code_status_real():
    status = ccb.get_claude_code_status()
    assert status["installed"] is True
    assert status["binary_path"] is not None
    assert "Claude Code" in status["version"]
    assert status["mode"] == "direct_cli_no_api"
    assert status["status"] in ("CLAUDE_CODE_READY", "CLAUDE_CODE_RATE_LIMITED", "CLAUDE_CODE_ERROR")


def test_claude_code_status_endpoint(client):
    res = client.get("/api/v1/claude-code/status")
    assert res.status_code == 200
    data = res.json()
    assert data["installed"] is True
    assert data["mode"] == "direct_cli_no_api"
    assert "version" in data


def test_ai_assistant_status_uses_claude_code(client):
    res = client.get("/api/v1/ai-assistant/status")
    assert res.status_code == 200
    data = res.json()
    assert data["provider"] == "claude_code_cli"
    assert data["enabled"] is True
    assert "Claude Code" in data["note"]


def test_invoke_claude_cli_mock_success(monkeypatch):
    mock_json = {
        "result": "Here is the optimized SEO proposal",
        "api_error": None,
        "is_error": False,
        "total_cost_usd": 0.0,
    }

    mock_run = MagicMock()
    mock_run.returncode = 0
    mock_run.stdout = json.dumps(mock_json)
    mock_run.stderr = ""

    with patch("subprocess.run", return_value=mock_run):
        res = ccb.invoke_claude_cli("Optimize this title tag")
        assert res["success"] is True
        assert res["status"] == "SUCCESS"
        assert res["output"] == "Here is the optimized SEO proposal"


def test_invoke_claude_cli_rate_limited(monkeypatch):
    mock_json = {
        "api_error": "usage_limit_reached",
        "api_error_status": 429,
        "result": "You've hit your weekly limit · resets 5pm (Europe/Istanbul)",
        "is_error": True,
    }

    mock_run = MagicMock()
    mock_run.returncode = 1
    mock_run.stdout = json.dumps(mock_json)
    mock_run.stderr = ""

    with patch("subprocess.run", return_value=mock_run):
        res = ccb.invoke_claude_cli("Optimize this title tag")
        assert res["success"] is False
        assert res["status"] == "CLAUDE_CODE_RATE_LIMITED"
        assert "weekly limit" in res["error"]


def test_claude_code_proposal_generation_flow(client, db, tmp_path):
    site = client.post("/api/v1/sites", json={"name": "Claude Test Site", "base_url": "https://claude-test.com"}).json()

    test_file = tmp_path / "index.html"
    test_file.write_text("<html><head></head><body><h1>Hello</h1></body></html>")

    repo = CodeRepository(
        org_id=site["org_id"],
        site_id=site["id"],
        name="test-repo",
        repo_type="local",
        local_path=str(tmp_path),
        detected_framework="static_html",
    )
    db.add(repo)
    db.commit()

    # Call claude code proposal generation endpoint
    res = client.post(
        "/api/v1/code-fix-proposals/claude-generate",
        json={
            "repo_id": repo.id,
            "file_path": "index.html",
            "issue_description": "Document lacks standard viewport tag for mobile responsiveness",
            "original_snippet": "<head></head>",
            "root_cause": "Empty HTML head template",
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["category"] == "claude_code_ai"
    assert data["file_path"] == "index.html"
    assert data["user_approved"] is False
    assert data["status"] == "proposed"
    assert "verification_report" in data
    assert data["verification_report"]["ai_engine"] == "Claude Code CLI"

    # User Approval Gate: cannot apply without explicit approval
    apply_res = client.post(f"/api/v1/code-fix-proposals/{data['id']}/apply")
    assert apply_res.status_code == 400
    assert "explicit user approval" in apply_res.json()["error"]["message"]
