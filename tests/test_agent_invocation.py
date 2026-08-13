from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from threading import Event, Lock

import pytest

from jsonschema import Draft202012Validator
from referencing import Registry, Resource as SchemaResource
from referencing.jsonschema import DRAFT202012

from aep.agent_invocation import invoke_agent
from aep.model_invocation import (
    FakeModelAdapter,
    ModelConfiguration,
    ModelAdapter,
    ModelErrorClass,
    ModelInvocationError,
    ModelResponse,
    ModelUsage,
)
from aep.observability import StructuredLifecycleLogger
from aep.openai_model_provider import (
    OpenAIModelAdapter,
    OpenAIProviderConfig,
    ProviderHttpResponse,
)
from aep.runtime_store import InMemoryRuntimeObjectStore


ROOT = Path(__file__).parents[1]
AGENT_ID = "agentinvocation-123456789abc"
MODEL_ID = "modelinvocation-123456789abc"


def test_success_persists_model_evidence_and_deterministically_assembled_input() -> None:
    store = InMemoryRuntimeObjectStore()
    adapter = FakeModelAdapter(
        [
            ModelResponse(
                output={"summary": "Implement the contract"},
                usage=ModelUsage(input_tokens=41, output_tokens=7),
                latency_ms=19,
                provider_metadata={"requestId": "fake-request-1", "finishReason": "stop"},
                cost=0.0125,
            )
        ]
    )
    logs = []

    result = invoke(adapter=adapter, store=store, logger=StructuredLifecycleLogger(logs.append))

    assert result["status"] == "SUCCEEDED"
    assert result["resolvedAgentId"] == resolved_agent()["id"]
    assert result["contextPackageId"] == context_package()["id"]
    assert result["modelInvocationIds"] == [MODEL_ID]
    assert result["output"] == {"summary": "Implement the contract"}
    assert result["outputSchemaValidation"] == "PASSED"
    assert result["tokenUsage"] == {"input": 41, "output": 7}
    assert result["cost"] == 0.0125

    model = store.get(MODEL_ID)
    assert model is not None
    assert model["status"] == "SUCCEEDED"
    assert model["agentInvocationId"] == AGENT_ID
    assert model["tokenUsage"] == {"input": 41, "output": 7}
    assert model["latencyMs"] == 19
    assert model["cost"] == 0.0125
    assert model["providerMetadata"] == {"requestId": "fake-request-1", "finishReason": "stop"}
    assert model["modelConfiguration"] == model_configuration().as_record()
    assert model["schemaValidation"] == "PASSED"
    assert model["inputAddress"].startswith("sha256:")
    assert model["outputAddress"].startswith("sha256:")
    assert list(runtime_validator("agentinvocation").iter_errors(dict(result))) == []
    assert list(runtime_validator("modelinvocation").iter_errors(dict(model))) == []

    request = adapter.requests[0]
    assert request.input == {
        "prompt": {
            "system": "Use only supplied context.",
            "formatting": "Return JSON.",
        },
        "contextPackage": {
            "id": "contextpackage-123456789abc",
            "repositoryRevision": "abc1234",
            "elements": [
                {
                    "type": "repository",
                    "content": {"path": "README.md", "summary": "AEP contract"},
                    "provenance": {"actor": "repository-knowledge-query", "resourceRefs": []},
                }
            ],
        },
        "outputSchema": output_schema(),
    }
    assert [entry["eventName"] for entry in logs] == [
        "AgentInvocationStarted",
        "ModelInvocationStarted",
        "ModelInvocationCompleted",
        "AgentInvocationCompleted",
    ]


def test_provider_failure_persists_both_failed_records_with_classification() -> None:
    store = InMemoryRuntimeObjectStore()
    adapter = FakeModelAdapter(
        [
            ModelInvocationError(
                "provider unavailable",
                classification=ModelErrorClass.RECOVERABLE,
                code="unavailable",
                provider_metadata={"requestId": "fake-failed-1"},
            )
        ]
    )

    result = invoke(adapter=adapter, store=store)

    assert result["status"] == "FAILED"
    assert result["failure"] == {
        "class": "RECOVERABLE",
        "message": "provider unavailable",
        "retryable": True,
    }
    assert result["outputSchemaValidation"] == "NOT_RUN"
    model = store.get(MODEL_ID)
    assert model is not None
    assert model["status"] == "FAILED"
    assert model["failure"] == result["failure"]
    assert model["providerMetadata"] == {
        "requestId": "fake-failed-1",
        "errorCode": "unavailable",
    }


