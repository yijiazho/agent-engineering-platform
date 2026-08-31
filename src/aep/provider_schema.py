"""Provider-specific structured-output schema compatibility contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError


_COMPOSITIONS = ("anyOf",)
_ANNOTATIONS = frozenset({"title", "description", "$comment", "default", "examples"})
_AEP_ONLY_VALIDATION = frozenset({"minLength", "maxLength", "uniqueItems"})
_SUPPORTED = frozenset(
    {
        "$defs", "$ref", "type", "enum", "const", "properties", "required",
        "additionalProperties", "items", *_COMPOSITIONS, *_ANNOTATIONS,
        "minItems", "maxItems", "minimum", "maximum", "pattern", "format",
        *_AEP_ONLY_VALIDATION,
    }
)


@dataclass(frozen=True)
class StrictProviderSchemaError(ValueError):
    """A safe, deterministic incompatibility with OpenAI strict outputs."""

    path: str
    reason: str
    names: tuple[str, ...] = ()

    def __str__(self) -> str:
        detail = f" ({', '.join(self.names)})" if self.names else ""
        return f"OpenAI strict output schema is incompatible at {self.path}: {self.reason}{detail}"


def validate_openai_strict_schema(schema: Any) -> None:
    """Validate the recursive subset accepted by OpenAI strict Structured Outputs.

    AEP-only validation keywords are permitted because provider projection removes
    them without changing structure; AEP applies the complete schema afterward.
    """

    if not isinstance(schema, Mapping) or schema.get("type") != "object":
        raise StrictProviderSchemaError("$", "root schema type must be object")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as error:
        raise StrictProviderSchemaError(
            _safe_schema_error_path(error.absolute_path),
            "invalid JSON Schema keyword value",
        ) from None
    if "anyOf" in schema:
        raise StrictProviderSchemaError("$.anyOf", "anyOf is not supported at the root")
    _validate(schema, "$", schema_position=True)


def redact_schema_path(path: str) -> str:
    """Redact declared property and definition names from an evidence path."""

    segments = path.split(".")
    rendered: list[str] = []
    redact_next = False
    for segment in segments:
        if redact_next:
            suffix = ""
            if "[" in segment:
                _, suffix = segment.split("[", 1)
                suffix = "[" + suffix
            rendered.append("<redacted>" + suffix)
            redact_next = False
        else:
            rendered.append(segment)
            redact_next = segment in {"properties", "$defs"}
    return ".".join(rendered)


def _safe_schema_error_path(parts: Sequence[Any]) -> str:
    """Render a useful schema path without retaining declared property names."""

    rendered = "$"
    redact_next = False
    structural = {
        "$defs", "additionalProperties", "allOf", "anyOf", "enum", "items",
        "oneOf", "properties", "required", "type",
    }
    for part in parts:
        if isinstance(part, int):
            rendered += f"[{part}]"
            continue
        value = str(part)
        if redact_next:
            rendered += ".<redacted>"
            redact_next = False
        elif value in structural:
            rendered += f".{value}"
            redact_next = value in {"$defs", "properties"}
        else:
            rendered += ".<redacted>"
    return rendered


def _validate(value: Any, path: str, *, schema_position: bool) -> None:
    if not isinstance(value, Mapping):
        if schema_position:
            raise StrictProviderSchemaError(path, "schema must be an object")
        return

    unsupported = sorted(str(key) for key in value if key not in _SUPPORTED)
    if unsupported:
        raise StrictProviderSchemaError(path, "unsupported keyword", tuple(unsupported))

    if isinstance(value.get("type"), list):
        raise StrictProviderSchemaError(
            f"{path}.type", "type unions are unsupported; use nested anyOf"
        )

    properties = value.get("properties")
    object_schema = value.get("type") == "object" or isinstance(properties, Mapping)
    if object_schema:
        if not isinstance(properties, Mapping):
            raise StrictProviderSchemaError(f"{path}.properties", "object properties must be declared")
        if value.get("additionalProperties") is not False:
            raise StrictProviderSchemaError(
                f"{path}.additionalProperties", "additionalProperties must be false"
            )
        required = value.get("required")
        if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
            raise StrictProviderSchemaError(f"{path}.required", "required must list every property")
        declared = set(properties)
        required_names = set(required)
        missing = tuple(sorted(declared - required_names))
        extra = tuple(sorted(required_names - declared))
        if missing:
            raise StrictProviderSchemaError(f"{path}.required", "declared properties are not required", missing)
        if extra:
            raise StrictProviderSchemaError(f"{path}.required", "required names are not declared", extra)
        for name, child in properties.items():
            _validate(child, f"{path}.properties.{name}", schema_position=True)

    items = value.get("items")
    if items is not None:
        _validate(items, f"{path}.items", schema_position=True)

    definitions = value.get("$defs")
    if definitions is not None:
        if not isinstance(definitions, Mapping):
            raise StrictProviderSchemaError(f"{path}.$defs", "$defs must be an object")
        for name, child in definitions.items():
            _validate(child, f"{path}.$defs.{name}", schema_position=True)

    for keyword in _COMPOSITIONS:
        branches = value.get(keyword)
        if branches is None:
            continue
        if not isinstance(branches, Sequence) or isinstance(branches, (str, bytes)) or not branches:
            raise StrictProviderSchemaError(f"{path}.{keyword}", f"{keyword} must contain schema branches")
        for index, child in enumerate(branches):
            _validate(child, f"{path}.{keyword}[{index}]", schema_position=True)
