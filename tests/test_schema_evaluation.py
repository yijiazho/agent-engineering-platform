import pytest

from aep.runtime_store import InMemoryRuntimeObjectStore
from aep.schema_evaluation import (
    SchemaEvaluationContractError,
    _evaluation_result_validator,
    evaluate_schema,
)


SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["summary", "priority"],
    "properties": {
        "summary": {"type": "string"},
        "priority": {"type": "integer"},
    },
}


def evaluate(
    content: object,
    schema: dict[str, object] | None = None,
    *,
    evaluation_ref: dict[str, object] | None = None,
    target: dict[str, object] | None = None,
):
    store = InMemoryRuntimeObjectStore()
    result = evaluate_schema(
        store=store,
        result_id="evaluationresult-123456789abc",
        task_execution_id="taskexecution-123456789abc",
        evaluation_ref=evaluation_ref or {"kind": "Evaluation", "name": "output-schema", "version": "1.0.0"},
        target=target or {"type": "GeneratedArtifact", "id": "generatedartifact-123456789abc"},
        content=content,
        schema=SCHEMA if schema is None else schema,
        trace_id="trace-123",
        timestamp="2026-07-11T00:00:00Z",
        provenance={"actor": "schema-evaluator", "resourceRefs": []},
    )
    return store, result


def test_valid_output_passes_and_persists_complete_evidence() -> None:
    store, result = evaluate({"summary": "Fix validation", "priority": 1})

    assert result["outcome"] == "PASS"
    assert result["evaluationRef"]["name"] == "output-schema"
    assert result["target"]["type"] == "GeneratedArtifact"
    assert result["logs"] == ["JSON Schema validation passed"]
    assert result["evidence"]["valid"] is True
    assert result["traceId"] == "trace-123"
    assert store.get(result["id"]) == result
    with pytest.raises(TypeError):
        result["outcome"] = "FAIL"


def test_missing_field_reports_actionable_path() -> None:
    _, result = evaluate({"summary": "Fix validation"})

    assert result["outcome"] == "FAIL"
    assert result["evidence"]["errors"] == [
        {"path": "$.priority", "message": "'priority' is a required property"}
    ]


def test_invalid_type_fails() -> None:
    _, result = evaluate({"summary": "Fix validation", "priority": "high"})

    assert result["outcome"] == "FAIL"
    assert result["evidence"]["errors"][0]["path"] == "$.priority"
    assert "integer" in result["evidence"]["errors"][0]["message"]


def test_numeric_property_name_is_not_rendered_as_array_index() -> None:
    _, result = evaluate(
        {"123": "not-an-integer"},
        {
            "type": "object",
            "properties": {"123": {"type": "integer"}},
        },
    )

    assert result["evidence"]["errors"][0]["path"] == '$["123"]'


def test_malformed_schema_returns_failure_evidence() -> None:
    _, result = evaluate({}, {"type": 7})

    assert result["outcome"] == "FAIL"
    assert result["evidence"]["errors"][0]["path"] == "$schema"
    assert result["logs"][0].startswith("$schema: Invalid JSON Schema:")


def test_unsupported_declared_schema_version_fails() -> None:
    _, result = evaluate({}, {"$schema": "https://example.com/unknown-schema", "type": "object"})

    assert result["outcome"] == "FAIL"
    assert result["evidence"]["errors"] == [
        {
            "path": "$schema",
            "message": "Unsupported JSON Schema version: 'https://example.com/unknown-schema'",
        }
    ]


def test_errors_have_deterministic_path_order() -> None:
    _, first = evaluate({"priority": "high"})
    _, second = evaluate({"priority": "high"})

    first_errors = first["evidence"]["errors"]
    assert first_errors == second["evidence"]["errors"]
    assert [error["path"] for error in first_errors] == ["$.priority", "$.summary"]


def test_multiple_required_errors_use_their_specific_missing_properties() -> None:
    schema = {"type": "object", "required": ["alpha", "beta"]}

    _, result = evaluate({}, schema)

    assert [error["path"] for error in result["evidence"]["errors"]] == [
        "$.alpha",
        "$.beta",
    ]


@pytest.mark.parametrize(
    "evaluation_ref",
    [
        {"kind": "Evaluation", "name": "output-schema"},
        {"kind": "Evaluation", "name": "output-schema", "version": "latest"},
    ],
)
def test_invalid_evaluation_reference_is_not_persisted(evaluation_ref: dict[str, object]) -> None:
    with pytest.raises(SchemaEvaluationContractError, match="evaluationRef"):
        evaluate({}, evaluation_ref=evaluation_ref)


def test_unsupported_target_is_not_persisted() -> None:
    with pytest.raises(SchemaEvaluationContractError, match="target"):
        evaluate({}, target={"type": "UnknownInvocation", "id": "unknowninvocation-123456789abc"})


def test_invocation_output_target_conforms_to_runtime_contract() -> None:
    _, result = evaluate(
        {"summary": "Fix validation", "priority": 1},
        target={"type": "AgentInvocation", "id": "agentinvocation-123456789abc"},
    )

    assert result["target"]["type"] == "AgentInvocation"


def test_evaluation_result_validator_is_compiled_once() -> None:
    _evaluation_result_validator.cache_clear()

    first = _evaluation_result_validator()
    second = _evaluation_result_validator()

    assert second is first
    assert _evaluation_result_validator.cache_info().misses == 1