def test_adapter_configuration_failure_terminalizes_both_invocations() -> None:
    store = InMemoryRuntimeObjectStore()
    configuration = ModelConfiguration(
        model_ref={"kind": "Model", "name": "test-model", "version": "1.0.0"},
        provider="openai",
        model="gpt-5",
        parameters={"previous_response_id": "must-not-use-provider-state"},
    )
    agent = resolved_agent()
    agent["modelParameters"] = dict(configuration.parameters)
    agent["modelConfiguration"] = configuration.as_record()
    logs = []

    result = invoke_agent(
        store=store,
        invocation_id=AGENT_ID,
        model_invocation_id=MODEL_ID,
        resolved_agent=agent,
        context_package=context_package(),
        prompt=prompt(),
        model_configuration=configuration,
        adapter=OpenAIModelAdapter(
            OpenAIProviderConfig(api_key="sk-must-not-leak"),
            transport=NoCallTransport(),
        ),
        started_at="2026-08-06T12:00:00Z",
        completed_at="2026-08-06T12:00:01Z",
        lifecycle_logger=StructuredLifecycleLogger(logs.append),
    )

    model = store.get(MODEL_ID)
    assert result["status"] == "FAILED"
    assert result["failure"] == {
        "class": "PERMANENT",
        "message": "model provider configuration contains unsupported parameters",
        "retryable": False,
    }
    assert model is not None and model["status"] == "FAILED"
    assert model["failure"] == result["failure"]
    assert model["providerMetadata"] == {"errorCode": "invalid_configuration"}
    assert "sk-must-not-leak" not in repr(logs)
    assert [entry["eventName"] for entry in logs] == [
        "AgentInvocationStarted",
        "ModelInvocationStarted",
        "ModelInvocationFailed",
        "AgentInvocationFailed",
    ]


def test_deep_provider_json_terminalizes_both_invocations_as_malformed() -> None:
    class DeepResponseTransport:
        def request(self, **_request):
            return ProviderHttpResponse(
                status=200,
                headers={},
                body=("[" * 2000 + "]" * 2000).encode(),
            )

    store = InMemoryRuntimeObjectStore()
    configuration = ModelConfiguration(
        model_ref={"kind": "Model", "name": "test-model", "version": "1.0.0"},
        provider="openai",
        model="gpt-5",
        parameters={"temperature": 0},
        timeout_ms=1_000,
    )
    agent = resolved_agent()
    agent["modelParameters"] = dict(configuration.parameters)
    agent["modelConfiguration"] = configuration.as_record()

    result = invoke_agent(
        store=store,
        invocation_id=AGENT_ID,
        model_invocation_id=MODEL_ID,
        resolved_agent=agent,
        context_package=context_package(),
        prompt=prompt(),
        model_configuration=configuration,
        adapter=OpenAIModelAdapter(
            OpenAIProviderConfig(api_key="sk-must-not-leak"),
            transport=DeepResponseTransport(),
        ),
        started_at="2026-08-06T12:00:00Z",
        completed_at="2026-08-06T12:00:01Z",
    )

    model = store.get(MODEL_ID)
    assert result["status"] == "FAILED"
    assert result["failure"]["class"] == "PERMANENT"
    assert model is not None and model["status"] == "FAILED"
    assert model["providerMetadata"]["errorCode"] == "malformed_response"


def test_invalid_structured_output_fails_agent_but_records_successful_model_call() -> None:
    store = InMemoryRuntimeObjectStore()
    adapter = FakeModelAdapter(
        [ModelResponse(output={"wrong": True}, usage=ModelUsage(5, 2), latency_ms=3)]
    )

    result = invoke(adapter=adapter, store=store)

    assert result["status"] == "FAILED"
    assert result["output"] == {"wrong": True}
    assert result["outputSchemaValidation"] == "FAILED"
    assert result["failure"]["class"] == "EVALUATION"
    assert "$.summary" in result["failure"]["message"]
    model = store.get(MODEL_ID)
    assert model is not None
    assert model["status"] == "SUCCEEDED"
    assert model["schemaValidation"] == "FAILED"


def test_invocation_has_no_repository_retrieval_path_and_uses_only_context_copy() -> None:
    package = context_package()
    original = deepcopy(package)
    adapter = FakeModelAdapter(
        [ModelResponse(output={"summary": "done"}, usage=ModelUsage(1, 1), latency_ms=1)]
    )

    invoke_agent(
        store=InMemoryRuntimeObjectStore(),
        invocation_id=AGENT_ID,
        model_invocation_id=MODEL_ID,
        resolved_agent=resolved_agent(),
        context_package=package,
        prompt=prompt(),
        model_configuration=model_configuration(),
        adapter=adapter,
        started_at="2026-08-06T12:00:00Z",
        completed_at="2026-08-06T12:00:01Z",
    )

    assert package == original
    assert adapter.requests[0].input["contextPackage"]["elements"] == original["elements"]
    assert set(adapter.requests[0].input) == {"prompt", "contextPackage", "outputSchema"}


