"""Swagger 2.0 loading, local reference resolution, and operation generation."""

from __future__ import annotations

import copy
import json
import logging
import re
import tempfile
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag, urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .config import Settings

LOGGER = logging.getLogger(__name__)
HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")
OFFICIAL_SCHEMA_BASE = "https://developers.strava.com/swagger/"
PARAMETER_SCHEMA_KEYS = {
    "type",
    "format",
    "title",
    "description",
    "default",
    "enum",
    "items",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minLength",
    "maxLength",
    "pattern",
    "minItems",
    "maxItems",
    "uniqueItems",
    "minProperties",
    "maxProperties",
    "additionalProperties",
}


class SpecError(ValueError):
    """Raised when the Swagger document cannot be loaded or validated."""


class ParameterBinding(BaseModel):
    """A generated MCP argument and its original Swagger location."""

    model_config = ConfigDict(populate_by_name=True)

    name: str
    original_name: str
    location: str
    required: bool = False
    json_schema: dict[str, Any] = Field(default_factory=dict, alias="schema")
    description: str = ""
    collection_format: str | None = None
    body_parameter: str | None = None


class ScopeRequirements(BaseModel):
    """Scope hints extracted from the human-readable official operation description."""

    required: tuple[str, ...] = ()
    alternatives: tuple[tuple[str, ...], ...] = ()
    conditional: tuple[str, ...] = ()

    def check(self, granted: set[str]) -> list[str]:
        missing = [scope for scope in self.required if scope not in granted]
        for group in self.alternatives:
            if not any(scope in granted for scope in group):
                missing.append("one of " + ", ".join(group))
        return missing

    def describe(self) -> str:
        parts: list[str] = []
        if self.required:
            parts.append("required: " + ", ".join(self.required))
        for group in self.alternatives:
            parts.append("one of: " + ", ".join(group))
        if self.conditional:
            parts.append("conditional: " + ", ".join(self.conditional))
        return "; ".join(parts)


class Operation(BaseModel):
    """Normalized operation used by both the MCP registry and HTTP client."""

    method: str
    path: str
    operation_id: str | None = None
    tool_name: str
    summary: str = ""
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    parameters: list[ParameterBinding] = Field(default_factory=list)
    body_mode: str | None = None
    category: str
    scopes: ScopeRequirements = Field(default_factory=ScopeRequirements)
    tags: list[str] = Field(default_factory=list)

    @property
    def endpoint_label(self) -> str:
        return f"{self.method.upper()} {self.path}"


def _json_pointer(document: dict[str, Any], pointer: str) -> Any:
    current: Any = document
    if pointer in {"", "/"}:
        return current
    for part in pointer.lstrip("/").split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        try:
            current = current[int(part)] if isinstance(current, list) else current[part]
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            raise SpecError(f"Unresolved JSON reference #/{pointer}") from exc
    return current


def _snake_case(value: str) -> str:
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    value = re.sub(r"[^A-Za-z0-9]+", "_", value)
    return value.strip("_").lower() or "operation"


def _path_fallback(method: str, path: str) -> str:
    parts = [part for part in path.strip("/").split("/") if part]
    readable = "_".join(
        "param" if part.startswith("{") and part.endswith("}") else part for part in parts
    )
    return f"{method.lower()}_{_snake_case(readable)}"


def _tool_name(method: str, operation_id: str | None, path: str, used: set[str]) -> str:
    if operation_id:
        base = _snake_case(operation_id)
        # Strava operationIds usually already start with a verb. Keep that verb
        # for readability, and add the HTTP method when it differs.
        verb = base.split("_", 1)[0]
        candidate = base if verb == method.lower() else f"{method.lower()}_{base}"
    else:
        candidate = _path_fallback(method, path)
    unique = candidate
    index = 2
    while unique in used:
        unique = f"{candidate}_{index}"
        index += 1
    used.add(unique)
    return unique


