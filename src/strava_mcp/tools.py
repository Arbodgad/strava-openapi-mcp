"""Dynamic MCP tool registry backed entirely by generated OpenAPI operations."""

from __future__ import annotations

import json
from typing import Any

from jsonschema import Draft7Validator

from .client import RequestArgumentError, StravaClient
from .config import Settings
from .openapi import Operation


class ToolExecutionError(RuntimeError):
    pass


class DynamicToolRegistry:
    def __init__(self, operations: list[Operation], client: StravaClient, settings: Settings):
        self.operations = operations
        self.client = client
        self.settings = settings
        self._by_name = {operation.tool_name: operation for operation in operations}

    def get(self, name: str) -> Operation:
        try:
            return self._by_name[name]
        except KeyError as exc:
            raise ToolExecutionError(f"Unknown Strava tool: {name}") from exc

    def mcp_tools(self) -> list[Any]:
        from mcp.types import Tool

        return [
            Tool(
                name=operation.tool_name,
                description=operation.description,
                inputSchema=operation.input_schema,
            )
            for operation in self.operations
            if self._enabled(operation)
        ]

    def _enabled(self, operation: Operation) -> bool:
        if operation.category == "destructive":
            return self.settings.allow_delete
        return True

    async def call(self, name: str, arguments: dict[str, Any] | None) -> Any:
        operation = self.get(name)
        if not self._enabled(operation):
            raise ToolExecutionError("DELETE tools are disabled by STRAVA_ALLOW_DELETE=false")
        if operation.category == "write" and not self.settings.allow_write:
            raise ToolExecutionError("Write tools are disabled by STRAVA_ALLOW_WRITE=false")
        values = arguments or {}
        errors = sorted(
            Draft7Validator(operation.input_schema).iter_errors(values),
            key=lambda error: list(error.path),
        )
        if errors:
            details = "; ".join(error.message for error in errors[:5])
            raise ToolExecutionError(f"Invalid arguments for {name}: {details}")
        try:
            return await self.client.request(operation, values)
        except (RequestArgumentError, ToolExecutionError) as exc:
            raise ToolExecutionError(str(exc)) from exc

    @staticmethod
    def render_result(value: Any) -> str:
        return json.dumps(value, indent=2, ensure_ascii=False, default=str)
