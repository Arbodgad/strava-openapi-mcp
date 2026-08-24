"""Local Strava OAuth 2.0 flow and rotating token persistence."""

from __future__ import annotations

import asyncio
import logging
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from pydantic import BaseModel, Field

from .config import Settings

LOGGER = logging.getLogger(__name__)


class OAuthError(RuntimeError):
    """A safe, user-facing OAuth error without secrets."""


class TokenState(BaseModel):
    access_token: str = Field(repr=False)
    refresh_token: str = Field(repr=False)
    expires_at: int
    scopes: tuple[str, ...] = ()


def parse_scopes(value: str | list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        values = value.replace(",", " ").split()
    else:
        values = value
    return tuple(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))


class TokenStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> TokenState | None:
        if not self.path.is_file():
            return None
        try:
            return TokenState.model_validate_json(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise OAuthError(f"Could not read token store {self.path}: {exc}") from exc

    def save(self, tokens: TokenState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(tokens.model_dump_json(indent=2) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self.path)
        self.path.chmod(0o600)


class OAuthManager:
    def __init__(self, settings: Settings, http_client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.store = TokenStore(settings.tokens_path)
        self._client = http_client
        self._owns_client = http_client is None
        self._lock = asyncio.Lock()

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.settings.request_timeout)
        return self._client

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def authorization_url(
        self, redirect_uri: str, state: str | None = None, scopes: tuple[str, ...] | None = None
    ) -> str:
        query = {
            "client_id": self.settings.client_id or "",
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "approval_prompt": "auto",
            "scope": ",".join(scopes or self.settings.oauth_scopes),
            "state": state or secrets.token_urlsafe(24),
        }
        return f"{self.settings.oauth_authorize_url}?{urlencode(query)}"

    async def exchange_code(self, code: str, granted_scopes: str | None = None) -> TokenState:
        return await self._token_request(
            {"grant_type": "authorization_code", "code": code}, granted_scopes
        )

    async def refresh(self, current: TokenState) -> TokenState:
        return await self._token_request(
            {"grant_type": "refresh_token", "refresh_token": current.refresh_token},
            " ".join(current.scopes),
        )

    async def _token_request(self, data: dict[str, str], granted_scopes: str | None) -> TokenState:
        if not self.settings.client_id or not self.settings.client_secret:
            raise OAuthError("STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET must be configured")
        payload = {
            "client_id": self.settings.client_id,
            "client_secret": self.settings.client_secret,
            **data,
        }
        client = await self._get_client()
        try:
            response = await client.post(self.settings.oauth_token_url, data=payload)
        except httpx.HTTPError as exc:
            raise OAuthError(
                f"OAuth token request failed: {_redact(str(exc), list(payload.values()))}"
            ) from exc
        if response.is_error:
            message = _redact(_response_message(response), list(payload.values()))
            raise OAuthError(
                f"OAuth token request failed with HTTP {response.status_code}: {message}"
            )
        try:
            body = response.json()
            tokens = TokenState(
                access_token=body["access_token"],
                refresh_token=body["refresh_token"],
                expires_at=int(body["expires_at"]),
                scopes=parse_scopes(body.get("scope") or granted_scopes),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise OAuthError("OAuth token response did not contain the expected fields") from exc
        self.store.save(tokens)
        return tokens

    async def get_valid_access_token(self, required: Any = None) -> str:
        async with self._lock:
            tokens = self.store.load()
            if tokens is None:
                raise OAuthError("No Strava authorization found. Run `strava-mcp auth` first.")
            if tokens.expires_at <= int(time.time()) + 60:
                tokens = await self.refresh(tokens)
            if required is not None:
                missing = required.check(set(tokens.scopes))
                if missing:
                    raise OAuthError(
                        "OAuth scope missing: "
                        + ", ".join(missing)
                        + ". Run `strava-mcp auth` again to authorize the required scope(s)."
                    )
            return tokens.access_token


def _response_message(response: httpx.Response) -> str:
    try:
        body = response.json()
        if isinstance(body, dict):
            return str(body.get("message") or body.get("error") or body)
        return str(body)
    except ValueError:
        return response.text[:500] or response.reason_phrase


def _redact(message: str, secrets_to_hide: list[str]) -> str:
    for secret in secrets_to_hide:
        if secret:
            message = message.replace(secret, "[redacted]")
    return message


class _CallbackHandler(BaseHTTPRequestHandler):
    result: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/callback":
            self.send_error(404)
            return
        query = parse_qs(parsed.query)
        self.result = {key: values[0] for key, values in query.items() if values}
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Strava authorization received. You can close this window.\n")

    def log_message(self, format: str, *args: object) -> None:
        LOGGER.debug("OAuth callback: %s", format % args)


def run_local_oauth(settings: Settings) -> TokenState:
    if not settings.client_id or not settings.client_secret:
        raise OAuthError(
            "Set STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET before running `strava-mcp auth`."
        )
    redirect_uri = f"http://{settings.callback_host}:{settings.callback_port}/callback"
    state = secrets.token_urlsafe(24)
    manager = OAuthManager(settings)
    url = manager.authorization_url(redirect_uri, state=state)
    callback = _CallbackHandler
    callback.result = {}
    server = HTTPServer((settings.callback_host, settings.callback_port), callback)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    print(f"Opening Strava authorization in your browser: {url}")
    if not webbrowser.open(url):
        print("Open the URL above manually if the browser did not open.")
    thread.join(timeout=300)
    server.server_close()
    if thread.is_alive():
        raise OAuthError("Timed out waiting for the Strava OAuth callback")
    result = callback.result
    if result.get("state") != state:
        raise OAuthError("OAuth state verification failed")
    if result.get("error"):
        raise OAuthError(f"Strava authorization failed: {result['error']}")
    if not result.get("code"):
        raise OAuthError("Strava callback did not contain an authorization code")
    try:
        return asyncio.run(manager.exchange_code(result["code"], result.get("scope")))
    finally:
        asyncio.run(manager.close())
