import json
from pathlib import Path
from time import sleep

import pytest

from aep.tool_runtime import (
    FakeToolAdapter,
    JsonSchemaToolValidator,
    ToolCaller,
    ToolFailureClass,
    ToolRequest,
    ToolResult,
    ToolResultStatus,
    invoke_tool,
)


FIXTURES = Path(__file__).parents[1] / "fixtures" / "tool-runtime"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def request(data: dict | None = None) -> ToolRequest:
    value = data or load_fixture("success")["request"]
    return ToolRequest(
        tool_ref=value["toolRef"], input=value["input"],
        caller=ToolCaller(**value["caller"]), capabilities=value["capabilities"],
        timeout_ms=value["timeoutMs"], trace_id=value["traceId"],
    )


def result(data: dict) -> ToolResult:
    metrics = data["metrics"]
    from aep.tool_runtime import ToolMetrics
    return ToolResult(
        status=ToolResultStatus(data["status"]), output=data.get("output"),
        logs_ref=data.get("logsRef"),
        metrics=ToolMetrics(metrics["durationMs"], metrics.get("cpuMs"), metrics.get("memoryBytes")),
        started_at=data["timing"]["startedAt"], completed_at=data["timing"]["completedAt"],
        failure_class=ToolFailureClass(data["failureClass"]) if data.get("failureClass") else None,
        failure_message=data.get("failureMessage"),
    )


def validator() -> JsonSchemaToolValidator:
    return JsonSchemaToolValidator(
        {"type": "object", "required": ["path"], "properties": {"path": {"type": "string"}}},
        {"type": "object", "required": ["content"], "properties": {"content": {"type": "string"}}},
    )


def test_success_contract_contains_request_and_result_evidence() -> None:
    fixture = load_fixture("success")
    tool_request = request(fixture["request"])
    expected = result(fixture["result"])
    adapter = FakeToolAdapter([expected])

    actual = invoke_tool(
        tool_request, validator=validator(), authorize=lambda _: True, adapter=adapter
    )

    assert actual.status is ToolResultStatus.SUCCEEDED
    assert actual.output == {"content": "hello"}
    assert actual.logs_ref == "sha256:" + "a" * 64
    assert actual.metrics.duration_ms == 8
    assert tool_request.capabilities == ("filesystem.read",)
    assert tool_request.trace_id == "trace-tool-0001"


def test_invalid_input_is_normalized_before_adapter_execution() -> None:
    fixture = load_fixture("validation-failure")
    adapter = FakeToolAdapter([result(load_fixture("success")["result"])])

    actual = invoke_tool(
        request(fixture["request"]),
        validator=validator(),
        authorize=lambda _: True,
        adapter=adapter,
    )

    assert adapter.requests == []
    assert actual.status is ToolResultStatus.FAILED
    assert actual.failure_class is ToolFailureClass.VALIDATION


def test_policy_denial_does_not_execute_adapter() -> None:
    fixture = load_fixture("policy-denial")
    adapter = FakeToolAdapter([result(load_fixture("success")["result"])])

    actual = invoke_tool(
        request(fixture["request"]),
        validator=validator(),
        authorize=lambda _: False,
        adapter=adapter,
    )

    assert actual.status is ToolResultStatus.DENIED
    assert actual.failure_class is ToolFailureClass.POLICY
    assert adapter.requests == []


def test_adapter_exception_is_normalized() -> None:
    actual = invoke_tool(
        request(), validator=validator(), authorize=lambda _: True,
        adapter=FakeToolAdapter([RuntimeError("sandbox failed")]),
    )

    assert actual.status is ToolResultStatus.FAILED
    assert actual.failure_class is ToolFailureClass.ADAPTER
    assert actual.failure_message == "sandbox failed"


def test_runtime_returns_timeout_when_adapter_exceeds_deadline() -> None:
    fixture = load_fixture("timeout")

    class SlowAdapter(FakeToolAdapter):
        def invoke(self, tool_request):
            sleep(0.05)
            return result(load_fixture("success")["result"])

    actual = invoke_tool(
        request(fixture["request"]), validator=validator(), authorize=lambda _: True,
        adapter=SlowAdapter([result(load_fixture("success")["result"])]),
    )

    assert actual.status is ToolResultStatus.TIMED_OUT
    assert actual.failure_class is ToolFailureClass.TIMEOUT


def test_invalid_adapter_output_is_normalized() -> None:
    successful = result(load_fixture("success")["result"])
    invalid = ToolResult(
        status=successful.status, output={"unexpected": True},
        logs_ref=successful.logs_ref, metrics=successful.metrics,
        started_at=successful.started_at, completed_at=successful.completed_at,
    )

    actual = invoke_tool(
        request(), validator=validator(), authorize=lambda _: True,
        adapter=FakeToolAdapter([invalid]),
    )

    assert actual.status is ToolResultStatus.FAILED
    assert actual.failure_class is ToolFailureClass.VALIDATION


def test_model_references_are_explicitly_excluded() -> None:
    data = load_fixture("success")["request"]
    data["toolRef"] = {"kind": "Model", "name": "provider", "version": "1.0.0"}
    with pytest.raises(ValueError, match="Model provider calls are excluded"):
        request(data)


def test_floating_tool_references_are_excluded() -> None:
    data = load_fixture("success")["request"]
    data["toolRef"]["version"] = "latest"
    with pytest.raises(ValueError, match="immutable version"):
        request(data)


def test_request_and_result_evidence_are_recursively_immutable() -> None:
    data = load_fixture("success")["request"]
    data["input"]["nested"] = {"items": ["original"]}
    tool_request = request(data)
    tool_result = result(load_fixture("success")["result"])

    with pytest.raises(TypeError):
        tool_request.input["nested"]["items"][0] = "changed"
    with pytest.raises(TypeError):
        tool_result.output["content"] = "changed"

    output_record = tool_result.output_record()
    output_record["content"] = "changed"
    assert tool_result.output["content"] == "hello"
