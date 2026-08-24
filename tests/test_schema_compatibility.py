from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft7Validator
from mcp.types import Tool

from strava_mcp.config import Settings
from strava_mcp.openapi import (
    SpecStore,
    build_operations,
    find_invalid_required_keywords,
    normalize_json_schema,
)

FIXTURE = Path(__file__).parent / "fixtures" / "swagger.json"


def test_all_generated_input_schemas_have_valid_required_keywords() -> None:
    operations = build_operations(SpecStore(Settings.from_env()).load())

    for operation in operations:
        assert find_invalid_required_keywords(operation.input_schema) == [], operation.tool_name
        schema = normalize_json_schema(operation.input_schema)
        Draft7Validator.check_schema(schema)
        tool = Tool(
            name=operation.tool_name,
            description=operation.description,
            inputSchema=schema,
        )
        assert tool.inputSchema == schema


def test_parameter_metadata_is_not_emitted_inside_property_schema() -> None:
    operations = build_operations(SpecStore(Settings.from_env()).load())
    operation = next(item for item in operations if item.tool_name == "get_stats")

    assert operation.input_schema["required"] == ["id"]
    assert operation.input_schema["properties"]["id"] == {
        "type": "integer",
        "format": "int64",
        "description": "The identifier of the athlete. Must match the authenticated athlete.",
    }


@pytest.mark.parametrize("invalid_required", [True, False, ["id", 1], "id"])
def test_invalid_required_keyword_is_detected_recursively(invalid_required: object) -> None:
    schema = {"type": "object", "properties": {"id": {"type": "integer"}}}
    schema["properties"]["id"]["required"] = invalid_required

    assert find_invalid_required_keywords(schema) == ["properties.id.required"]
    with pytest.raises(ValueError, match="invalid JSON Schema `required`"):
        normalize_json_schema(schema)


def test_business_property_named_required_is_preserved() -> None:
    schema = {
        "type": "object",
        "properties": {
            "required": {"type": "boolean"},
            "id": {"type": "integer"},
        },
        "required": ["id"],
    }

    normalized = normalize_json_schema(schema)

    assert normalized == schema
    Tool(name="example", inputSchema=normalized)


def test_nested_business_property_named_required_is_preserved() -> None:
    schema = {
        "type": "object",
        "properties": {
            "activity": {
                "type": "object",
                "properties": {"required": {"type": "boolean"}},
            }
        },
    }

    normalized = normalize_json_schema(schema)

    assert normalized == schema
    Tool(name="nested-example", inputSchema=normalized)


def test_debug_schema_is_json_serializable() -> None:
    settings = Settings(config_dir=FIXTURE.parent, openapi_path=FIXTURE)
    operations = build_operations(SpecStore(settings).load())

    for operation in operations:
        json.dumps(operation.input_schema)
