import json
from hashlib import sha256
from pathlib import Path
import traceback

import pytest

from aep.model_invocation import ModelConfiguration, ModelInvocationError, ModelRequest
from aep.openai_model_provider import (
    ModelProviderConfigurationError,
    OpenAIModelAdapter,
    OpenAIProviderConfig,
    ProviderHttpResponse,
    _provider_schema,
    openai_model_adapter_from_environment,
    verify_openai_model_provider_environment,
)


class ScriptedTransport:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def request(self, **request):
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def model_request(
    *,
    provider="openai",
    retry_policy=None,
    timeout_ms=5_000,
    token_limit=321,
):
    return ModelRequest(
        configuration=ModelConfiguration(
            model_ref={"kind": "Model", "name": "default-reasoning", "version": "1.0.0"},
            provider=provider,
            model="gpt-5",
            parameters={"temperature": 0.1},
            token_limit=token_limit,
            timeout_ms=timeout_ms,
            retry_policy=retry_policy or {"maxAttempts": 1},
        ),
        input={
            "prompt": {"system": "secret prompt body"},
            "contextPackage": {"elements": [{"content": "secret context body"}]},
            "outputSchema": {
                "type": "object",
                "required": ["answer"],
                "properties": {"answer": {"type": "integer"}},
                "additionalProperties": False,
            },
        },
        correlation={
            "traceId": "trace-live-model-1",
            "workflowExecutionId": "workflowexecution-123456789abc",
            "taskExecutionId": "taskexecution-123456789abc",
        },
    )


def response(status, body, *, headers=None):
    return ProviderHttpResponse(
        status=status,
        headers=headers or {},
        body=json.dumps(body).encode(),
    )


def success(*, output='{"answer":42}', request_id="resp_123", model="gpt-5"):
    return response(
        200,
        {
            "id": request_id,
            "model": model,
            "status": "completed",
            "usage": {"input_tokens": 17, "output_tokens": 5},
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": output}],
                }
            ],
        },
    )


def adapter(transport):
    return OpenAIModelAdapter(
        OpenAIProviderConfig(api_key="sk-runtime-secret"),
        transport=transport,
        sleeper=lambda _: None,
    )


def normalized_id(value):
    return "redacted:sha256:" + sha256(value.encode()).hexdigest()


def test_structured_success_preserves_exact_model_bounds_and_usage_evidence():
    transport = ScriptedTransport([success()])

    result = adapter(transport).invoke(model_request())

    assert result.output == {"answer": 42}
    assert result.usage.as_record() == {"input": 17, "output": 5}
    assert result.provider_metadata == {
        "provider": "openai",
        "model": "gpt-5",
        "requestId": normalized_id("resp_123"),
        "finishReason": "completed",
        "attemptCount": 1,
        "attempts": [
            {
                "attempt": 1,
                "code": "succeeded",
                "requestId": normalized_id("resp_123"),
            }
        ],
    }
    sent = json.loads(transport.requests[0]["body"])
    assert sent["model"] == "gpt-5"
    assert sent["temperature"] == 0.1
    assert sent["max_output_tokens"] == 321
    assert sent["text"]["format"]["schema"] == model_request().input["outputSchema"]
    assert transport.requests[0]["timeout_ms"] <= 5_000


def test_transient_and_rate_limit_failures_retry_only_to_resource_bound():
    transport = ScriptedTransport(
        [
            response(503, {"error": {"message": "unsafe provider detail"}}),
            response(429, {"error": {}}, headers={"Retry-After": "0"}),
            success(),
        ]
    )
    request = model_request(retry_policy={"maxAttempts": 3, "backoffMs": 1})

    result = adapter(transport).invoke(request)

    assert result.output == {"answer": 42}
    assert result.provider_metadata["attemptCount"] == 3
    assert [attempt["code"] for attempt in result.provider_metadata["attempts"]] == [
        "provider_unavailable",
        "rate_limit",
        "succeeded",
    ]
    assert len(transport.requests) == 3


def test_timeout_is_recoverable_and_stops_at_max_attempts():
    transport = ScriptedTransport([TimeoutError("secret timeout"), TimeoutError("again")])

    with pytest.raises(ModelInvocationError) as raised:
        adapter(transport).invoke(
            model_request(retry_policy={"maxAttempts": 2, "backoffMs": 0})
        )

    assert raised.value.code == "timeout"
    assert raised.value.recoverable is True
    assert raised.value.provider_metadata["attemptCount"] == 2
    assert len(transport.requests) == 2


def test_rate_limit_exhaustion_has_explicit_safe_classification():
    transport = ScriptedTransport(
        [response(429, {"error": {"message": "secret body"}}, headers={"X-Request-ID": "req_rate"})]
    )

    with pytest.raises(ModelInvocationError) as raised:
        adapter(transport).invoke(model_request())

    assert raised.value.code == "rate_limit"
    assert raised.value.recoverable is True
    assert raised.value.provider_metadata["requestId"] == normalized_id("req_rate")


