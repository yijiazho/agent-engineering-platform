"""Provider-specific structured-output schema compatibility contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import re
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError


_COMPOSITIONS = ("anyOf",)

# Reviewed contract for OpenAI Responses API strict outputs on the deployed
# gpt-5 model generation.  Keep acceptance and projection derived from this
# table so adding a validator keyword cannot accidentally make it transmissible.
OPENAI_RESPONSES_GPT5_SCHEMA_COMPATIBILITY = {
    "$defs": "provider", "$ref": "provider", "type": "provider",
    "enum": "provider", "properties": "provider", "required": "provider",
    "additionalProperties": "provider", "items": "provider",
    "anyOf": "provider", "title": "provider", "description": "provider",
    "$comment": "provider", "default": "provider", "examples": "provider",
    "minItems": "provider", "maxItems": "provider", "minimum": "provider",
    "maximum": "provider", "pattern": "provider", "format": "provider",
    "minLength": "aep-only", "maxLength": "aep-only",
    "uniqueItems": "aep-only", "const": "unsupported",
    "allOf": "unsupported", "oneOf": "unsupported", "not": "unsupported",
}
OPENAI_RESPONSES_GPT5_PROVIDER_KEYWORDS = frozenset(
    key for key, support in OPENAI_RESPONSES_GPT5_SCHEMA_COMPATIBILITY.items()
    if support == "provider"
)
OPENAI_RESPONSES_GPT5_AEP_ONLY_KEYWORDS = frozenset(
    key for key, support in OPENAI_RESPONSES_GPT5_SCHEMA_COMPATIBILITY.items()
    if support == "aep-only"
)
OPENAI_RESPONSES_GPT5_ACCEPTED_KEYWORDS = (
    OPENAI_RESPONSES_GPT5_PROVIDER_KEYWORDS
    | OPENAI_RESPONSES_GPT5_AEP_ONLY_KEYWORDS
)


@dataclass(frozen=True)
class StrictProviderSchemaError(ValueError):
    """A safe, deterministic incompatibility with OpenAI strict outputs."""

    path: str
    reason: str
    names: tuple[str, ...] = ()
    evidence_path: str | None = None

    @property
    def safe_path(self) -> str:
        return self.evidence_path or self.path

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
    root_definitions = schema.get("$defs", {})
    _validate(
        schema,
        "$",
        safe_path="$",
        root_definitions=root_definitions,
        schema_position=True,
    )


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


def _validate(
    value: Any,
    path: str,
    *,
    safe_path: str,
    root_definitions: Any,
    schema_position: bool,
) -> None:
    if not isinstance(value, Mapping):
        if schema_position:
            raise StrictProviderSchemaError(
                path, "schema must be an object", evidence_path=safe_path
            )
        return

    unsupported = sorted(
        str(key) for key in value
        if key not in OPENAI_RESPONSES_GPT5_ACCEPTED_KEYWORDS
    )
    if unsupported:
        raise StrictProviderSchemaError(
            path,
            "unsupported keyword",
            tuple(unsupported),
            evidence_path=safe_path,
        )

    if isinstance(value.get("type"), list):
        raise StrictProviderSchemaError(
            f"{path}.type",
            "type unions are unsupported; use nested anyOf",
            evidence_path=f"{safe_path}.type",
        )

    reference = value.get("$ref")
    if reference is not None:
        target = _local_definition_target(reference)
        if target is None or not isinstance(root_definitions, Mapping) or target not in root_definitions:
            raise StrictProviderSchemaError(
                f"{path}.$ref",
                "reference must resolve to a root-local definition",
                evidence_path=f"{safe_path}.$ref",
            )

    properties = value.get("properties")
    object_schema = value.get("type") == "object" or isinstance(properties, Mapping)
    if object_schema:
        if not isinstance(properties, Mapping):
            raise StrictProviderSchemaError(
                f"{path}.properties",
                "object properties must be declared",
                evidence_path=f"{safe_path}.properties",
            )
        if value.get("additionalProperties") is not False:
            raise StrictProviderSchemaError(
                f"{path}.additionalProperties",
                "additionalProperties must be false",
                evidence_path=f"{safe_path}.additionalProperties",
            )
        required = value.get("required")
        if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
            raise StrictProviderSchemaError(
                f"{path}.required",
                "required must list every property",
                evidence_path=f"{safe_path}.required",
            )
        declared = set(properties)
        required_names = set(required)
        missing = tuple(sorted(declared - required_names))
        extra = tuple(sorted(required_names - declared))
        if missing:
            raise StrictProviderSchemaError(
                f"{path}.required",
                "declared properties are not required",
                missing,
                evidence_path=f"{safe_path}.required",
            )
        if extra:
            raise StrictProviderSchemaError(
                f"{path}.required",
                "required names are not declared",
                extra,
                evidence_path=f"{safe_path}.required",
            )
        for name, child in properties.items():
            _validate(
                child,
                f"{path}.properties.{name}",
                safe_path=f"{safe_path}.properties.<redacted>",
                root_definitions=root_definitions,
                schema_position=True,
            )

    items = value.get("items")
    if items is not None:
        _validate(
            items,
            f"{path}.items",
            safe_path=f"{safe_path}.items",
            root_definitions=root_definitions,
            schema_position=True,
        )

    definitions = value.get("$defs")
    if definitions is not None:
        if not isinstance(definitions, Mapping):
            raise StrictProviderSchemaError(
                f"{path}.$defs",
                "$defs must be an object",
                evidence_path=f"{safe_path}.$defs",
            )
        for name, child in definitions.items():
            _validate(
                child,
                f"{path}.$defs.{name}",
                safe_path=f"{safe_path}.$defs.<redacted>",
                root_definitions=root_definitions,
                schema_position=True,
            )

    for keyword in _COMPOSITIONS:
        branches = value.get(keyword)
        if branches is None:
            continue
        if not isinstance(branches, Sequence) or isinstance(branches, (str, bytes)) or not branches:
            raise StrictProviderSchemaError(
                f"{path}.{keyword}",
                f"{keyword} must contain schema branches",
                evidence_path=f"{safe_path}.{keyword}",
            )
        for index, child in enumerate(branches):
            _validate(
                child,
                f"{path}.{keyword}[{index}]",
                safe_path=f"{safe_path}.{keyword}[{index}]",
                root_definitions=root_definitions,
                schema_position=True,
            )


def _local_definition_target(reference: Any) -> str | None:
    if not isinstance(reference, str) or not reference.startswith("#/$defs/"):
        return None
    encoded = reference[len("#/$defs/"):]
    if not encoded or "/" in encoded or not re.fullmatch(r"(?:[^~]|~[01])*", encoded):
        return None
    decoded = encoded.replace("~1", "/").replace("~0", "~")
    return decoded
