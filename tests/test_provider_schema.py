import pytest

from aep.provider_schema import (
    OPENAI_RESPONSES_GPT5_ACCEPTED_KEYWORDS,
    OPENAI_RESPONSES_GPT5_PROVIDER_KEYWORDS,
    OPENAI_RESPONSES_GPT5_SCHEMA_COMPATIBILITY,
    StrictProviderSchemaError,
    validate_openai_strict_schema,
)


def object_schema(properties, required=None, **extra):
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": list(properties) if required is None else required,
        **extra,
    }


def test_reports_original_planner_nested_mismatch_at_stable_path():
    schema = object_schema({
        "acceptanceCriteriaClassifications": {
            "type": "array",
            "items": object_schema(
                {"criterion": {"type": "string"}, "classification": {"type": "string"},
                 "requiredInsertion": {"type": "null"}},
                ["criterion", "classification"],
            ),
        }
    })

    with pytest.raises(StrictProviderSchemaError) as raised:
        validate_openai_strict_schema(schema)

    assert raised.value.path == "$.properties.acceptanceCriteriaClassifications.items.required"
    assert raised.value.names == ("requiredInsertion",)


@pytest.mark.parametrize("schema", [{"type": "string"}, {"type": "array", "items": {"type": "string"}}])
def test_rejects_non_object_root_while_nested_scalars_remain_supported(schema):
    with pytest.raises(StrictProviderSchemaError) as raised:
        validate_openai_strict_schema(schema)
    assert raised.value.path == "$"
    assert raised.value.reason == "root schema type must be object"


@pytest.mark.parametrize(
    ("schema", "path"),
    [
        (object_schema({"value": {"type": "bogus"}}), "$.properties.<redacted>.type"),
        (object_schema({"value": {"type": "string"}}, ["value", "value"]), "$.required"),
    ],
)
def test_rejects_invalid_keyword_values_with_redacted_schema_paths(schema, path):
    with pytest.raises(StrictProviderSchemaError) as raised:
        validate_openai_strict_schema(schema)
    assert raised.value.path == path
    assert raised.value.reason == "invalid JSON Schema keyword value"


def test_rejects_root_anyof_but_allows_nested_anyof():
    nested = object_schema({
        "value": {"anyOf": [{"type": "string"}, {"type": "null"}]}
    })
    validate_openai_strict_schema(nested)

    root = {**nested, "anyOf": [object_schema({"other": {"type": "string"}})]}
    with pytest.raises(StrictProviderSchemaError) as raised:
        validate_openai_strict_schema(root)
    assert raised.value.path == "$.anyOf"


def test_rejects_type_union_and_redacts_dynamic_evidence_path():
    schema = object_schema({
        "secret-project-123": {
            "type": ["object", "null"],
            "additionalProperties": True,
            "properties": {"hidden-tenant": {"type": "string"}},
        }
    })
    with pytest.raises(StrictProviderSchemaError) as raised:
        validate_openai_strict_schema(schema)
    assert "secret-project-123" in raised.value.path
    safe_path = raised.value.safe_path
    assert safe_path == "$.properties.<redacted>.type"
    assert "secret-project-123" not in safe_path
    assert "hidden-tenant" not in safe_path


def test_dotted_property_name_preserves_exact_diagnostic_and_safe_boundary():
    secret_name = "secret.project-123"
    schema = object_schema({secret_name: {
        "type": "object",
        "additionalProperties": True,
        "properties": {},
        "required": [],
    }})
    with pytest.raises(StrictProviderSchemaError) as raised:
        validate_openai_strict_schema(schema)
    assert secret_name in raised.value.path
    assert raised.value.safe_path == "$.properties.<redacted>.additionalProperties"
    assert secret_name not in raised.value.safe_path


def test_references_must_resolve_to_audited_root_local_definitions():
    valid = object_schema(
        {"value": {"$ref": "#/$defs/Value"}},
        **{"$defs": {"Value": {"type": "string"}}},
    )
    validate_openai_strict_schema(valid)

    for reference in ("#/$defs/Missing", "https://example.test/schema.json"):
        invalid = object_schema(
            {"secret.project": {"$ref": reference}},
            **{"$defs": {"Value": {"type": "string"}}},
        )
        with pytest.raises(StrictProviderSchemaError) as raised:
            validate_openai_strict_schema(invalid)
        assert raised.value.safe_path == "$.properties.<redacted>.$ref"


@pytest.mark.parametrize(
    ("schema", "path"),
    [
        (object_schema({"nested": object_schema({"x": {"type": "string"}}, [])}),
         "$.properties.nested.required"),
        (object_schema({"values": {
            "type": "array",
            "items": object_schema({"x": {"type": "string"}}, ["x", "ghost"]),
        }}), "$.properties.values.items.required"),
        (object_schema({"nested": {"type": "object", "properties": {}, "required": []}}),
         "$.properties.nested.additionalProperties"),
        (object_schema({"value": {"anyOf": [object_schema({"x": {"type": "string"}}), {"type": "null"}]}}),
         None),
        (object_schema({"value": {"allOf": [{"type": "string"}]}}), "$.properties.value"),
        (object_schema({"value": {"oneOf": [{"type": "string"}]}}), "$.properties.value"),
        (object_schema({"value": {"not": {"type": "null"}}}), "$.properties.value"),
    ],
)
def test_recursive_strict_subset(schema, path):
    if path is None:
        validate_openai_strict_schema(schema)
    else:
        with pytest.raises(StrictProviderSchemaError) as raised:
            validate_openai_strict_schema(schema)
        assert raised.value.path == path


@pytest.mark.parametrize(("branch", "operation"), [(0, "write"), (1, "delete")])
def test_rejects_const_at_each_code_generator_discriminator(branch, operation):
    branches = [
        object_schema({"operation": {"type": "string", "enum": ["write"]}}),
        object_schema({"operation": {"type": "string", "enum": ["delete"]}}),
    ]
    branches[branch]["properties"]["operation"] = {"const": operation}
    schema = object_schema({"changes": {"type": "array", "items": {"anyOf": branches}}})

    with pytest.raises(StrictProviderSchemaError) as raised:
        validate_openai_strict_schema(schema)

    assert raised.value.path == (
        f"$.properties.changes.items.anyOf[{branch}].properties.operation"
    )
    assert raised.value.names == ("const",)


def test_typed_singleton_enums_are_in_reviewed_provider_contract():
    schema = object_schema({"operation": {"type": "string", "enum": ["write"]}})
    validate_openai_strict_schema(schema)
    assert OPENAI_RESPONSES_GPT5_SCHEMA_COMPATIBILITY["enum"] == "provider"
    assert "enum" in OPENAI_RESPONSES_GPT5_PROVIDER_KEYWORDS
    assert "const" not in OPENAI_RESPONSES_GPT5_ACCEPTED_KEYWORDS
