import pytest

from aep.model_invocation import (
    FakeModelAdapter,
    ModelConfiguration,
    ModelErrorClass,
    ModelInvocationError,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    model_invocation_record,
)


def request() -> ModelRequest:
    return ModelRequest(
        configuration=ModelConfiguration(
            model_ref={"kind": "Model", "name": "test-model", "version": "1.0.0"},
            provider="local",
            model="deterministic-test-model",
            parameters={"temperature": 0},
            timeout_ms=1000,
        ),
        input={"messages": [{"role": "user", "content": "Return JSON"}]},
        correlation={
            "traceId": "trace-123",
            "workflowExecutionId": "workflowexecution-123456789abc",
            "taskExecutionId": "taskexecution-123456789abc",
        },
    )


def test_fake_returns_normalized_output_usage_latency_and_metadata() -> None:
    response = ModelResponse(
        output={"answer": 42},
        usage=ModelUsage(input_tokens=8, output_tokens=3),
        latency_ms=12,
        provider_metadata={"requestId": "fake-1", "finishReason": "stop"},
    )
    adapter = FakeModelAdapter([response])

    result = adapter.invoke(request())

    assert result.output == {"answer": 42}
    assert result.usage.as_record() == {"input": 8, "output": 3}
    assert result.latency_ms == 12
    assert result.provider_metadata["requestId"] == "fake-1"
    assert adapter.requests[0].configuration.provider == "local"


def test_fake_is_configurable_and_repeats_final_outcome_deterministically() -> None:
    responses = [
        ModelResponse(output={"attempt": 1}, usage=ModelUsage(1, 1), latency_ms=2),
        ModelResponse(output={"attempt": 2}, usage=ModelUsage(1, 1), latency_ms=3),
    ]
    adapter = FakeModelAdapter(responses)

    assert adapter.invoke(request()).output == {"attempt": 1}
    assert adapter.invoke(request()).output == {"attempt": 2}
    assert adapter.invoke(request()).output == {"attempt": 2}


@pytest.mark.parametrize(
    ("classification", "recoverable"),
    [(ModelErrorClass.RECOVERABLE, True), (ModelErrorClass.PERMANENT, False)],
)
def test_errors_expose_retry_classification(
    classification: ModelErrorClass, recoverable: bool
) -> None:
    error = ModelInvocationError(
        "provider failure", classification=classification, code="provider_error"
    )
    adapter = FakeModelAdapter([error])

    with pytest.raises(ModelInvocationError) as raised:
        adapter.invoke(request())

    assert raised.value.recoverable is recoverable
    assert raised.value.code == "provider_error"


def test_builds_modelinvocation_record_with_model_and_execution_metadata() -> None:
    model_request = request()
    response = ModelResponse(
        output={"answer": 42},
        usage=ModelUsage(8, 3),
        latency_ms=12,
        provider_metadata={"requestId": "fake-1"},
        cost=0.01,
    )

    record = model_invocation_record(
        invocation_id="modelinvocation-123456789abc",
        agent_invocation_id="agentinvocation-123456789abc",
        request=model_request,
        response=response,
        started_at="2026-07-11T00:00:00Z",
        completed_at="2026-07-11T00:00:01Z",
        input_address="sha256:" + "1" * 64,
        output_address="sha256:" + "2" * 64,
        schema_validation="PASSED",
        provenance={
            "actor": "fake-model-adapter",
            "workflowExecutionId": "workflowexecution-123456789abc",
            "taskExecutionId": "taskexecution-123456789abc",
            "resourceRefs": [
                {"kind": "Model", "name": "test-model", "version": "1.0.0"}
            ],
        },
    )

    assert record["kind"] == "ModelInvocation"
    assert record["modelRef"] == model_request.configuration.model_ref
    assert record["modelConfiguration"] == model_request.configuration.as_record()
    assert record["agentInvocationId"] == "agentinvocation-123456789abc"
    assert record["tokenUsage"] == {"input": 8, "output": 3}
    assert record["latencyMs"] == 12
    assert record["providerMetadata"] == {"requestId": "fake-1"}
    assert record["traceId"] == "trace-123"
    assert record["provenance"]["workflowExecutionId"] == (
        "workflowexecution-123456789abc"
    )


def test_request_copies_mutable_configuration_and_input() -> None:
    model_ref = {"kind": "Model", "name": "test-model", "version": "1.0.0"}
    assembled_input = {"messages": ["original"]}
    configuration = ModelConfiguration(
        model_ref=model_ref, provider="local", model="test", parameters={}
    )
    model_request = ModelRequest(
        configuration=configuration,
        input=assembled_input,
        correlation={
            "traceId": "trace-123",
            "workflowExecutionId": "workflowexecution-123456789abc",
            "taskExecutionId": "taskexecution-123456789abc",
        },
    )
    model_ref["name"] = "changed"
    assembled_input["messages"] = ["changed"]

    assert configuration.model_ref["name"] == "test-model"
    assert model_request.input["messages"] == ["original"]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_configuration_rejects_nonfinite_parameters(value: float) -> None:
    with pytest.raises(ValueError, match="must be finite"):
        ModelConfiguration(
            model_ref={"kind": "Model", "name": "test-model", "version": "1.0.0"},
            provider="openai",
            model="gpt-5",
            parameters={"temperature": value},
        )