def test_rejects_context_package_with_conflicting_workflow_provenance_before_provider() -> None:
    package = context_package()
    package["provenance"]["workflowExecutionId"] = "workflowexecution-ffffffffffff"
    adapter = FakeModelAdapter(
        [ModelResponse(output={"summary": "unused"}, usage=ModelUsage(1, 1), latency_ms=1)]
    )

    with pytest.raises(ValueError, match="must share trace, WorkflowExecution"):
        invoke_agent(
            store=InMemoryRuntimeObjectStore(),
            invocation_id=AGENT_ID,
            model_invocation_id=MODEL_ID,
            resolved_agent=resolved_agent(),
            context_package=package,
            prompt=prompt(),
            model_configuration=model_configuration(),
            adapter=adapter,
            started_at="2026-08-06T12:00:00Z",
            completed_at="2026-08-06T12:00:01Z",
        )

    assert adapter.requests == []


def test_concurrent_identical_calls_claim_one_provider_execution() -> None:
    store = InMemoryRuntimeObjectStore()
    adapter = BlockingModelAdapter()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(invoke, adapter=adapter, store=store)
        assert adapter.entered.wait(timeout=2)
        second = executor.submit(invoke, adapter=adapter, store=store)
        second_result = second.result(timeout=2)
        adapter.release.set()
        first_result = first.result(timeout=2)

    assert len(adapter.requests) == 1
    assert second_result["status"] == "RUNNING"
    assert first_result["status"] == "SUCCEEDED"
    assert store.get(AGENT_ID)["status"] == "SUCCEEDED"
    assert store.get(MODEL_ID)["status"] == "SUCCEEDED"


def test_non_json_provider_output_persists_terminal_invalid_output_evidence() -> None:
    store = InMemoryRuntimeObjectStore()
    adapter = FakeModelAdapter(
        [ModelResponse(output={"summary": {"not-json"}}, usage=ModelUsage(2, 1), latency_ms=4)]
    )

    result = invoke(adapter=adapter, store=store)

    assert result["status"] == "FAILED"
    assert result["outputSchemaValidation"] == "FAILED"
    assert "not JSON-compatible" in result["failure"]["message"]
    assert "output" not in result
    model = store.get(MODEL_ID)
    assert model is not None
    assert model["status"] == "SUCCEEDED"
    assert model["schemaValidation"] == "FAILED"
    assert "outputAddress" not in model


def test_occupied_model_identity_is_rejected_before_agent_persistence_or_provider() -> None:
    store = InMemoryRuntimeObjectStore()
    store.create(
        {
            "kind": "ModelInvocation",
            "id": MODEL_ID,
            "agentInvocationId": "agentinvocation-ffffffffffff",
            "status": "RUNNING",
        },
        deterministic_key="occupied-model-identity",
    )
    adapter = FakeModelAdapter(
        [ModelResponse(output={"summary": "unused"}, usage=ModelUsage(1, 1), latency_ms=1)]
    )

    with pytest.raises(ValueError, match="already belongs to another invocation"):
        invoke(adapter=adapter, store=store)

    assert store.get(AGENT_ID) is None
    assert adapter.requests == []


def test_replay_cannot_change_the_claimed_model_invocation_identity() -> None:
    store = InMemoryRuntimeObjectStore()
    adapter = FakeModelAdapter(
        [ModelResponse(output={"summary": "done"}, usage=ModelUsage(1, 1), latency_ms=1)]
    )
    invoke(adapter=adapter, store=store)

    with pytest.raises(ValueError, match="already exists for different inputs"):
        invoke_agent(
            store=store,
            invocation_id=AGENT_ID,
            model_invocation_id="modelinvocation-ffffffffffff",
            resolved_agent=resolved_agent(),
            context_package=context_package(),
            prompt=prompt(),
            model_configuration=model_configuration(),
            adapter=adapter,
            started_at="2026-08-06T12:00:00Z",
            completed_at="2026-08-06T12:00:01Z",
        )

    assert len(adapter.requests) == 1


class BlockingModelAdapter(ModelAdapter):
    def __init__(self) -> None:
        self.entered = Event()
        self.release = Event()
        self.requests = []
        self._lock = Lock()

    def invoke(self, request):
        with self._lock:
            self.requests.append(request)
        self.entered.set()
        if not self.release.wait(timeout=2):
            raise AssertionError("test did not release provider")
        return ModelResponse(
            output={"summary": "claimed once"},
            usage=ModelUsage(2, 2),
            latency_ms=2,
        )


