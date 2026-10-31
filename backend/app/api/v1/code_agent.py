"""Module 12: Code Agent & Repository Fix API."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.v1.common import ApiError, get_or_404
from app.auth import Principal, current_principal, get_db
from app.models import CodeFixProposal, CodeRepository, Site
from app.services import claude_code_bridge
from app.services import code_fix_agent as ca

router = APIRouter()


class RepoCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    repo_type: str = Field("local", pattern="^(local|github)$")
    local_path: str | None = None
    github_repo: str | None = None
    default_branch: str = "main"


class ApproveFixIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_approved: bool = True


class ClaudeFixIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo_id: int
    file_path: str = Field(min_length=1, max_length=500)
    issue_description: str = Field(min_length=5, max_length=1000)
    original_snippet: str = Field(min_length=1, max_length=10000)
    root_cause: str = Field("Identified via Claude Code autonomous AST / code inspection", max_length=1000)


@router.get("/claude-code/status")
def get_claude_status(_: Principal = Depends(current_principal)):
    """Returns local Claude Code CLI installation, version, and rate-limit / readiness status."""
    return claude_code_bridge.get_claude_code_status()


@router.get("/sites/{site_id}/code-repositories")
def list_repositories(site_id: int, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    get_or_404(db, Site, site_id, "site")
    rows = db.scalars(select(CodeRepository).where(CodeRepository.site_id == site_id, CodeRepository.org_id == p.org_id)).all()
    return {"items": [ca._repo_out(r) for r in rows]}


@router.post("/sites/{site_id}/code-repositories", status_code=201)
def add_repository(site_id: int, body: RepoCreateIn, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    site = get_or_404(db, Site, site_id, "site")
    repo = CodeRepository(
        org_id=p.org_id,
        site_id=site.id,
        name=body.name,
        repo_type=body.repo_type,
        local_path=body.local_path,
        github_repo=body.github_repo,
        default_branch=body.default_branch,
        detected_framework=ca.detect_framework(body.local_path) if body.local_path else "unknown",
    )
    db.add(repo)
    db.commit()
    return ca._repo_out(repo)


@router.post("/code-repositories/{repo_id}/scan")
def scan_repository(repo_id: int, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    repo = get_or_404(db, CodeRepository, repo_id, "code repository")
    if repo.org_id != p.org_id:
        raise ApiError(404, "not_found", "repo not found")
    proposals = ca.scan_and_generate_proposals(db, repo)
    return {"repository": ca._repo_out(repo), "proposals": proposals}


@router.get("/sites/{site_id}/code-fix-proposals")
def list_proposals(site_id: int, status: str | None = None, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    get_or_404(db, Site, site_id, "site")
    stmt = select(CodeFixProposal).where(CodeFixProposal.site_id == site_id, CodeFixProposal.org_id == p.org_id)
    if status:
        stmt = stmt.where(CodeFixProposal.status == status)
    rows = db.scalars(stmt.order_by(CodeFixProposal.id.desc())).all()
    return {"items": [ca._proposal_out(r) for r in rows]}


@router.post("/code-fix-proposals/{proposal_id}/approve")
def approve_proposal(proposal_id: int, body: ApproveFixIn, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    p_rec = get_or_404(db, CodeFixProposal, proposal_id, "code fix proposal")
    if p_rec.org_id != p.org_id:
        raise ApiError(404, "not_found", "proposal not found")
    p_rec.user_approved = body.user_approved
    db.commit()
    return ca._proposal_out(p_rec)


@router.post("/code-fix-proposals/{proposal_id}/apply")
def apply_proposal(proposal_id: int, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    try:
        return ca.apply_code_fix(db, proposal_id, p.org_id)
    except ValueError as exc:
        raise ApiError(400, "bad_request", str(exc)) from exc


@router.post("/code-fix-proposals/{proposal_id}/rollback")
def rollback_proposal(proposal_id: int, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    try:
        return ca.rollback_code_fix(db, proposal_id, p.org_id)
    except ValueError as exc:
        raise ApiError(400, "bad_request", str(exc)) from exc


@router.post("/code-fix-proposals/claude-generate")
def generate_claude_proposal(body: ClaudeFixIn, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    """Generates an autonomous code fix proposal powered directly by Claude Code CLI."""
    repo = get_or_404(db, CodeRepository, body.repo_id, "code repository")
    if repo.org_id != p.org_id:
        raise ApiError(404, "not_found", "repo not found")
    try:
        return ca.create_claude_code_proposal(
            db=db,
            repo=repo,
            file_path=body.file_path,
            issue_description=body.issue_description,
            original_snippet=body.original_snippet,
            root_cause=body.root_cause,
        )
    except ValueError as exc:
        raise ApiError(400, "bad_request", str(exc)) from exc