def test_safety_refusal_is_permanent_and_contains_no_output_body():
    refusal = response(
        200,
        {
            "id": "resp_refusal",
            "model": "gpt-5",
            "status": "completed",
            "usage": {"input_tokens": 3, "output_tokens": 0},
            "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "secret reason"}]}],
        },
    )

    with pytest.raises(ModelInvocationError) as raised:
        adapter(ScriptedTransport([refusal])).invoke(model_request())

    assert raised.value.code == "safety_refusal"
    assert raised.value.recoverable is False
    assert "secret reason" not in str(raised.value)


@pytest.mark.parametrize(
    "provider_response",
    [
        ProviderHttpResponse(200, {}, b"not-json secret output"),
        success(output="not-json secret output"),
        success(model="different-model"),
    ],
)
def test_malformed_or_mismatched_success_is_permanent(provider_response):
    with pytest.raises(ModelInvocationError) as raised:
        adapter(ScriptedTransport([provider_response])).invoke(model_request())

    assert raised.value.recoverable is False
    assert raised.value.code in {"malformed_response", "model_identity_mismatch"}
    assert "secret output" not in str(raised.value)


def test_unsupported_provider_and_missing_credentials_fail_fast(tmp_path: Path):
    with pytest.raises(ModelProviderConfigurationError, match="unsupported"):
        openai_model_adapter_from_environment("anthropic", environ={})
    with pytest.raises(ModelProviderConfigurationError, match="required"):
        openai_model_adapter_from_environment("openai", environ={})
    with pytest.raises(ModelInvocationError) as raised:
        adapter(ScriptedTransport([success()])).invoke(model_request(provider="local"))
    assert raised.value.code == "invalid_configuration"
    assert raised.value.recoverable is False


def test_environment_factory_reads_secret_file_but_readiness_and_verification_do_not_expose_it(
    tmp_path: Path,
):
    secret = "sk-file-secret-value"
    secret_file = tmp_path / "openai-key.txt"
    secret_file.write_text(secret, encoding="utf-8")
    values = {"AEP_OPENAI_API_KEY_FILE": str(secret_file)}

    provider = openai_model_adapter_from_environment(
        "openai", environ=values, transport=ScriptedTransport([success()])
    )

    assert secret not in repr(provider.readiness())
    assert str(secret_file) not in repr(provider.readiness())
    assert secret not in repr(provider._config)
    verification = verify_openai_model_provider_environment("openai", environ={})
    assert verification["status"] == "CONFIGURATION_VALID"


def test_transport_and_provider_error_details_are_redacted_from_exception_chain():
    secret = "sk-runtime-secret"
    for outcome in (
        RuntimeError(f"transport leaked {secret} and prompt/context/output bodies"),
        response(400, {"error": {"message": f"provider leaked {secret}"}}),
    ):
        with pytest.raises(ModelInvocationError) as raised:
            adapter(ScriptedTransport([outcome])).invoke(model_request())
        rendered = "".join(
            traceback.format_exception(
                type(raised.value), raised.value, raised.value.__traceback__
            )
        )
        assert secret not in rendered
        assert "secret prompt body" not in rendered
        assert "secret context body" not in rendered
        assert raised.value.__cause__ is None
        assert raised.value.__context__ is None


def test_hostile_response_and_header_ids_are_normalized_before_evidence():
    hostile_body_id = "resp_secret prompt/context/output"
    result = adapter(ScriptedTransport([success(request_id=hostile_body_id)])).invoke(
        model_request()
    )

    request_id = result.provider_metadata["requestId"]
    assert request_id.startswith("redacted:sha256:")
    assert hostile_body_id not in repr(result.provider_metadata)
    assert result.provider_metadata["attempts"][0]["requestId"] == request_id

    hostile_header_id = "req_secret prompt/context/output"
    with pytest.raises(ModelInvocationError) as raised:
        adapter(
            ScriptedTransport(
                [response(400, {}, headers={"X-Request-ID": hostile_header_id})]
            )
        ).invoke(model_request())
    metadata = raised.value.provider_metadata
    assert metadata["requestId"].startswith("redacted:sha256:")
    assert hostile_header_id not in repr(metadata)
    assert metadata["attempts"][0]["requestId"] == metadata["requestId"]


def test_every_self_hosting_agent_schema_projects_to_supported_provider_subset():
    root = Path(__file__).parents[1]
    for path in sorted((root / ".ai" / "agents").glob("*.yaml")):
        agent = json.loads(path.read_text(encoding="utf-8"))
        declared = agent["spec"]["outputSchema"]

        projected = _provider_schema(declared)

        rendered = json.dumps(projected)
        assert "minLength" not in rendered
        assert "maxLength" not in rendered
        assert "uniqueItems" not in rendered
        assert projected["type"] == "object"
        assert projected["additionalProperties"] is False
        assert set(projected["required"]) == set(projected["properties"])