def _merge_object_schemas(schemas: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
    for schema in schemas:
        if schema.get("type") and schema["type"] != "object" and not schema.get("properties"):
            result.update(schema)
            continue
        result.update(
            {
                key: value
                for key, value in schema.items()
                if key not in {"properties", "required", "allOf"}
            }
        )
        result.setdefault("properties", {}).update(schema.get("properties", {}))
        result["required"] = list(
            dict.fromkeys([*result.get("required", []), *schema.get("required", [])])
        )
    if not result["required"]:
        result.pop("required", None)
    return result


class SpecBundle:
    """Main Swagger document plus locally available external JSON documents."""

    def __init__(
        self, document: dict[str, Any], external_documents: dict[str, dict[str, Any]] | None = None
    ):
        self.document = document
        self.external_documents = external_documents or {}

    def resolve(self, reference: str, base_document: dict[str, Any] | None = None) -> Any:
        document_ref, fragment = urldefrag(reference)
        if not document_ref:
            return _json_pointer(base_document or self.document, fragment)
        document = self.external_documents.get(document_ref)
        if document is None:
            # Permit an equivalent URL with a trailing slash normalization.
            document = next(
                (
                    doc
                    for url, doc in self.external_documents.items()
                    if url.rstrip("/") == document_ref.rstrip("/")
                ),
                None,
            )
        if document is None:
            raise SpecError(f"External Swagger document is not available locally: {document_ref}")
        return _json_pointer(document, fragment)

    def dereference(
        self,
        value: Any,
        seen: set[str] | None = None,
        base_document: dict[str, Any] | None = None,
    ) -> Any:
        seen = set() if seen is None else seen
        if isinstance(value, dict) and "$ref" in value:
            reference = value["$ref"]
            if reference in seen:
                return {key: val for key, val in value.items() if key != "$ref"}
            document_ref, _ = urldefrag(reference)
            resolved = copy.deepcopy(self.resolve(reference, base_document))
            if document_ref:
                resolved_base = next(
                    (
                        doc
                        for url, doc in self.external_documents.items()
                        if url.rstrip("/") == document_ref.rstrip("/")
                    ),
                    None,
                )
            else:
                resolved_base = base_document or self.document
            return self.dereference(resolved, {*seen, reference}, resolved_base)
        if isinstance(value, dict):
            return {key: self.dereference(val, seen, base_document) for key, val in value.items()}
        if isinstance(value, list):
            return [self.dereference(item, seen, base_document) for item in value]
        return value

    def schema(self, schema: dict[str, Any] | None) -> dict[str, Any]:
        if not schema:
            return {"type": "object", "additionalProperties": True}
        try:
            resolved = self.dereference(schema)
        except SpecError as exc:
            if "External Swagger document is not available locally" not in str(exc):
                raise
            return {
                "type": "object",
                "additionalProperties": True,
                "description": (
                    "Schema is referenced by the OpenAPI document but is not cached locally."
                ),
            }
        if "allOf" in resolved:
            parts = [self.schema(part) for part in resolved["allOf"]]
            remaining = {key: value for key, value in resolved.items() if key != "allOf"}
            return _merge_object_schemas([*parts, self.schema(remaining)])
        if resolved.get("type") == "object" or "properties" in resolved:
            result = {
                key: value
                for key, value in resolved.items()
                if key not in {"properties", "required"}
            }
            result["type"] = "object"
            result["properties"] = {
                name: self.schema(child) for name, child in resolved.get("properties", {}).items()
            }
            if resolved.get("required"):
                result["required"] = list(resolved["required"])
            return result
        if resolved.get("type") == "array" and "items" in resolved:
            resolved["items"] = self.schema(resolved["items"])
        return resolved


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SpecError(f"Could not read Swagger JSON at {path}: {exc}") from exc


def validate_spec(
    document: dict[str, Any], external_documents: dict[str, dict[str, Any]] | None = None
) -> None:
    if not isinstance(document, dict) or document.get("swagger") != "2.0":
        raise SpecError("The Strava document must be a Swagger 2.0 document")
    if not isinstance(document.get("paths"), dict) or not document["paths"]:
        raise SpecError("The Swagger document has no paths")
    try:
        from openapi_spec_validator import validate as validator

        # Strava's published document has a few non-standard siblings next to
        # local $refs (for example `in: query` next to #/parameters/perPage).
        # Validate a standards-normalized copy but retain the official input
        # unchanged for runtime resolution and local caching.
        def normalize_refs(value: Any) -> Any:
            if isinstance(value, dict):
                if "$ref" in value:
                    return {"$ref": value["$ref"]}
                normalized = {key: normalize_refs(child) for key, child in value.items()}
                # Two export operations in the official Swagger 2.0 file use
                # the OpenAPI 3 response `content` shape. Map its first media
                # type to the Swagger 2.0 response `schema` for validation.
                if (
                    "content" in normalized
                    and "schema" not in normalized
                    and isinstance(normalized["content"], dict)
                ):
                    media = next(iter(normalized["content"].values()), None)
                    if isinstance(media, dict) and "schema" in media:
                        normalized["schema"] = media["schema"]
                    normalized.pop("content", None)
                return normalized
            if isinstance(value, list):
                return [normalize_refs(child) for child in value]
            return value

        normalized = normalize_refs(document)
        if isinstance(normalized, dict):
            # The official PUT /athlete entry declares `weight` as a path
            # parameter although `/athlete` has no `{weight}` placeholder;
            # its multipart declaration shows that this is a form field.
            for path, path_item in normalized.get("paths", {}).items():
                if not isinstance(path_item, dict):
                    continue
                for method, operation in path_item.items():
                    if method not in HTTP_METHODS or not isinstance(operation, dict):
                        continue
                    declared_path_names = {
                        parameter.get("name")
                        for parameter in operation.get("parameters", [])
                        if isinstance(parameter, dict) and parameter.get("in") == "path"
                    }
                    for parameter in operation.get("parameters", []):
                        if (
                            isinstance(parameter, dict)
                            and parameter.get("in") == "path"
                            and f"{{{parameter.get('name')}}}" not in path
                        ):
                            parameter["in"] = "formData"
                    for name in re.findall(r"{([^}]+)}", path):
                        if name not in declared_path_names:
                            operation.setdefault("parameters", []).append(
                                {
                                    "name": name,
                                    "in": "path",
                                    "required": True,
                                    "type": "string",
                                    "description": "Path parameter inferred from the URL template.",
                                }
                            )
        if external_documents:
            # The validator otherwise tries to retrieve remote refs with
            # urllib. Expand against the local bundle so validation remains
            # deterministic and offline.
            normalized = SpecBundle(document, external_documents).dereference(normalized)
        else:
            # A manually supplied local copy may contain remote schema refs
            # without the optional `openapi-refs` directory. Keep validation
            # offline in that case; runtime will expose an opaque body object
            # for the unresolved schema rather than downloading at startup.
            def replace_remote_refs(value: Any) -> Any:
                if isinstance(value, dict):
                    if isinstance(value.get("$ref"), str) and value["$ref"].startswith(
                        ("http://", "https://")
                    ):
                        return {"type": "object", "additionalProperties": True}
                    return {key: replace_remote_refs(child) for key, child in value.items()}
                if isinstance(value, list):
                    return [replace_remote_refs(child) for child in value]
                return value

            normalized = replace_remote_refs(normalized)
        validator(normalized)
    except ImportError as exc:
        raise SpecError(
            "openapi-spec-validator is required to validate the Swagger document"
        ) from exc
    except Exception as exc:  # validator versions expose different exception classes
        raise SpecError(f"Swagger validation failed: {exc}") from exc


def _external_urls(*documents: dict[str, Any]) -> set[str]:
    urls: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            reference = value.get("$ref")
            if isinstance(reference, str) and reference.startswith(("http://", "https://")):
                urls.add(urldefrag(reference)[0])
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    for document in documents:
        walk(document)
    return urls


class SpecStore:
    """Loads the configured local spec or the packaged official fallback."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def load(self) -> SpecBundle:
        if self.settings.openapi_path.is_file():
            document = _load_json(self.settings.openapi_path)
            refs_dir = self.settings.openapi_path.parent / "openapi-refs"
            external = self._load_refs(refs_dir)
            validate_spec(document, external)
            return SpecBundle(document, external)

        package_root = resources.files("strava_mcp.data")
        document = json.loads(package_root.joinpath("openapi.json").read_text(encoding="utf-8"))
        external: dict[str, dict[str, Any]] = {}
        for item in package_root.joinpath("schemas").iterdir():
            if item.name.endswith(".json"):
                external[f"{OFFICIAL_SCHEMA_BASE}{item.name}"] = json.loads(
                    item.read_text(encoding="utf-8")
                )
        validate_spec(document, external)
        return SpecBundle(document, external)

    @staticmethod
    def _load_refs(refs_dir: Path) -> dict[str, dict[str, Any]]:
        if not refs_dir.is_dir():
            return {}
        documents: dict[str, dict[str, Any]] = {}
        for path in refs_dir.glob("*.json"):
            documents[f"{OFFICIAL_SCHEMA_BASE}{path.name}"] = _load_json(path)
        return documents

    def update(self) -> tuple[str, Path, int]:
        """Download and validate a spec bundle before replacing the local copy."""
        try:
            with httpx.Client(
                timeout=self.settings.request_timeout, follow_redirects=True
            ) as client:
                response = client.get(self.settings.openapi_url)
                response.raise_for_status()
                document = response.json()
                pending_urls = set(_external_urls(document))
                external: dict[str, dict[str, Any]] = {}
                while pending_urls:
                    url = pending_urls.pop()
                    if url in external:
                        continue
                    schema_response = client.get(url)
                    schema_response.raise_for_status()
                    schema = schema_response.json()
                    if not isinstance(schema, dict):
                        raise SpecError(f"External Swagger document is not an object: {url}")
                    external[url] = schema
                    pending_urls.update(_external_urls(schema) - set(external))
                validate_spec(document, external)
        except (httpx.HTTPError, ValueError, SpecError) as exc:
            raise SpecError(
                f"Spec update aborted; the existing local spec was kept: {exc}"
            ) from exc

        target = self.settings.openapi_path
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=target.parent) as temporary:
            stage = Path(temporary)
            staged_spec = stage / target.name
            staged_spec.write_text(
                json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            staged_refs = stage / "openapi-refs"
            staged_refs.mkdir()
            for url, schema in external.items():
                filename = Path(urlparse(url).path).name or "schema.json"
                (staged_refs / filename).write_text(
                    json.dumps(schema, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            import os

            os.replace(staged_spec, target)
            refs_target = target.parent / "openapi-refs"
            refs_target.mkdir(exist_ok=True)
            for path in staged_refs.glob("*.json"):
                os.replace(path, refs_target / path.name)
        version = str(
            document.get("info", {}).get("version") or document.get("swagger") or "unknown"
        )
        return version, target, len(external)


def _resolve_parameter(bundle: SpecBundle, parameter: dict[str, Any]) -> dict[str, Any]:
    resolved = bundle.dereference(parameter)
    if not isinstance(resolved, dict):
        raise SpecError("Swagger parameter reference did not resolve to an object")
    return resolved


def _parameter_schema(bundle: SpecBundle, parameter: dict[str, Any]) -> dict[str, Any]:
    if parameter.get("in") == "formData" and parameter.get("type") == "file":
        return {
            "type": "string",
            "format": "path",
            "description": str(parameter.get("description") or "Path to the file to upload."),
            "x-strava-mcp-file": True,
        }
    if parameter.get("in") == "body":
        schema = parameter.get("schema")
    else:
        # Swagger 2.0 parameters contain transport metadata next to their
        # schema.  Only copy JSON Schema keywords here; copying the complete
        # parameter would incorrectly emit e.g. `required: true` inside
        # `properties.<parameter>`, which is not valid JSON Schema.
        schema = {
            key: copy.deepcopy(parameter[key])
            for key in PARAMETER_SCHEMA_KEYS
            if key in parameter
        }
    result = bundle.schema(schema)
    for key in ("enum", "default", "minimum", "maximum", "minItems", "maxItems", "format"):
        if key in parameter and key not in result:
            result[key] = parameter[key]
    if parameter.get("items") and "items" not in result:
        result["items"] = bundle.schema(parameter["items"])
    return result


def infer_scopes(description: str) -> ScopeRequirements:
    """Extract explicit scope statements while preserving Strava's alternatives."""
    text = " ".join(description.split())
    lower = text.lower()
    scopes = (
        "read",
        "read_all",
        "profile:read_all",
        "profile:write",
        "activity:read",
        "activity:read_all",
        "activity:write",
    )
    required: list[str] = []
    conditional: list[str] = []
    for scope in scopes:
        escaped = rf"(?<![A-Za-z0-9_:]){re.escape(scope)}(?![A-Za-z0-9_])"
        if re.search(rf"requires?[^.]*\b{escaped}\b", lower):
            if re.search(rf"also requires?[^.]*{escaped}|in order to[^.]*{escaped}", lower):
                conditional.append(scope)
            else:
                required.append(scope)
        elif scope == "read_all" and re.search(rf"\b{escaped}\s+scope required\b", lower):
            required.append(scope)
    if "activity:read" in required and re.search(r"activity:read_all", lower):
        required.remove("activity:read")
        if "activity:read_all" in required:
            required.remove("activity:read_all")
        alternatives = [("activity:read", "activity:read_all")]
    else:
        alternatives = []
    if "activity:read" in required and "activity:read_all" in required:
        required.remove("activity:read")
        required.remove("activity:read_all")
        alternatives.append(("activity:read", "activity:read_all"))
    return ScopeRequirements(
        required=tuple(dict.fromkeys(required)),
        alternatives=tuple(alternatives),
        conditional=tuple(dict.fromkeys(conditional)),
    )


def _operation_description(
    op: dict[str, Any], method: str, path: str, scopes: ScopeRequirements
) -> str:
    summary = op.get("summary") or ""
    description = op.get("description") or ""
    category = "read" if method == "get" else "destructive" if method == "delete" else "write"
    action = {
        "read": "Read-only operation.",
        "write": "This operation modifies Strava data.",
        "destructive": "WARNING: this operation may delete Strava data.",
    }[category]
    scope_note = f"\nScopes: {scopes.describe()}." if scopes.describe() else ""
    return (
        f"{summary}\n\nEndpoint:\n{method.upper()} {path}\n\n"
        f"Description:\n{description}\n\n{action}{scope_note}"
    ).strip()


def build_input_schema(parameters: list[ParameterBinding]) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for parameter in parameters:
        schema = copy.deepcopy(parameter.json_schema)
        if parameter.description and "description" not in schema:
            schema["description"] = parameter.description
        properties[parameter.name] = schema
        if parameter.required:
            required.append(parameter.name)
    result: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        result["required"] = required
    return result


def find_invalid_required_keywords(schema: Any) -> list[str]:
    """Return JSON paths where the JSON Schema ``required`` keyword is invalid."""

    invalid: list[str] = []

    def walk(value: Any, path: tuple[str, ...]) -> None:
        if not isinstance(value, dict):
            if isinstance(value, list):
                for index, child in enumerate(value):
                    walk(child, path + (str(index),))
            return

        if "required" in value:
            required = value["required"]
            if not isinstance(required, list) or not all(
                isinstance(item, str) for item in required
            ):
                invalid.append(".".join(path + ("required",)) or "required")

        # `properties` is a map whose keys are user-defined property names.
        # A property is allowed to be named `required`; it is not the JSON
        # Schema keyword at this level.
        properties = value.get("properties")
        if isinstance(properties, dict):
            for name, child in properties.items():
                walk(child, path + ("properties", str(name)))

        for key, child in value.items():
            if key != "properties":
                walk(child, path + (str(key),))

    walk(schema, ())
    return invalid


def normalize_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Copy and validate a generated MCP input schema without changing semantics.

    In particular, a user parameter named ``required`` remains under
    ``properties``.  The function only rejects malformed JSON Schema keyword
    values so generator regressions cannot reach an MCP client unnoticed.
    """

    normalized = copy.deepcopy(schema)
    invalid = find_invalid_required_keywords(normalized)
    if invalid:
        locations = ", ".join(invalid[:5])
        suffix = "" if len(invalid) <= 5 else ", ..."
        raise SpecError(
            "Generated input schema has an invalid JSON Schema `required` keyword at "
            f"{locations}{suffix}"
        )
    try:
        from jsonschema import Draft7Validator

        Draft7Validator.check_schema(normalized)
    except ImportError as exc:
        raise SpecError("jsonschema is required to validate generated MCP schemas") from exc
    except Exception as exc:
        raise SpecError(f"Generated MCP input schema is invalid: {exc}") from exc
    return normalized


def build_operations(bundle: SpecBundle) -> list[Operation]:
    operations: list[Operation] = []
    used_names: set[str] = set()
    for path, path_item in bundle.document.get("paths", {}).items():
        if not isinstance(path_item, dict):
            continue
        path_parameters = [
            _resolve_parameter(bundle, item) for item in path_item.get("parameters", [])
        ]
        for method in HTTP_METHODS:
            raw = path_item.get(method)
            if not isinstance(raw, dict):
                continue
            operation_parameters = path_parameters + [
                _resolve_parameter(bundle, item) for item in raw.get("parameters", [])
            ]
            # An operation-level parameter replaces the path-level parameter with the same identity.
            deduped: dict[tuple[str, str], dict[str, Any]] = {}
            for parameter in operation_parameters:
                identity = (str(parameter.get("name")), str(parameter.get("in")))
                deduped[identity] = parameter
            bindings: list[ParameterBinding] = []
            body_mode: str | None = None
            for parameter in deduped.values():
                location = parameter.get("in")
                if (
                    location == "path"
                    and f"{{{parameter.get('name')}}}" not in path
                    and "multipart/form-data" in " ".join(raw.get("consumes", []))
                ):
                    # Repair the same kind of malformed declaration present
                    # in the official PUT /athlete operation generically:
                    # an undeclared path placeholder in a multipart operation
                    # is a form field, not an URL segment.
                    location = "formData"
                if location not in {"path", "query", "body", "formData", "header"}:
                    continue
                if location == "body":
                    body_mode = "json"
                    body_schema = _parameter_schema(bundle, parameter)
                    if body_schema.get("type") == "object" and body_schema.get("properties"):
                        body_required = set(body_schema.get("required", []))
                        for name, schema in body_schema["properties"].items():
                            bindings.append(
                                ParameterBinding(
                                    name=name,
                                    original_name=name,
                                    location="body",
                                    required=name in body_required,
                                    schema=schema,
                                    description=str(schema.get("description") or ""),
                                    body_parameter=str(parameter.get("name") or "body"),
                                )
                            )
                    else:
                        bindings.append(
                            ParameterBinding(
                                name=str(parameter.get("name") or "body"),
                                original_name=str(parameter.get("name") or "body"),
                                location="body",
                                required=bool(parameter.get("required")),
                                schema=body_schema,
                                description=str(parameter.get("description") or "Request body"),
                                body_parameter=str(parameter.get("name") or "body"),
                            )
                        )
                    continue
                schema = _parameter_schema(bundle, parameter)
                bindings.append(
                    ParameterBinding(
                        name=str(parameter["name"]),
                        original_name=str(parameter["name"]),
                        location=str(location),
                        required=bool(parameter.get("required")),
                        schema=schema,
                        description=str(parameter.get("description") or ""),
                        collection_format=parameter.get("collectionFormat"),
                    )
                )
                if location == "formData":
                    body_mode = "form"
            declared_path_names = {
                parameter.original_name for parameter in bindings if parameter.location == "path"
            }
            for name in re.findall(r"{([^}]+)}", path):
                if name not in declared_path_names:
                    bindings.append(
                        ParameterBinding(
                            name=name,
                            original_name=name,
                            location="path",
                            required=True,
                            schema={"type": "string"},
                            description="Path parameter inferred from the URL template.",
                        )
                    )
            scopes = infer_scopes(str(raw.get("description") or ""))
            operation_id = raw.get("operationId")
            name = _tool_name(method, str(operation_id) if operation_id else None, path, used_names)
            category = (
                "read" if method == "get" else "destructive" if method == "delete" else "write"
            )
            operations.append(
                Operation(
                    method=method,
                    path=path,
                    operation_id=str(operation_id) if operation_id else None,
                    tool_name=name,
                    summary=str(raw.get("summary") or ""),
                    description=_operation_description(raw, method, path, scopes),
                    input_schema=build_input_schema(bindings),
                    parameters=bindings,
                    body_mode=body_mode,
                    category=category,
                    scopes=scopes,
                    tags=[str(tag) for tag in raw.get("tags", [])],
                )
            )
    return operations
