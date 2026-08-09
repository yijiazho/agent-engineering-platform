"""Live OpenAI Responses API adapter for the provider-neutral model boundary.

Only the assembled ModelRequest crosses this boundary. The adapter has no Tool,
filesystem, Git, GitHub, scheduling, or repository-knowledge dependencies.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from hashlib import sha256
import json
import os
from pathlib import Path
import time
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from aep.model_invocation import (
    ModelAdapter,
    ModelErrorClass,
    ModelInvocationError,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)


_DEFAULT_API_URL = "https://api.openai.com/v1"
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_RESERVED_PARAMETERS = frozenset({"model", "input", "max_output_tokens", "text"})


class ModelProviderConfigurationError(ValueError):
    """Safe fail-fast error for unsupported or incomplete live configuration."""


@dataclass(frozen=True)
class ProviderHttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class ProviderHttpTransport(Protocol):
    def request(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout_ms: int,
    ) -> ProviderHttpResponse: ...


class UrllibProviderTransport:
    """Small dependency-free HTTPS transport with a bounded response body."""

    def request(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout_ms: int,
    ) -> ProviderHttpResponse:
        request = Request(url, data=body, headers=dict(headers), method="POST")
        try:
            with urlopen(request, timeout=max(0.001, timeout_ms / 1000)) as response:
                payload = response.read(_MAX_RESPONSE_BYTES + 1)
                if len(payload) > _MAX_RESPONSE_BYTES:
                    raise ValueError("provider response exceeded size limit")
                return ProviderHttpResponse(
                    status=int(response.status),
                    headers=dict(response.headers.items()),
                    body=payload,
                )
        except HTTPError as error:
            payload = error.read(_MAX_RESPONSE_BYTES + 1)
            if len(payload) > _MAX_RESPONSE_BYTES:
                payload = b""
            return ProviderHttpResponse(
                status=int(error.code),
                headers=dict(error.headers.items()) if error.headers else {},
                body=payload,
            )
        except (TimeoutError, URLError) as error:
            if isinstance(error, URLError) and not isinstance(error.reason, TimeoutError):
                raise ConnectionError("provider transport failed") from None
            raise TimeoutError("provider request timed out") from None


@dataclass(frozen=True)
class OpenAIProviderConfig:
    api_key: str = field(repr=False)
    api_url: str = _DEFAULT_API_URL

    def __post_init__(self) -> None:
        if not self.api_key.strip():
            raise ModelProviderConfigurationError("OpenAI credential is missing")
        parsed = urlsplit(self.api_url)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ModelProviderConfigurationError("OpenAI API URL must be a clean HTTPS URL")


class OpenAIModelAdapter(ModelAdapter):
    """Bounded structured-output adapter for the OpenAI Responses API."""

    def __init__(
        self,
        config: OpenAIProviderConfig,
        *,
        transport: ProviderHttpTransport | None = None,
        monotonic_clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config
        self._transport = transport or UrllibProviderTransport()
        self._monotonic = monotonic_clock
        self._sleep = sleeper

    def readiness(self) -> dict[str, Any]:
        """Return a credential-free local configuration diagnostic."""

        return {"status": "READY", "provider": "openai", "apiUrl": self._config.api_url}

    def invoke(self, request: ModelRequest) -> ModelResponse:
        configuration = request.configuration
        if configuration.provider != "openai":
            raise ModelInvocationError(
                "model provider configuration is unsupported",
                classification=ModelErrorClass.PERMANENT,
                code="invalid_configuration",
            )
        collisions = _RESERVED_PARAMETERS.intersection(configuration.parameters)
        if collisions:
            raise ModelInvocationError(
                "model provider configuration overrides bounded fields",
                classification=ModelErrorClass.PERMANENT,
                code="invalid_configuration",
            )

        timeout_ms = configuration.timeout_ms or 60_000
        deadline = self._monotonic() + timeout_ms / 1000
        payload: dict[str, Any] = {
            "model": configuration.model,
            "input": json.dumps(
                dict(request.input),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ),
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "aep_structured_output",
                    "schema": _provider_schema(request.input["outputSchema"]),
                    "strict": True,
                }
            },
            **dict(configuration.parameters),
        }
        if configuration.token_limit is not None:
            payload["max_output_tokens"] = configuration.token_limit
        encoded = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8")
        attempts: list[dict[str, Any]] = []
        started = self._monotonic()

        for attempt in range(1, configuration.max_attempts + 1):
            remaining_ms = _remaining_ms(deadline, self._monotonic())
            try:
                response = self._transport.request(
                    url=self._config.api_url.rstrip("/") + "/responses",
                    headers={
                        "Authorization": f"Bearer {self._config.api_key}",
                        "Content-Type": "application/json",
                    },
                    body=encoded,
                    timeout_ms=remaining_ms,
                )
            except TimeoutError:
                failure = _failure("timeout", recoverable=True)
                response = None
            except Exception:
                failure = _failure("provider_unavailable", recoverable=True)
                response = None
            else:
                failure = _http_failure(response)
                if failure is None:
                    try:
                        return _successful_response(
                            response,
                            configuration.model,
                            round((self._monotonic() - started) * 1000),
                            attempt,
                            attempts,
                        )
                    except ModelInvocationError as error:
                        failure = error

            request_id = _request_id(response.headers) if response is not None else None
            if request_id is None:
                candidate = failure.provider_metadata.get("requestId")
                request_id = candidate if isinstance(candidate, str) else None
            attempts.append(
                {
                    "attempt": attempt,
                    "code": failure.code,
                    "requestId": request_id,
                }
            )
            if not failure.recoverable or attempt == configuration.max_attempts:
                raise ModelInvocationError(
                    str(failure),
                    classification=failure.classification,
                    code=failure.code,
                    provider_metadata={
                        **dict(failure.provider_metadata),
                        "provider": "openai",
                        "model": configuration.model,
                        "requestId": request_id,
                        "attemptCount": attempt,
                        "attempts": attempts,
                    },
                ) from None
            delay_ms = max(
                configuration.backoff_ms,
                _retry_after_ms(response.headers) if response is not None else 0,
            )
            if self._monotonic() + delay_ms / 1000 >= deadline:
                raise ModelInvocationError(
                    "model provider request timed out",
                    classification=ModelErrorClass.RECOVERABLE,
                    code="timeout",
                    provider_metadata={
                        "provider": "openai",
                        "model": configuration.model,
                        "requestId": request_id,
                        "attemptCount": attempt,
                        "attempts": attempts,
                    },
                ) from None
            if delay_ms:
                self._sleep(delay_ms / 1000)

        raise AssertionError("unreachable provider retry state")


def openai_model_adapter_from_environment(
    provider: str,
    *,
    environ: Mapping[str, str] | None = None,
    transport: ProviderHttpTransport | None = None,
) -> OpenAIModelAdapter:
    """Select and configure the live adapter without putting secrets in Resources."""

    if provider != "openai":
        raise ModelProviderConfigurationError(f"unsupported Model provider {provider!r}")
    values = os.environ if environ is None else environ
    secret_file = values.get("AEP_OPENAI_API_KEY_FILE", "").strip()
    if not secret_file:
        raise ModelProviderConfigurationError("AEP_OPENAI_API_KEY_FILE is required")
    try:
        api_key = Path(secret_file).read_text(encoding="utf-8").strip()
    except OSError:
        raise ModelProviderConfigurationError("OpenAI credential file is unavailable") from None
    config = OpenAIProviderConfig(
        api_key=api_key,
        api_url=values.get("AEP_OPENAI_API_URL", _DEFAULT_API_URL).strip(),
    )
    return OpenAIModelAdapter(config, transport=transport)


def verify_openai_model_provider_environment(
    provider: str, *, environ: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Verify selection and endpoint without reading or requiring credentials."""

    if provider != "openai":
        raise ModelProviderConfigurationError(f"unsupported Model provider {provider!r}")
    values = os.environ if environ is None else environ
    api_url = values.get("AEP_OPENAI_API_URL", _DEFAULT_API_URL).strip()
    # A disposable marker validates only endpoint syntax; it is never returned.
    OpenAIProviderConfig(api_key="verification-only", api_url=api_url)
    return {"status": "CONFIGURATION_VALID", "provider": "openai", "apiUrl": api_url}