class NoCallTransport:
    def request(self, **_request):
        raise AssertionError("invalid configuration reached provider transport")


def invoke(*, adapter, store, logger=None):
    return invoke_agent(
        store=store,
        invocation_id=AGENT_ID,
        model_invocation_id=MODEL_ID,
        resolved_agent=resolved_agent(),
        context_package=context_package(),
        prompt=prompt(),
        model_configuration=model_configuration(),
        adapter=adapter,
        started_at="2026-08-06T12:00:00Z",
        completed_at="2026-08-06T12:00:01Z",
        lifecycle_logger=logger,
    )


def output_schema():
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["summary"],
        "properties": {"summary": {"type": "string"}},
    }


def resolved_agent():
    refs = [
        {"kind": "Task", "name": "analyze-issue", "version": "1.0.0"},
        {"kind": "Agent", "name": "issue-analyzer", "version": "1.0.0"},
        {"kind": "Prompt", "name": "issue-analysis", "version": "1.0.0"},
        {"kind": "Model", "name": "test-model", "version": "1.0.0"},
    ]
    return {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "ResolvedAgent",
        "id": "resolvedagent-123456789abc",
        "traceId": "trace-agent-invocation-123",
        "createdAt": "2026-08-06T11:59:58Z",
        "updatedAt": "2026-08-06T11:59:58Z",
        "provenance": {
            "actor": "agent-resolver",
            "workflowExecutionId": "workflowexecution-123456789abc",
            "taskExecutionId": "taskexecution-123456789abc",
            "resourceRefs": refs,
        },
        "taskExecutionId": "taskexecution-123456789abc",
        "agentRef": refs[1],
        "promptRef": refs[2],
        "modelRef": refs[3],
        "toolRefs": [],
        "policyRefs": [],
        "modelParameters": {"temperature": 0},
        "modelConfiguration": model_configuration().as_record(),
        "outputSchema": output_schema(),
        "resolvedAt": "2026-08-06T11:59:58Z",
    }


def context_package():
    return {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "ContextPackage",
        "id": "contextpackage-123456789abc",
        "traceId": "trace-agent-invocation-123",
        "createdAt": "2026-08-06T11:59:59Z",
        "updatedAt": "2026-08-06T11:59:59Z",
        "provenance": {
            "actor": "context-builder",
            "workflowExecutionId": "workflowexecution-123456789abc",
            "taskExecutionId": "taskexecution-123456789abc",
            "repositoryRevision": "abc1234",
            "resourceRefs": [{"kind": "Task", "name": "analyze-issue", "version": "1.0.0"}],
        },
        "taskExecutionId": "taskexecution-123456789abc",
        "taskRef": {"kind": "Task", "name": "analyze-issue", "version": "1.0.0"},
        "repositoryRevision": "abc1234",
        "elements": [
            {
                "type": "repository",
                "content": {"path": "README.md", "summary": "AEP contract"},
                "provenance": {"actor": "repository-knowledge-query", "resourceRefs": []},
            }
        ],
        "tokenBudget": 100,
        "tokenCount": 10,
        "tokenEstimate": {
            "algorithm": "test",
            "count": 10,
            "breakdown": {"task": {"elementCount": 1, "tokenCount": 10}},
        },
        "truncation": "NONE",
        "selection": {
            "requiredContext": ["repository"],
            "optionalContext": [],
            "selected": ["repository"],
            "discarded": [],
        },
    }


def prompt():
    return {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "Prompt",
        "metadata": {"name": "issue-analysis", "version": "1.0.0"},
        "spec": {"system": "Use only supplied context.", "formatting": "Return JSON."},
    }


def model_configuration():
    return ModelConfiguration(
        model_ref={"kind": "Model", "name": "test-model", "version": "1.0.0"},
        provider="fake",
        model="deterministic",
        parameters={"temperature": 0},
    )


def runtime_validator(name):
    paths = (
        ROOT / "schemas/resources/v1/resource-definitions.schema.json",
        ROOT / "schemas/runtime/v1/runtime-definitions.schema.json",
        ROOT / f"schemas/runtime/v1/{name}.schema.json",
    )
    schemas = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    registry = Registry().with_resources(
        (schema["$id"], SchemaResource.from_contents(schema, default_specification=DRAFT202012))
        for schema in schemas
    )
    return Draft202012Validator(schemas[-1], registry=registry)
