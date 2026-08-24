from __future__ import annotations

import json
from pathlib import Path

from strava_mcp.config import Settings
from strava_mcp.openapi import SpecStore, build_operations, infer_scopes, validate_spec

FIXTURE = Path(__file__).parent / "fixtures" / "swagger.json"


def settings(tmp_path: Path) -> Settings:
    return Settings(config_dir=tmp_path, openapi_path=FIXTURE)


def test_fixture_is_valid_swagger_and_operations_are_generated(tmp_path: Path) -> None:
    document = json.loads(FIXTURE.read_text())
    validate_spec(document)
    operations = build_operations(SpecStore(settings(tmp_path)).load())

    assert len(operations) == 6
    assert {operation.tool_name for operation in operations} == {
        "get_item",
        "put_update_item",
        "delete_item",
        "post_create_item",
        "get_search_items",
        "get_missing_param",
    }


def test_path_parameter_is_inferred_when_official_document_omits_it(tmp_path: Path) -> None:
    operation = next(
        operation
        for operation in build_operations(SpecStore(settings(tmp_path)).load())
        if operation.path == "/missing/{item_id}"
    )
    assert operation.input_schema["properties"]["item_id"] == {
        "type": "string",
        "description": "Path parameter inferred from the URL template.",
    }
    assert operation.input_schema["required"] == ["item_id"]


def test_body_properties_and_nested_objects_are_flattened(tmp_path: Path) -> None:
    operation = next(
        operation
        for operation in build_operations(SpecStore(settings(tmp_path)).load())
        if operation.method == "put"
    )
    properties = operation.input_schema["properties"]
    assert {"id", "name", "description", "status", "metadata"} <= set(properties)
    assert properties["status"]["enum"] == ["open", "closed"]
    assert properties["metadata"]["properties"]["source"]["type"] == "string"
    assert operation.parameters[1].location == "body"


def test_real_strava_activity_update_contains_editable_fields() -> None:
    operations = build_operations(SpecStore(Settings.from_env()).load())
    operation = next(
        operation
        for operation in operations
        if operation.path == "/activities/{id}" and operation.method == "put"
    )
    assert operation.tool_name == "put_update_activity_by_id"
    assert {"id", "name", "description", "gear_id", "sport_type"} <= set(
        operation.input_schema["properties"]
    )
    assert operation.input_schema["properties"]["name"]["type"] == "string"


def test_malformed_multipart_path_parameter_is_sent_as_form_data() -> None:
    operations = build_operations(SpecStore(Settings.from_env()).load())
    operation = next(
        operation
        for operation in operations
        if operation.path == "/athlete" and operation.method == "put"
    )
    assert operation.input_schema["properties"]["weight"]["type"] == "number"
    assert (
        next(parameter for parameter in operation.parameters if parameter.name == "weight").location
        == "formData"
    )


def test_file_form_data_is_exposed_as_a_valid_mcp_string_schema() -> None:
    operations = build_operations(SpecStore(Settings.from_env()).load())
    operation = next(operation for operation in operations if operation.path == "/uploads")
    file_schema = operation.input_schema["properties"]["file"]
    assert file_schema["type"] == "string"
    assert file_schema["x-strava-mcp-file"] is True


def test_scope_inference_preserves_alternatives() -> None:
    requirements = infer_scopes(
        "Requires activity:read for Everyone. Requires activity:read_all for Only Me."
    )
    assert requirements.required == ()
    assert requirements.alternatives == (("activity:read", "activity:read_all"),)
