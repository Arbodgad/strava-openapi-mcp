from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


@pytest.mark.asyncio
async def test_stdio_client_can_initialize_and_discover_generated_tools(tmp_path: Path) -> None:
    project_root = Path(__file__).parents[1]
    server_env = {
        **os.environ,
        "PYTHONPATH": str(project_root / "src"),
        "STRAVA_OPENAPI_PATH": str(tmp_path / "not-present.json"),
    }
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "strava_mcp.cli"],
        cwd=project_root,
        env=server_env,
    )
    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            initialization = await session.initialize()
            tools = await session.list_tools()

    names = {tool.name for tool in tools.tools}
    assert initialization.serverInfo.name == "strava-openapi-mcp"
    assert "get_activity_by_id" in names
    assert "put_update_activity_by_id" in names
    assert len(tools.tools) == 34
    # If diagnostics had been written to stdout, MCP framing would fail before
    # this point. The captured stderr is intentionally allowed to contain
    # controlled server diagnostics.