def _successful_response(
    response: ProviderHttpResponse,
    requested_model: str,
    latency_ms: int,
    attempt_count: int,
    prior_attempts: list[dict[str, Any]],
) -> ModelResponse:
    try:
        document = json.loads(response.body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _malformed() from None
    if not isinstance(document, Mapping):
        raise _malformed()
    request_id = _safe_provider_id(document.get("id")) or _request_id(response.headers)
    actual_model = document.get("model")
    if actual_model != requested_model:
        raise ModelInvocationError(
            "model provider returned a different model identity",
            classification=ModelErrorClass.PERMANENT,
            code="model_identity_mismatch",
            provider_metadata={"provider": "openai", "model": requested_model, "requestId": request_id},
        )
    status = document.get("status")
    refusal = _refusal(document)
    if refusal:
        raise ModelInvocationError(
            "model provider refused the structured request",
            classification=ModelErrorClass.PERMANENT,
            code="safety_refusal",
            provider_metadata={"provider": "openai", "model": requested_model, "requestId": request_id, "finishReason": "refusal"},
        )
    if status == "failed":
        raise ModelInvocationError(
            "model provider failed the structured request",
            classification=ModelErrorClass.PERMANENT,
            code="provider_error",
            provider_metadata={"provider": "openai", "model": requested_model, "requestId": request_id, "finishReason": "failed"},
        )
    if status != "completed":
        raise ModelInvocationError(
            "model provider returned an incomplete response",
            classification=ModelErrorClass.RECOVERABLE,
            code="incomplete_response",
            provider_metadata={"provider": "openai", "model": requested_model, "requestId": request_id, "finishReason": "incomplete"},
        )
    output_text = _output_text(document)
    try:
        output = json.loads(output_text)
    except (TypeError, json.JSONDecodeError):
        raise _malformed(request_id=request_id, model=requested_model) from None
    usage = document.get("usage")
    if not isinstance(usage, Mapping):
        raise _malformed(request_id=request_id, model=requested_model)
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if not _non_negative_int(input_tokens) or not _non_negative_int(output_tokens):
        raise _malformed(request_id=request_id, model=requested_model)
    return ModelResponse(
        output=output,
        usage=ModelUsage(input_tokens, output_tokens),
        latency_ms=max(0, latency_ms),
        provider_metadata={
            "provider": "openai",
            "model": requested_model,
            "requestId": request_id,
            "finishReason": "completed",
            "attemptCount": attempt_count,
            "attempts": [
                *prior_attempts,
                {"attempt": attempt_count, "code": "succeeded", "requestId": request_id},
            ],
        },
    )


def _output_text(document: Mapping[str, Any]) -> str:
    output = document.get("output")
    if not isinstance(output, list):
        raise _malformed()
    for item in output:
        if not isinstance(item, Mapping) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, Mapping) and part.get("type") == "output_text" and isinstance(part.get("text"), str):
                return part["text"]
    raise _malformed()


