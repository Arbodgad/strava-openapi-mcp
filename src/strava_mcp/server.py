"""MCP stdio server with dynamically generated tools."""

from __future__ import annotations

import logging
import sys
from typing import Any

from mcp.server import Server
from mcp.server.lowlevel.server import NotificationOptions
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent

from . import __version__
from .auth import OAuthError, OAuthManager
from .client import StravaClient, StravaHTTPError
from .config import Settings
from .openapi import SpecStore, build_operations
from .tools import DynamicToolRegistry, ToolExecutionError

LOGGER = logging.getLogger(__name__)


def configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        stream=sys.stderr, level=getattr(logging, settings.log_level.upper(), logging.INFO)
    )


async def run_stdio(settings: Settings | None = None) -> None:
    settings = settings or Settings.from_env()
    configure_logging(settings)
    bundle = SpecStore(settings).load()
    operations = build_operations(bundle)
    oauth = OAuthManager(settings)
    client = StravaClient(settings, oauth)
    registry = DynamicToolRegistry(operations, client, settings)
    server = Server("strava-openapi-mcp")

    @server.list_tools()
    async def list_tools() -> list[Any]:
        return registry.mcp_tools()

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any] | None) -> CallToolResult:
        try:
            result = await registry.call(name, arguments)
            return CallToolResult(
                content=[TextContent(type="text", text=registry.render_result(result))],
                structuredContent=result if isinstance(result, dict) else None,
                isError=False,
            )
        except (OAuthError, StravaHTTPError, ToolExecutionError, ValueError) as exc:
            LOGGER.info("Tool %s failed: %s", name, exc)
            return CallToolResult(content=[TextContent(type="text", text=str(exc))], isError=True)
        except Exception:
            LOGGER.exception("Unexpected failure while calling tool %s", name)
            return CallToolResult(
                content=[TextContent(type="text", text="Unexpected internal server error")],
                isError=True,
            )

    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name="strava-openapi-mcp",
                    server_version=__version__,
                    capabilities=server.get_capabilities(
                        notification_options=NotificationOptions(), experimental_capabilities={}
                    ),
                ),
            )
    finally:
        await client.close()
        await oauth.close()
