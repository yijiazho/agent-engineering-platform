import json
from pathlib import Path

import pytest

from aep.build_test_evaluation import (
    BuildTestEvaluationContractError,
    ValidationExpectation,
    evaluate_build_and_test,
)
from aep.runtime_store import InMemoryRuntimeObjectStore


ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "fixtures" / "build-test-evaluation" / "passing-tool-invocation.json"


def invocation() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def expectation(name: str, command_index: int) -> ValidationExpectation:
    return ValidationExpectation(
        evaluation_ref={"kind": "Evaluation", "name": name, "version": "1.0.0"},
        command_index=command_index,
    )


def docker_tool_ref() -> dict[str, str]:
    return {"kind": "Tool", "name": "docker-validation", "version": "1.0.0"}


def evaluate(
    value: dict | None = None,
    *,
    store: InMemoryRuntimeObjectStore | None = None,
    build_result_id: str = "evaluationresult-aaaabbbb0001",
    test_result_id: str = "evaluationresult-aaaabbbb0002",
    configured_tool_ref: dict[str, str] | None = None,
):
    store = store or InMemoryRuntimeObjectStore()
    results = evaluate_build_and_test(
        store=store,
        build_result_id=build_result_id,
        test_result_id=test_result_id,
        task_execution_id="taskexecution-aaaabbbb0001",
        tool_invocation=value if value is not None else invocation(),
        docker_tool_ref=configured_tool_ref or docker_tool_ref(),
        build_expectation=expectation("build", 0),
        test_expectation=expectation("test", 1),
        correlation={
            "traceId": "trace-validation-0001",
            "workflowExecutionId": "workflowexecution-aaaabbbb0001",
            "taskExecutionId": "taskexecution-aaaabbbb0001",
        },
        timestamp="2026-08-04T10:01:00Z",
        provenance={
            "actor": "build-test-evaluator",
            "workflowExecutionId": "workflowexecution-aaaabbbb0001",
            "taskExecutionId": "taskexecution-aaaabbbb0001",
            "resourceRefs": [
                {"kind": "Evaluation", "name": "build", "version": "1.0.0"},
                {"kind": "Evaluation", "name": "test", "version": "1.0.0"},
            ],
        },
    )
    return store, results


def test_passing_build_and_test_create_separate_immutable_results() -> None:
    store, (build, test) = evaluate()

    assert build["outcome"] == "PASS"
    assert build["evidence"]["commandStatus"] == "PASSED"
    assert build["metrics"]["durationMs"] == 1200
    assert build["logsAddress"].endswith("a" * 64)
    assert test["outcome"] == "PASS"
    assert test["evidence"]["commandStatus"] == "PASSED"
    assert test["metrics"]["durationMs"] == 1800
    assert test["logsAddress"].endswith("b" * 64)
    assert build["target"] == {
        "type": "ToolInvocation",
        "id": "toolinvocation-aaaabbbb0001",
    }
    assert store.get(build["id"]) == build
    assert store.get(test["id"]) == test
    with pytest.raises(TypeError):
        build["outcome"] = "FAIL"


def test_nonzero_build_fails_build_and_records_test_as_not_run() -> None:
    value = invocation()
    value["status"] = "FAILED"
    value["resultStatus"] = "FAILED"
    value["output"]["commands"][0]["exitCode"] = 2
    value["output"]["commands"] = value["output"]["commands"][:1]

    _, (build, test) = evaluate(value)

    assert build["status"] == "SUCCEEDED"
    assert build["outcome"] == "FAIL"
    assert build["evidence"]["commandStatus"] == "FAILED"
    assert build["evidence"]["exitCode"] == 2
    assert test["status"] == "SUCCEEDED"
    assert test["outcome"] == "FAIL"
    assert test["evidence"]["commandStatus"] == "NOT_RUN"


def test_nonzero_test_fails_only_test_evaluation() -> None:
    value = invocation()
    value["status"] = "FAILED"
    value["resultStatus"] = "FAILED"
    value["output"]["commands"][1]["exitCode"] = 7

    _, (build, test) = evaluate(value)

    assert build["outcome"] == "PASS"
    assert test["outcome"] == "FAIL"
    assert test["evidence"]["commandStatus"] == "FAILED"
    assert test["evidence"]["exitCode"] == 7


def test_test_timeout_preserves_completed_build_result() -> None:
    value = invocation()
    value["status"] = "FAILED"
    value["resultStatus"] = "TIMED_OUT"
    value["output"]["commands"] = value["output"]["commands"][:1]

    _, (build, test) = evaluate(value)

    assert build["outcome"] == "PASS"
    assert test["status"] == "SUCCEEDED"
    assert test["outcome"] == "FAIL"
    assert test["evidence"]["commandStatus"] == "TIMED_OUT"
    assert test["logsAddress"].endswith("c" * 64)


def test_timeout_before_build_marks_build_timed_out_and_test_not_run() -> None:
    value = invocation()
    value["status"] = "FAILED"
    value["resultStatus"] = "TIMED_OUT"
    value["output"]["commands"] = []

    _, (build, test) = evaluate(value)

    assert build["evidence"]["commandStatus"] == "TIMED_OUT"
    assert test["evidence"]["commandStatus"] == "NOT_RUN"


