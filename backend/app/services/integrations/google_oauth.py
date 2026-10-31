"""Google OAuth 2.0 (authorization code + PKCE) helpers."""
from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from app.services.integrations.retry import request_with_retry

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = [
    "https://www.googleapis.com/auth/webmasters.readonly",
    "https://www.googleapis.com/auth/analytics.readonly",
]


@dataclass(frozen=True)
class TokenResponse:
    access_token: str
    refresh_token: str | None
    expires_in: int
    scope: str


def generate_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, S256 code_challenge)."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def build_authorization_url(client_id: str, redirect_uri: str, state: str, code_challenge: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def _parse(data: dict, fallback_refresh: str | None = None) -> TokenResponse:
    return TokenResponse(
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token") or fallback_refresh,
        expires_in=int(data.get("expires_in", 0)),
        scope=data.get("scope", ""),
    )


async def exchange_code(
    client: httpx.AsyncClient,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    code: str,
    code_verifier: str,
) -> TokenResponse:
    resp = await request_with_retry(
        client,
        "POST",
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "code": code,
            "code_verifier": code_verifier,
        },
    )
    return _parse(resp.json())


async def refresh_access_token(
    client: httpx.AsyncClient, client_id: str, client_secret: str, refresh_token: str
) -> TokenResponse:
    """Refresh an access token; the refresh token is echoed back if Google omits it."""
    resp = await request_with_retry(
        client,
        "POST",
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        },
    )
    return _parse(resp.json(), fallback_refresh=refresh_token)
