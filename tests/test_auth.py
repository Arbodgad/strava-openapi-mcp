from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest

from strava_mcp.auth import OAuthError, OAuthManager, TokenState, TokenStore
from strava_mcp.config import Settings
from strava_mcp.openapi import ScopeRequirements


@pytest.mark.asyncio
async def test_oauth_exchange_and_refresh_persist_rotating_tokens(tmp_path: Path) -> None:
    responses = iter(
        [
            {
                "access_token": "access-1",
                "refresh_token": "refresh-1",
                "expires_at": int(time.time()) + 3600,
                "scope": "activity:read",
            },
            {
                "access_token": "access-2",
                "refresh_token": "refresh-2",
                "expires_at": int(time.time()) + 3600,
                "scope": "activity:read activity:write",
            },
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = next(responses)
        assert request.url == "https://strava.test/token"
        assert "client_secret=secret" in request.content.decode()
        return httpx.Response(200, json=body, request=request)

    settings = Settings(
        config_dir=tmp_path,
        openapi_path=tmp_path / "openapi.json",
        client_id="123",
        client_secret="secret",
        oauth_token_url="https://strava.test/token",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        manager = OAuthManager(settings, http)
        exchanged = await manager.exchange_code("one-time-code", "activity:read")
        assert exchanged.access_token == "access-1"
        assert TokenStore(settings.tokens_path).load() == exchanged
        expired = exchanged.model_copy(update={"expires_at": int(time.time()) - 1})
        TokenStore(settings.tokens_path).save(expired)
        token = await manager.get_valid_access_token()

    assert token == "access-2"
    saved = TokenStore(settings.tokens_path).load()
    assert saved is not None
    assert saved.refresh_token == "refresh-2"
    assert saved.scopes == ("activity:read", "activity:write")
    assert oct(settings.tokens_path.stat().st_mode & 0o777) == "0o600"


@pytest.mark.asyncio
async def test_oauth_missing_scope_is_explicit(tmp_path: Path) -> None:
    settings = Settings(config_dir=tmp_path, openapi_path=tmp_path / "openapi.json")
    TokenStore(settings.tokens_path).save(
        TokenState(
            access_token="a",
            refresh_token="r",
            expires_at=int(time.time()) + 3600,
            scopes=("activity:read",),
        )
    )
    manager = OAuthManager(settings)
    with pytest.raises(OAuthError, match="activity:write"):
        await manager.get_valid_access_token(ScopeRequirements(required=("activity:write",)))


def test_token_store_does_not_expose_secrets_in_json_diagnostics(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    TokenStore(path).save(
        TokenState(access_token="access-secret", refresh_token="refresh-secret", expires_at=1)
    )
    payload = json.loads(path.read_text())
    assert payload["access_token"] == "access-secret"
    assert payload["refresh_token"] == "refresh-secret"
