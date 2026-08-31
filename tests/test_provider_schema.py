import pytest

from aep.provider_schema import StrictProviderSchemaError, validate_openai_strict_schema


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


@pytest.mark.parametrize(
    ("schema", "path"),
    [
        (object_schema({"nested": object_schema({"x": {"type": "string"}}, [])}),
         "$.properties.nested.required"),
        ({"type": "array", "items": object_schema({"x": {"type": "string"}}, ["x", "ghost"])},
         "$.items.required"),
        (object_schema({"nested": {"type": "object", "properties": {}, "required": []}}),
         "$.properties.nested.additionalProperties"),
        (object_schema({"value": {"anyOf": [object_schema({"x": {"type": "string"}}), {"type": "null"}]}}),
         None),
        (object_schema({"value": {"allOf": [{"type": "string"}]}}), None),
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