def test_missing_validation_output_creates_configuration_failures() -> None:
    value = invocation()
    value["status"] = "FAILED"
    value["resultStatus"] = "FAILED"
    del value["output"]

    _, (build, test) = evaluate(value)

    for result in (build, test):
        assert result["status"] == "FAILED"
        assert result["outcome"] == "FAIL"
        assert result["failure"]["class"] == "CONFIGURATION"
        assert result["evidence"]["commandStatus"] == "INVALID_OUTPUT"


def test_incomplete_command_output_is_a_configuration_failure() -> None:
    value = invocation()
    del value["output"]["commands"][1]["logsRef"]

    _, (build, test) = evaluate(value)

    assert build["outcome"] == "PASS"
    assert test["status"] == "FAILED"
    assert test["failure"]["class"] == "CONFIGURATION"
    assert "incomplete" in test["failure"]["message"]


def test_missing_configured_command_is_a_configuration_failure() -> None:
    value = invocation()
    value["input"]["commands"] = value["input"]["commands"][:1]
    value["output"]["commands"] = value["output"]["commands"][:1]

    _, (build, test) = evaluate(value)

    assert build["outcome"] == "PASS"
    assert test["status"] == "FAILED"
    assert test["failure"]["class"] == "CONFIGURATION"


def test_nonterminal_invocation_is_rejected_without_persistence() -> None:
    value = invocation()
    value["status"] = "RUNNING"

    with pytest.raises(BuildTestEvaluationContractError, match="terminal"):
        evaluate(value)


def test_non_docker_tool_invocation_is_rejected_without_persistence() -> None:
    value = invocation()
    value["toolRef"] = {"kind": "Tool", "name": "github", "version": "1.0.0"}
    store = InMemoryRuntimeObjectStore()

    with pytest.raises(BuildTestEvaluationContractError, match="configured Docker"):
        evaluate(value, store=store)

    assert store.list_by_task_execution("taskexecution-aaaabbbb0001") == ()


def test_missing_result_status_is_rejected_without_persistence() -> None:
    value = invocation()
    del value["resultStatus"]
    store = InMemoryRuntimeObjectStore()

    with pytest.raises(BuildTestEvaluationContractError, match="resultStatus"):
        evaluate(value, store=store)

    assert store.list_by_task_execution("taskexecution-aaaabbbb0001") == ()


@pytest.mark.parametrize(
    ("status", "result_status"),
    [("FAILED", "SUCCEEDED"), ("SUCCEEDED", "FAILED"), ("SUCCEEDED", "TIMED_OUT")],
)
def test_inconsistent_status_and_result_status_are_rejected(
    status: str, result_status: str
) -> None:
    value = invocation()
    value["status"] = status
    value["resultStatus"] = result_status

    with pytest.raises(BuildTestEvaluationContractError, match="inconsistent"):
        evaluate(value)


def test_extra_output_command_is_shared_configuration_failure() -> None:
    value = invocation()
    value["output"]["commands"].append(
        {
            **value["output"]["commands"][1],
            "argv": ["python", "-m", "lint"],
        }
    )

    _, results = evaluate(value)

    for result in results:
        assert result["status"] == "FAILED"
        assert result["failure"]["class"] == "CONFIGURATION"
        assert "extra command" in result["failure"]["message"]


def test_misordered_output_commands_are_shared_configuration_failure() -> None:
    value = invocation()
    value["output"]["commands"].reverse()

    _, results = evaluate(value)

    for result in results:
        assert result["status"] == "FAILED"
        assert result["failure"]["class"] == "CONFIGURATION"
        assert "configured order" in result["failure"]["message"]


def test_trailing_output_after_nonzero_exit_is_shared_configuration_failure() -> None:
    value = invocation()
    value["status"] = "FAILED"
    value["resultStatus"] = "FAILED"
    value["output"]["commands"][0]["exitCode"] = 2

    _, results = evaluate(value)

    for result in results:
        assert result["status"] == "FAILED"
        assert result["failure"]["class"] == "CONFIGURATION"
        assert "after a nonzero exit" in result["failure"]["message"]


def test_duplicate_result_ids_are_rejected_before_any_persistence() -> None:
    store = InMemoryRuntimeObjectStore()

    with pytest.raises(BuildTestEvaluationContractError, match="must be different"):
        evaluate(
            store=store,
            build_result_id="evaluationresult-aaaabbbb0001",
            test_result_id="evaluationresult-aaaabbbb0001",
        )

    assert store.list_by_task_execution("taskexecution-aaaabbbb0001") == ()


def test_floating_evaluation_reference_is_rejected_before_persistence() -> None:
    store = InMemoryRuntimeObjectStore()

    with pytest.raises(BuildTestEvaluationContractError, match="evaluationRef"):
        evaluate_build_and_test(
            store=store,
            build_result_id="evaluationresult-aaaabbbb0001",
            test_result_id="evaluationresult-aaaabbbb0002",
            task_execution_id="taskexecution-aaaabbbb0001",
            tool_invocation=invocation(),
            docker_tool_ref=docker_tool_ref(),
            build_expectation=ValidationExpectation(
                {"kind": "Evaluation", "name": "build", "version": "latest"}, 0
            ),
            test_expectation=expectation("test", 1),
            correlation={
                "traceId": "trace-validation-0001",
                "workflowExecutionId": "workflowexecution-aaaabbbb0001",
                "taskExecutionId": "taskexecution-aaaabbbb0001",
            },
            timestamp="2026-08-04T10:01:00Z",
            provenance={"actor": "evaluator", "resourceRefs": []},
        )

    assert store.list_by_task_execution("taskexecution-aaaabbbb0001") == ()
