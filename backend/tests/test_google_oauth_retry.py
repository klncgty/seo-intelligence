import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from app.services.integrations import google_oauth as go
from app.services.integrations import retry as rt


@pytest.fixture(autouse=True)
def no_delay(monkeypatch):
    monkeypatch.setattr(rt, "DEFAULT_BASE_DELAY", 0)


def test_auth_url_and_pkce():
    verifier, challenge = go.generate_pkce_pair()
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected
    q = parse_qs(urlparse(go.build_authorization_url("cid", "http://x/cb", "st", challenge)).query)
    assert q["access_type"] == ["offline"] and q["prompt"] == ["consent"]
    assert q["include_granted_scopes"] == ["true"]
    assert q["code_challenge_method"] == ["S256"] and q["code_challenge"] == [challenge]
    assert q["scope"] == [" ".join(go.SCOPES)] and q["state"] == ["st"]


@respx.mock
async def test_exchange_and_refresh():
    route = respx.post(go.TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "a", "refresh_token": "r", "expires_in": 3599, "scope": "s"})
    )
    async with httpx.AsyncClient() as c:
        t = await go.exchange_code(c, "cid", "sec", "http://x/cb", "code", "ver")
        assert (t.access_token, t.refresh_token, t.expires_in) == ("a", "r", 3599)
        body = parse_qs(route.calls[0].request.content.decode())
        assert body["code_verifier"] == ["ver"] and body["grant_type"] == ["authorization_code"]
        respx.post(go.TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "b", "expires_in": 10}))
        t2 = await go.refresh_access_token(c, "cid", "sec", "r")
        assert t2.access_token == "b" and t2.refresh_token == "r"


@respx.mock
async def test_retry_then_success():
    route = respx.get("https://e.test/x").mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "0"}), httpx.Response(503), httpx.Response(200, json={"ok": 1})]
    )
    async with httpx.AsyncClient() as c:
        r = await rt.request_with_retry(c, "GET", "https://e.test/x")
    assert r.json() == {"ok": 1} and route.call_count == 3


@respx.mock
async def test_retry_after_is_honored_with_zero_backoff(monkeypatch):
    route = respx.get("https://e.test/x").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "3"}),
            httpx.Response(200, json={"ok": 1}),
        ]
    )
    delays = []

    async def record_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(rt.asyncio, "sleep", record_sleep)
    async with httpx.AsyncClient() as c:
        await rt.request_with_retry(c, "GET", "https://e.test/x", base_delay=0)
    assert route.call_count == 2
    assert delays == [3.0]


@respx.mock
async def test_retry_transport_error_then_success():
    respx.get("https://e.test/x").mock(side_effect=[httpx.ConnectError("boom"), httpx.Response(200, json={})])
    async with httpx.AsyncClient() as c:
        assert (await rt.request_with_retry(c, "GET", "https://e.test/x")).status_code == 200


@respx.mock
async def test_error_after_retries():
    route = respx.get("https://e.test/x").mock(
        return_value=httpx.Response(503, json={"error": {"message": "down", "status": "UNAVAILABLE"}})
    )
    async with httpx.AsyncClient() as c:
        with pytest.raises(rt.GoogleApiError) as ei:
            await rt.request_with_retry(c, "GET", "https://e.test/x")
    assert ei.value.status == 503 and ei.value.reason == "UNAVAILABLE"
    assert route.call_count == 6


@respx.mock
async def test_no_retry_on_400():
    route = respx.get("https://e.test/x").mock(
        return_value=httpx.Response(400, json={"error": {"message": "bad", "status": "INVALID_ARGUMENT"}})
    )
    async with httpx.AsyncClient() as c:
        with pytest.raises(rt.GoogleApiError):
            await rt.request_with_retry(c, "GET", "https://e.test/x")
    assert route.call_count == 1