def _refusal(document: Mapping[str, Any]) -> bool:
    output = document.get("output")
    if not isinstance(output, list):
        return False
    return any(
        isinstance(part, Mapping) and part.get("type") == "refusal"
        for item in output
        if isinstance(item, Mapping) and isinstance(item.get("content"), list)
        for part in item["content"]
    )


def _http_failure(response: ProviderHttpResponse) -> ModelInvocationError | None:
    if 200 <= response.status < 300:
        return None
    if response.status == 429:
        return _failure("rate_limit", recoverable=True)
    if response.status in {408, 409} or response.status >= 500:
        return _failure("provider_unavailable", recoverable=True)
    if response.status in {401, 403}:
        return _failure("authentication", recoverable=False)
    return _failure("provider_error", recoverable=False)


def _failure(code: str, *, recoverable: bool) -> ModelInvocationError:
    messages = {
        "timeout": "model provider request timed out",
        "rate_limit": "model provider rate limit exceeded",
        "provider_unavailable": "model provider is unavailable",
        "authentication": "model provider authentication failed",
        "provider_error": "model provider rejected the request",
    }
    return ModelInvocationError(
        messages[code],
        classification=ModelErrorClass.RECOVERABLE if recoverable else ModelErrorClass.PERMANENT,
        code=code,
    )


def _malformed(*, request_id: str | None = None, model: str | None = None) -> ModelInvocationError:
    metadata = {"provider": "openai", "requestId": request_id}
    if model is not None:
        metadata["model"] = model
    return ModelInvocationError(
        "model provider returned a malformed structured response",
        classification=ModelErrorClass.PERMANENT,
        code="malformed_response",
        provider_metadata=metadata,
    )


def _request_id(headers: Mapping[str, str]) -> str | None:
    for key, value in headers.items():
        if key.casefold() in {"x-request-id", "request-id"} and isinstance(value, str):
            return _safe_provider_id(value)
    return None


def _safe_provider_id(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    digest = sha256(value.encode("utf-8", errors="replace")).hexdigest()
    return f"redacted:sha256:{digest}"


def _retry_after_ms(headers: Mapping[str, str]) -> int:
    for key, value in headers.items():
        if key.casefold() == "retry-after":
            try:
                return max(0, min(60_000, round(float(value) * 1000)))
            except (TypeError, ValueError):
                return 0
    return 0


def _remaining_ms(deadline: float, now: float) -> int:
    remaining = round((deadline - now) * 1000)
    if remaining < 1:
        raise ModelInvocationError(
            "model provider request timed out",
            classification=ModelErrorClass.RECOVERABLE,
            code="timeout",
        )
    return remaining


def _non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _provider_schema(value: Any) -> Any:
    """Project an AEP schema to the provider-supported structural subset.

    The coordinator still validates the returned value against the complete,
    immutable ResolvedAgent schema. Removing unsupported generation hints here
    never weakens AEP's publication contract.
    """

    if isinstance(value, Mapping):
        unsupported = {"minLength", "maxLength", "uniqueItems"}
        return {
            str(key): _provider_schema(item)
            for key, item in value.items()
            if key not in unsupported
        }
    if isinstance(value, (list, tuple)):
        return [_provider_schema(item) for item in value]
    return value
