"""Generic asynchronous Strava HTTP client."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from .config import Settings
from .openapi import Operation

LOGGER = logging.getLogger(__name__)


class AccessTokenProvider(Protocol):
    async def get_valid_access_token(self, required: Any = None) -> str: ...


class StravaHTTPError(RuntimeError):
    def __init__(
        self,
        operation: Operation,
        response: httpx.Response,
        secrets_to_hide: tuple[str, ...] = (),
    ):
        self.status_code = response.status_code
        self.operation = operation
        self.rate_headers = {
            key: value
            for key, value in response.headers.items()
            if "rate" in key.lower() or key.lower() == "retry-after"
        }
        self.message = _response_message(response)
        for secret in secrets_to_hide:
            if secret:
                self.message = self.message.replace(secret, "[redacted]")
        super().__init__(self.safe_message())

    def safe_message(self) -> str:
        text = (
            f"HTTP {self.status_code} {httpx.codes.get_reason_phrase(self.status_code)}\n"
            f"Endpoint: {self.operation.endpoint_label}\nMessage: {self.message}"
        )
        if self.rate_headers:
            text += "\nRate-limit headers: " + ", ".join(
                f"{key}={value}" for key, value in self.rate_headers.items()
            )
        if self.status_code == 429:
            text += "\nStrava rate limit reached; no aggressive retry was attempted."
        return text


class RequestArgumentError(ValueError):
    pass


def _response_message(response: httpx.Response) -> str:
    try:
        body = response.json()
        if isinstance(body, dict):
            if body.get("message"):
                return str(body["message"])
            if body.get("errors"):
                return json.dumps(body["errors"], ensure_ascii=False)
            return json.dumps(body, ensure_ascii=False)
        return str(body)
    except ValueError:
        return response.text[:1000] or response.reason_phrase


def _query_value(value: Any, collection_format: str | None) -> Any:
    if isinstance(value, (list, tuple)):
        if collection_format == "csv":
            return ",".join(str(item) for item in value)
        return [str(item) for item in value]
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


class StravaClient:
    def __init__(
        self,
        settings: Settings,
        token_provider: AccessTokenProvider,
        http_client: httpx.AsyncClient | None = None,
    ):
        self.settings = settings
        self.token_provider = token_provider
        self._http = http_client
        self._owns_http = http_client is None

    async def _get_http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self.settings.request_timeout)
        return self._http

    async def close(self) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    async def request(self, operation: Operation, arguments: dict[str, Any]) -> Any:
        token = await self.token_provider.get_valid_access_token(operation.scopes)
        path_values: dict[str, Any] = {}
        query: dict[str, Any] = {}
        headers: dict[str, str] = {"Authorization": f"Bearer {token}"}
        form: dict[str, Any] = {}
        files: dict[str, tuple[str, Any]] = {}
        body: dict[str, Any] = {}
        for parameter in operation.parameters:
            present = parameter.name in arguments and arguments[parameter.name] is not None
            if parameter.required and not present:
                raise RequestArgumentError(f"Missing required parameter: {parameter.name}")
            if not present:
                continue
            value = arguments[parameter.name]
            if parameter.location == "path":
                path_values[parameter.original_name] = value
            elif parameter.location == "query":
                query[parameter.original_name] = _query_value(value, parameter.collection_format)
            elif parameter.location == "formData":
                if parameter.json_schema.get("x-strava-mcp-file"):
                    file_path = Path(str(value)).expanduser()
                    if not file_path.is_file():
                        raise RequestArgumentError(
                            f"File parameter {parameter.name} does not exist: {file_path}"
                        )
                    files[parameter.original_name] = (file_path.name, file_path.open("rb"))
                else:
                    form[parameter.original_name] = _query_value(value, parameter.collection_format)
            elif parameter.location == "body":
                body[parameter.original_name] = value
            elif parameter.location == "header":
                headers[parameter.original_name] = str(value)
        try:
            path = operation.path
            for name, value in path_values.items():
                path = path.replace("{" + name + "}", quote(str(value), safe=""))
            if "{" in path or "}" in path:
                raise RequestArgumentError(f"Missing path parameter for {operation.path}")
            url = self.settings.api_base_url.rstrip("/") + "/" + path.lstrip("/")
            request_kwargs: dict[str, Any] = {"params": query, "headers": headers}
            if operation.body_mode == "json":
                if body:
                    request_kwargs["json"] = body
            elif operation.body_mode == "form":
                request_kwargs["data"] = form
                if files:
                    request_kwargs["files"] = files
            http = await self._get_http()
            LOGGER.debug("Calling Strava %s %s", operation.method.upper(), operation.path)
            response = await http.request(operation.method.upper(), url, **request_kwargs)
            if response.is_error:
                raise StravaHTTPError(operation, response, secrets_to_hide=(token,))
            if response.status_code == 204 or not response.content:
                return {"status": "success", "http_status": response.status_code}
            content_type = response.headers.get("content-type", "")
            if "json" in content_type.lower():
                return response.json()
            return {
                "http_status": response.status_code,
                "content_type": content_type,
                "body": response.text,
            }
        finally:
            for _, file_tuple in files.items():
                file_tuple[1].close()
