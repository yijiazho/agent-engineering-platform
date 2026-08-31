import pytest

from aep.provider_schema import (
    StrictProviderSchemaError,
    redact_schema_path,
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
    safe_path = redact_schema_path(raised.value.path)
    assert safe_path == "$.properties.<redacted>.type"
    assert "secret-project-123" not in safe_path
    assert "hidden-tenant" not in safe_path


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
