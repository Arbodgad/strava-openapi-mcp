from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from strava_mcp.client import StravaClient, StravaHTTPError
from strava_mcp.config import Settings
from strava_mcp.openapi import SpecStore, build_operations
from strava_mcp.tools import DynamicToolRegistry, ToolExecutionError

FIXTURE = Path(__file__).parent / "fixtures" / "swagger.json"


class FakeTokenProvider:
    def __init__(self) -> None:
        self.seen_requirements = []

    async def get_valid_access_token(self, required=None) -> str:
        self.seen_requirements.append(required)
        return "test-access-token"


def make_settings(tmp_path: Path, **kwargs) -> Settings:
    return Settings(config_dir=tmp_path, openapi_path=FIXTURE, **kwargs)


def operation(tmp_path: Path, method: str, path: str):
    return next(
        item
        for item in build_operations(SpecStore(make_settings(tmp_path)).load())
        if item.method == method and item.path == path
    )


@pytest.mark.asyncio
async def test_get_builds_path_query_and_bearer_header(tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers["authorization"]
        return httpx.Response(200, json={"id": 12, "name": "A"}, request=request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = StravaClient(
            make_settings(tmp_path, api_base_url="https://api.test/v3"), FakeTokenProvider(), http
        )
        result = await client.request(
            operation(tmp_path, "get", "/items/{id}"), {"id": 12, "verbose": True}
        )

    assert result == {"id": 12, "name": "A"}
    assert seen["url"] == "https://api.test/v3/items/12?verbose=true"
    assert seen["authorization"] == "Bearer test-access-token"


@pytest.mark.asyncio
async def test_put_sends_only_supplied_body_fields(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert str(request.url) == "https://api.test/v3/items/12"
        assert request.headers["content-type"] == "application/json"
        assert json.loads(request.content) == {"name": "New name", "description": "Details"}
        return httpx.Response(200, json={"id": 12, "name": "New name"}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = StravaClient(
            make_settings(tmp_path, api_base_url="https://api.test/v3"), FakeTokenProvider(), http
        )
        result = await client.request(
            operation(tmp_path, "put", "/items/{id}"),
            {"id": 12, "name": "New name", "description": "Details"},
        )
    assert result["name"] == "New name"


@pytest.mark.asyncio
async def test_post_sends_form_data(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.headers["content-type"] == "application/x-www-form-urlencoded"
        assert request.content == b"name=Created&labels=one%2Ctwo"
        return httpx.Response(201, json={"id": 1}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = StravaClient(
            make_settings(tmp_path, api_base_url="https://api.test/v3"), FakeTokenProvider(), http
        )
        result = await client.request(
            operation(tmp_path, "post", "/items"), {"name": "Created", "labels": ["one", "two"]}
        )
    assert result == {"id": 1}


@pytest.mark.asyncio
async def test_delete_and_empty_204_response(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = StravaClient(
            make_settings(tmp_path, api_base_url="https://api.test/v3"), FakeTokenProvider(), http
        )
        result = await client.request(operation(tmp_path, "delete", "/items/{id}"), {"id": 99})
    assert result == {"status": "success", "http_status": 204}


@pytest.mark.asyncio
async def test_http_error_and_rate_limit_are_llm_friendly(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"message": "Too many requests"},
            headers={"X-ReadRateLimit": "100,200", "X-ReadRate": "100,100"},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = StravaClient(
            make_settings(tmp_path, api_base_url="https://api.test/v3"), FakeTokenProvider(), http
        )
        with pytest.raises(StravaHTTPError) as error:
            await client.request(operation(tmp_path, "get", "/items/{id}"), {"id": 1})
    message = str(error.value)
    assert "HTTP 429" in message
    assert "GET /items/{id}" in message
    assert "Too many requests" in message
    assert "x-readratelimit" in message
    assert "no aggressive retry" in message


@pytest.mark.asyncio
async def test_registry_protects_writes_and_delete_tools(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, allow_write=False, allow_delete=False)
    operations = build_operations(SpecStore(settings).load())
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, request=request))
    ) as http:
        registry = DynamicToolRegistry(
            operations, StravaClient(settings, FakeTokenProvider(), http), settings
        )
        names = {tool.name for tool in registry.mcp_tools()}
        assert "delete_item" not in names
        with pytest.raises(ToolExecutionError, match="Write tools are disabled"):
            await registry.call("post_create_item", {"name": "blocked"})
        with pytest.raises(ToolExecutionError, match="DELETE tools are disabled"):
            await registry.call("delete_item", {"id": 1})
