"""Environment-driven configuration and safe local paths."""

from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


def _env(name: str, *aliases: str, default: str | None = None) -> str | None:
    for key in (name, *aliases):
        value = os.getenv(key)
        if value is not None:
            return value
    return default


def _env_bool(name: str, *aliases: str, default: bool) -> bool:
    value = _env(name, *aliases)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean (true/false), got {value!r}")


def default_config_dir() -> Path:
    configured = os.getenv("XDG_CONFIG_HOME")
    return (
        Path(configured).expanduser() / "strava-mcp"
        if configured
        else Path.home() / ".config" / "strava-mcp"
    )


class Settings(BaseModel):
    """Runtime settings. Secrets are intentionally excluded from string output."""

    model_config = ConfigDict(extra="forbid")

    client_id: str | None = Field(default=None, repr=False)
    client_secret: str | None = Field(default=None, repr=False)
    api_base_url: str = "https://www.strava.com/api/v3"
    openapi_url: str = "https://developers.strava.com/swagger/swagger.json"
    openapi_path: Path
    config_dir: Path
    allow_write: bool = True
    allow_delete: bool = False
    log_level: str = "INFO"
    oauth_scopes: tuple[str, ...] = (
        "read",
        "read_all",
        "profile:read_all",
        "profile:write",
        "activity:read",
        "activity:read_all",
        "activity:write",
    )
    oauth_authorize_url: str = "https://www.strava.com/oauth/authorize"
    oauth_token_url: str = "https://www.strava.com/api/v3/oauth/token"
    callback_host: str = "127.0.0.1"
    callback_port: int = 8765
    request_timeout: float = 30.0

    @classmethod
    def from_env(cls) -> Settings:
        config_dir = default_config_dir()
        local_client_id: str | None = None
        local_client_secret: str | None = None
        credentials_path = config_dir / "credentials.json"
        if credentials_path.is_file():
            try:
                credentials = json.loads(credentials_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(f"Could not read {credentials_path}: {exc}") from exc
            if not isinstance(credentials, dict):
                raise ValueError(f"{credentials_path} must contain a JSON object")
            local_client_id = credentials.get("client_id")
            local_client_secret = credentials.get("client_secret")
            try:
                credentials_path.chmod(0o600)
            except OSError:
                pass
        configured_path = _env("STRAVA_OPENAPI_PATH")
        scopes_text = _env(
            "STRAVA_OAUTH_SCOPES",
            default="read,read_all,profile:read_all,profile:write,activity:read,activity:read_all,activity:write",
        )
        scopes = tuple(
            scope.strip()
            for scope in (scopes_text or "").replace(" ", ",").split(",")
            if scope.strip()
        )
        return cls(
            client_id=_env("STRAVA_CLIENT_ID", default=local_client_id),
            client_secret=_env("STRAVA_CLIENT_SECRET", default=local_client_secret),
            api_base_url=(
                _env("STRAVA_API_BASE_URL", default=cls.model_fields["api_base_url"].default) or ""
            ).rstrip("/"),
            openapi_url=_env("STRAVA_OPENAPI_URL", default=cls.model_fields["openapi_url"].default)
            or "",
            openapi_path=Path(configured_path).expanduser()
            if configured_path
            else config_dir / "openapi.json",
            config_dir=config_dir,
            allow_write=_env_bool("STRAVA_ALLOW_WRITE", "STRAVA_MCP_ALLOW_WRITE", default=True),
            allow_delete=_env_bool("STRAVA_ALLOW_DELETE", "STRAVA_MCP_ALLOW_DELETE", default=False),
            log_level=_env("STRAVA_LOG_LEVEL", default="INFO") or "INFO",
            oauth_scopes=scopes,
            oauth_authorize_url=_env(
                "STRAVA_OAUTH_AUTHORIZE_URL",
                default=cls.model_fields["oauth_authorize_url"].default,
            )
            or "",
            oauth_token_url=_env(
                "STRAVA_OAUTH_TOKEN_URL", default=cls.model_fields["oauth_token_url"].default
            )
            or "",
            callback_host=_env("STRAVA_CALLBACK_HOST", default="127.0.0.1") or "127.0.0.1",
            callback_port=int(_env("STRAVA_CALLBACK_PORT", default="8765") or "8765"),
            request_timeout=float(_env("STRAVA_REQUEST_TIMEOUT", default="30") or "30"),
        )

    @property
    def tokens_path(self) -> Path:
        return self.config_dir / "tokens.json"

    @property
    def credentials_path(self) -> Path:
        return self.config_dir / "credentials.json"

    def safe_dict(self) -> dict[str, object]:
        """Return a config representation suitable for CLI output and diagnostics."""
        return {
            "api_base_url": self.api_base_url,
            "openapi_url": self.openapi_url,
            "openapi_path": str(self.openapi_path),
            "config_dir": str(self.config_dir),
            "client_id_configured": bool(self.client_id),
            "client_secret_configured": bool(self.client_secret),
            "allow_write": self.allow_write,
            "allow_delete": self.allow_delete,
            "log_level": self.log_level,
            "oauth_scopes": list(self.oauth_scopes),
            "callback": f"http://{self.callback_host}:{self.callback_port}/callback",
        }
