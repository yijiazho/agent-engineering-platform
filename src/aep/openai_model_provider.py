"""Live OpenAI Responses API adapter for the provider-neutral model boundary.

Only the assembled ModelRequest crosses this boundary. The adapter has no Tool,
filesystem, Git, GitHub, scheduling, or repository-knowledge dependencies.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from math import isfinite
import os
from pathlib import Path
import re
from threading import Event, Lock, Thread
import time
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from aep.model_invocation import (
    ModelAdapter,
    ModelErrorClass,
    ModelInvocationError,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)
from aep.model_rate_limits import (
    CoordinatorStateError,
    ProcessLocalModelAdmissionCoordinator,
)
from aep.provider_schema import StrictProviderSchemaError, validate_openai_strict_schema


_DEFAULT_API_URL = "https://api.openai.com/v1"
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024
_ALLOWED_PARAMETERS = frozenset({"temperature", "top_p"})
_PROVIDER_MODEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_UNSUPPORTED_SCHEMA_KEYWORDS = frozenset(
    {"minLength", "maxLength", "uniqueItems"}
)
_SCHEMA_MAP_KEYWORDS = frozenset(
    {"$defs", "definitions", "dependentSchemas", "patternProperties", "properties"}
)
_MAX_BACKOFF_MS = 60_000


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
    """Deadline-bound HTTPS transport that never follows redirects."""

    def __init__(self) -> None:
        self._opener = build_opener(_NoRedirectHandler())

    def request(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout_ms: int,
    ) -> ProviderHttpResponse:
        if timeout_ms < 1:
            raise TimeoutError("provider request timed out")
        request = Request(url, data=body, headers=dict(headers), method="POST")
        completed = Event()
        cancelled = Event()
        active_response: list[Any] = []
        outcome: list[tuple[str, ProviderHttpResponse | None]] = []

        def run() -> None:
            try:
                result = self._request_sync(
                    request,
                    timeout_ms=timeout_ms,
                    cancelled=cancelled,
                    active_response=active_response,
                )
            except TimeoutError:
                outcome.append(("timeout", None))
            except Exception:
                outcome.append(("failure", None))
            else:
                outcome.append(("response", result))
            finally:
                completed.set()

        worker = Thread(target=run, name="aep-openai-http", daemon=True)
        worker.start()
        if not completed.wait(timeout_ms / 1000):
            cancelled.set()
            if active_response:
                try:
                    active_response[0].close()
                except Exception:
                    pass
            raise TimeoutError("provider request timed out")

        kind, result = outcome[0]
        if kind == "timeout":
            raise TimeoutError("provider request timed out")
        if kind == "failure" or result is None:
            raise ConnectionError("provider transport failed")
        return result

    def _request_sync(
        self,
        request: Request,
        *,
        timeout_ms: int,
        cancelled: Event,
        active_response: list[Any],
    ) -> ProviderHttpResponse:
        try:
            with self._opener.open(
                request, timeout=max(0.001, timeout_ms / 1000)
            ) as response:
                active_response.append(response)
                payload = _read_bounded(response, cancelled)
                return ProviderHttpResponse(
                    status=int(response.status),
                    headers=dict(response.headers.items()),
                    body=payload,
                )
        except HTTPError as error:
            with error:
                active_response.append(error)
                payload = _read_bounded(error, cancelled)
                return ProviderHttpResponse(
                    status=int(error.code),
                    headers=dict(error.headers.items()) if error.headers else {},
                    body=payload,
                )
        except (TimeoutError, URLError) as error:
            if isinstance(error, URLError) and not isinstance(error.reason, TimeoutError):
                raise ConnectionError("provider transport failed") from None
            raise TimeoutError("provider request timed out") from None


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _read_bounded(response: Any, cancelled: Event) -> bytes:
    chunks: list[bytes] = []
    size = 0
    read = getattr(response, "read1", None) or response.read
    while True:
        if cancelled.is_set():
            raise TimeoutError("provider request timed out")
        chunk = read(min(_READ_CHUNK_BYTES, _MAX_RESPONSE_BYTES + 1 - size))
        if cancelled.is_set():
            raise TimeoutError("provider request timed out")
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        size += len(chunk)
        if size > _MAX_RESPONSE_BYTES:
            raise ValueError("provider response exceeded size limit")


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
        wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        jitter: Callable[[], float] = lambda: 0.5,
        coordinator: ProcessLocalModelAdmissionCoordinator | None = None,
        coordinator_state_root: Path | None = None,
        wall_time: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._transport = transport or UrllibProviderTransport()
        self._monotonic = monotonic_clock
        self._sleep = sleeper
        self._wall_clock = wall_clock
        self._jitter = jitter
        self._coordinator = coordinator
        self._coordinator_state_root = coordinator_state_root
        self._wall_time = wall_time
        self._coordinators: dict[
            tuple[str, int, int], ProcessLocalModelAdmissionCoordinator
        ] = {}
        self._coordinators_lock = Lock()

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
        unsupported_parameters = set(configuration.parameters) - _ALLOWED_PARAMETERS
        if unsupported_parameters:
            raise ModelInvocationError(
                "model provider configuration contains unsupported parameters",
                classification=ModelErrorClass.PERMANENT,
                code="invalid_configuration",
            )
        if configuration.timeout_ms is None:
            raise ModelInvocationError(
                "OpenAI model provider configuration requires timeoutMs",
                classification=ModelErrorClass.PERMANENT,
                code="invalid_configuration",
            )

        try:
            validate_openai_strict_schema(request.input["outputSchema"])
        except (KeyError, StrictProviderSchemaError) as error:
            path = error.path if isinstance(error, StrictProviderSchemaError) else "$.outputSchema"
            metadata: dict[str, Any] = {
                "provider": "openai",
                "requestedModel": configuration.model,
                "schemaPath": path,
                "attemptCount": 0,
                "attempts": [],
                "retryDecision": "suppressed",
                "quotaReserved": False,
            }
            if isinstance(error, StrictProviderSchemaError) and error.names:
                metadata["schemaNames"] = list(error.names)
            raise ModelInvocationError(
                "model output schema is incompatible with the provider strict schema contract",
                classification=ModelErrorClass.PERMANENT,
                code="invalid_response_schema",
                provider_metadata=metadata,
            ) from None

        timeout_ms = configuration.timeout_ms
        deadline = self._monotonic() + timeout_ms / 1000
        try:
            instructions, provider_input = _provider_input(request.input)
            payload: dict[str, Any] = {
                "model": configuration.model,
                "instructions": instructions,
                "input": json.dumps(
                    provider_input,
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
            encoded = json.dumps(
                payload, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
        except (KeyError, RecursionError, TypeError, ValueError):
            raise ModelInvocationError(
                "model provider configuration cannot be serialized safely",
                classification=ModelErrorClass.PERMANENT,
                code="invalid_configuration",
            ) from None
        attempts: list[dict[str, Any]] = []
        started = self._monotonic()
        estimated_input_tokens = max(1, (len(encoded) + 3) // 4)
        requests_per_minute = int(
            configuration.rate_limit_policy["requestsPerMinute"]
        )
        tokens_per_minute = int(configuration.rate_limit_policy["tokensPerMinute"])
        coordinator = self._coordinator
        if coordinator is None:
            key = (configuration.model, requests_per_minute, tokens_per_minute)
            with self._coordinators_lock:
                coordinator = self._coordinators.setdefault(
                    key,
                    ProcessLocalModelAdmissionCoordinator(
                        requests_per_minute=requests_per_minute,
                        tokens_per_minute=tokens_per_minute,
                        state_path=(
                            self._coordinator_state_root
                            / (
                                sha256(
                                    (
                                        f"{configuration.model}:{requests_per_minute}:"
                                        f"{tokens_per_minute}"
                                    ).encode("utf-8")
                                ).hexdigest()
                                + ".json"
                            )
                            if self._coordinator_state_root is not None
                            else None
                        ),
                        wall_clock=self._wall_time,
                    ),
                )

        for attempt in range(1, configuration.max_attempts + 1):
            admitted_at = self._monotonic()
            try:
                admission = coordinator.admit(
                    now=admitted_at,
                    deadline=deadline,
                    estimated_input_tokens=estimated_input_tokens,
                    output_token_allowance=configuration.token_limit or 0,
                )
            except CoordinatorStateError:
                raise _coordinator_unavailable(configuration.model, attempts) from None
            admission_evidence = {
                "estimatedInputTokens": admission.estimated_input_tokens,
                "outputTokenAllowance": configuration.token_limit or 0,
                "reservedTokens": admission.reserved_tokens,
                "coordinatorDelayMs": admission.delay_ms,
            }
            if not admission.admitted:
                eligible = _future_timestamp(self._wall_clock(), admission.delay_ms)
                raise ModelInvocationError(
                    "model provider retry is deferred beyond the invocation deadline",
                    classification=ModelErrorClass.RECOVERABLE,
                    code="rate_limit_deferred",
                    provider_metadata={
                        "provider": "openai",
                        "requestedModel": configuration.model,
                        "attemptCount": len(attempts),
                        "attempts": attempts,
                        **admission_evidence,
                        "delaySource": "coordinator",
                        "retryEligibleAt": eligible,
                        "retryDecision": "deferred",
                    },
                ) from None
            if admission.delay_ms:
                self._sleep(admission.delay_ms / 1000)
            while True:
                rechecked_at = self._monotonic()
                try:
                    recheck = coordinator.revalidate(
                        now=rechecked_at, deadline=deadline
                    )
                except CoordinatorStateError:
                    raise _coordinator_unavailable(
                        configuration.model, attempts
                    ) from None
                if not recheck.admitted:
                    additional_delay = recheck.delay_ms
                    eligible = _future_timestamp(
                        self._wall_clock(), additional_delay
                    )
                    admission_evidence["coordinatorDelayMs"] += additional_delay
                    raise ModelInvocationError(
                        "model provider retry is deferred beyond the invocation deadline",
                        classification=ModelErrorClass.RECOVERABLE,
                        code="rate_limit_deferred",
                        provider_metadata={
                            "provider": "openai",
                            "requestedModel": configuration.model,
                            "attemptCount": len(attempts),
                            "attempts": attempts,
                            **admission_evidence,
                            "delaySource": "coordinator-recheck",
                            "retryEligibleAt": eligible,
                            "retryDecision": "deferred",
                        },
                    ) from None
                if not recheck.delay_ms:
                    break
                admission_evidence["coordinatorDelayMs"] += recheck.delay_ms
                self._sleep(recheck.delay_ms / 1000)
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
                            admission_evidence,
                            coordinator,
                            admission.reserved_tokens,
                            admission.reservation_id,
                            self._monotonic(),
                        )
                    except ModelInvocationError as error:
                        failure = error

            request_id = _request_id(response.headers) if response is not None else None
            if request_id is None:
                candidate = failure.provider_metadata.get("requestId")
                request_id = candidate if isinstance(candidate, str) else None
            http_evidence = _safe_rate_limit_evidence(response)
            attempt_evidence = {
                "attempt": attempt,
                "code": failure.code,
                "requestId": request_id,
                **admission_evidence,
                **http_evidence,
            }
            attempts.append(attempt_evidence)
            if not failure.recoverable:
                attempt_evidence["retryDecision"] = "suppressed"
                raise ModelInvocationError(
                    str(failure),
                    classification=failure.classification,
                    code=failure.code,
                    provider_metadata={
                        **dict(failure.provider_metadata),
                        "provider": "openai",
                        "requestedModel": configuration.model,
                        "requestId": request_id,
                        "attemptCount": attempt,
                        "attempts": attempts,
                        **http_evidence,
                        **admission_evidence,
                        "retryDecision": "suppressed",
                    },
                ) from None
            retry_after_ms = http_evidence.get("retryAfterMs", 0)
            reset_ms = max(
                int(http_evidence.get("resetRequestsMs", 0)),
                int(http_evidence.get("resetTokensMs", 0)),
            )
            backoff_ms = _backoff_ms(
                configuration.backoff_ms, attempt, self._jitter()
            )
            delay_ms = max(backoff_ms, retry_after_ms, reset_ms)
            delay_source = (
                "retry-after"
                if retry_after_ms >= max(backoff_ms, reset_ms) and retry_after_ms
                else "provider-reset"
                if reset_ms >= backoff_ms and reset_ms
                else "exponential-backoff"
            )
            retry_eligible_at = _future_timestamp(self._wall_clock(), delay_ms)
            decision = (
                "deferred"
                if attempt == configuration.max_attempts
                else "scheduled"
            )
            attempt_evidence.update(
                {
                    "appliedDelayMs": delay_ms,
                    "delaySource": delay_source,
                    "retryEligibleAt": retry_eligible_at,
                    "retryDecision": decision,
                }
            )
            throttle_observed_at = self._monotonic()
            try:
                coordinator.observe_throttle(
                    now=throttle_observed_at,
                    eligible_at=throttle_observed_at + delay_ms / 1000,
                )
            except CoordinatorStateError:
                raise _coordinator_unavailable(
                    configuration.model, attempts
                ) from None
            if attempt == configuration.max_attempts:
                raise ModelInvocationError(
                    str(failure),
                    classification=failure.classification,
                    code=failure.code,
                    provider_metadata={
                        **dict(failure.provider_metadata),
                        "provider": "openai",
                        "requestedModel": configuration.model,
                        "requestId": request_id,
                        "attemptCount": attempt,
                        "attempts": attempts,
                        **http_evidence,
                        **admission_evidence,
                        "appliedDelayMs": delay_ms,
                        "delaySource": delay_source,
                        "retryEligibleAt": retry_eligible_at,
                        "retryDecision": "deferred",
                    },
                ) from None
            if throttle_observed_at + delay_ms / 1000 >= deadline:
                raise ModelInvocationError(
                    "model provider retry is deferred beyond the invocation deadline",
                    classification=ModelErrorClass.RECOVERABLE,
                    code="rate_limit_deferred" if failure.code == "rate_limit" else "timeout",
                    provider_metadata={
                        "provider": "openai",
                        "requestedModel": configuration.model,
                        "requestId": request_id,
                        "attemptCount": attempt,
                        "attempts": attempts,
                        **http_evidence,
                        **admission_evidence,
                        "appliedDelayMs": delay_ms,
                        "delaySource": delay_source,
                        "retryEligibleAt": retry_eligible_at,
                        "retryDecision": "deferred",
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
    replicas = values.get("AEP_MODEL_WORKER_REPLICAS", "1").strip()
    if replicas != "1":
        raise ModelProviderConfigurationError(
            "process-local Model rate-limit coordination requires exactly one worker replica"
        )
    state_root = values.get("AEP_STATE_ROOT", "").strip()
    if not state_root:
        raise ModelProviderConfigurationError(
            "AEP_STATE_ROOT is required for durable Model rate-limit coordination"
        )
    return OpenAIModelAdapter(
        config,
        transport=transport,
        coordinator_state_root=(
            Path(state_root)
            / "model-rate-limits"
            / sha256(
                (config.api_url + "\0" + api_key).encode(
                    "utf-8", errors="replace"
                )
            ).hexdigest()
        ),
    )


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
    admission_evidence: Mapping[str, Any],
    coordinator: ProcessLocalModelAdmissionCoordinator,
    reserved_tokens: int,
    reservation_id: int | None,
    observed_at: float,
) -> ModelResponse:
    try:
        document = json.loads(response.body)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise _malformed() from None
    if not isinstance(document, Mapping):
        raise _malformed()
    request_id = _safe_provider_id(document.get("id")) or _request_id(response.headers)
    provider_model = document.get("model")
    if not isinstance(provider_model, str) or not _PROVIDER_MODEL_PATTERN.fullmatch(
        provider_model
    ):
        raise _malformed(request_id=request_id, model=requested_model)
    status = document.get("status")
    refusal = _refusal(document)
    if refusal:
        raise ModelInvocationError(
            "model provider refused the structured request",
            classification=ModelErrorClass.PERMANENT,
            code="safety_refusal",
            provider_metadata=_response_metadata(
                requested_model, provider_model, request_id, finish_reason="refusal"
            ),
        )
    if status == "failed":
        raise ModelInvocationError(
            "model provider failed the structured request",
            classification=ModelErrorClass.PERMANENT,
            code="provider_error",
            provider_metadata=_response_metadata(
                requested_model, provider_model, request_id, finish_reason="failed"
            ),
        )
    if status == "incomplete":
        raise _incomplete_failure(
            document,
            requested_model=requested_model,
            provider_model=provider_model,
            request_id=request_id,
        )
    if status != "completed":
        raise ModelInvocationError(
            "model provider returned an incomplete response",
            classification=ModelErrorClass.RECOVERABLE,
            code="incomplete_response",
            provider_metadata=_response_metadata(
                requested_model, provider_model, request_id, finish_reason="incomplete"
            ),
        )
    output_text = _output_text(document)
    try:
        output = json.loads(output_text)
    except (TypeError, json.JSONDecodeError, RecursionError):
        raise _malformed(request_id=request_id, model=requested_model) from None
    usage = document.get("usage")
    if not isinstance(usage, Mapping):
        raise _malformed(request_id=request_id, model=requested_model)
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if not _non_negative_int(input_tokens) or not _non_negative_int(output_tokens):
        raise _malformed(request_id=request_id, model=requested_model)
    try:
        response = ModelResponse(
            output=output,
            usage=ModelUsage(input_tokens, output_tokens),
            latency_ms=max(0, latency_ms),
            provider_metadata={
            "provider": "openai",
            "requestedModel": requested_model,
            "providerModel": provider_model,
            "requestId": request_id,
            "finishReason": "completed",
            "attemptCount": attempt_count,
            "attempts": [
                *prior_attempts,
                {
                    "attempt": attempt_count,
                    "code": "succeeded",
                    "requestId": request_id,
                    **dict(admission_evidence),
                    "retryDecision": "terminal",
                },
            ],
                **dict(admission_evidence),
                "retryDecision": "terminal",
            },
        )
    except (RecursionError, ValueError, TypeError):
        raise _malformed(request_id=request_id, model=requested_model) from None
    coordinator.observe_success(
        now=observed_at,
        reserved_tokens=reserved_tokens,
        actual_input_tokens=input_tokens,
        actual_output_tokens=output_tokens,
        reservation_id=reservation_id,
    )
    return response


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
        reason, scope = _provider_error_reason(response.body)
        permanent = {
            "quota": "quota",
            "billing": "billing",
            "authentication": "authentication",
            "authorization": "authorization",
            "invalid_request": "invalid_request",
            "unsupported_model": "unsupported_model",
        }
        if reason in permanent:
            return _failure(
                permanent[reason],
                recoverable=False,
                metadata={"httpStatus": 429, "providerErrorReason": reason},
            )
        return _failure(
            "rate_limit",
            recoverable=True,
            metadata={
                "httpStatus": 429,
                "providerErrorReason": reason or "temporary_throttle",
                "rateLimitScope": scope,
            },
        )
    if response.status in {408, 409} or response.status >= 500:
        return _failure("provider_unavailable", recoverable=True)
    if response.status in {401, 403}:
        code = "authentication" if response.status == 401 else "authorization"
        return _failure(code, recoverable=False, metadata={"httpStatus": response.status})
    if response.status == 400:
        details = _safe_invalid_request_evidence(response.body)
        if details:
            return _failure(
                "invalid_request", recoverable=False,
                metadata={"httpStatus": 400, **details},
            )
    return _failure("provider_error", recoverable=False)


def _failure(
    code: str, *, recoverable: bool, metadata: Mapping[str, Any] | None = None
) -> ModelInvocationError:
    messages = {
        "timeout": "model provider request timed out",
        "rate_limit": "model provider rate limit exceeded",
        "provider_unavailable": "model provider is unavailable",
        "authentication": "model provider authentication failed",
        "authorization": "model provider authorization failed",
        "quota": "model provider quota is exhausted",
        "billing": "model provider billing action is required",
        "invalid_request": "model provider rejected an invalid request",
        "unsupported_model": "model provider model is unsupported",
        "provider_error": "model provider rejected the request",
    }
    return ModelInvocationError(
        messages[code],
        classification=ModelErrorClass.RECOVERABLE if recoverable else ModelErrorClass.PERMANENT,
        code=code,
        provider_metadata=metadata,
    )


def _coordinator_unavailable(
    requested_model: str, attempts: list[dict[str, Any]]
) -> ModelInvocationError:
    return ModelInvocationError(
        "model rate-limit coordinator state is unavailable",
        classification=ModelErrorClass.RECOVERABLE,
        code="coordinator_unavailable",
        provider_metadata={
            "provider": "openai",
            "requestedModel": requested_model,
            "attemptCount": len(attempts),
            "attempts": attempts,
            "retryDecision": "suppressed",
        },
    )


def _incomplete_failure(
    document: Mapping[str, Any],
    *,
    requested_model: str,
    provider_model: str,
    request_id: str | None,
) -> ModelInvocationError:
    details = document.get("incomplete_details")
    reason = details.get("reason") if isinstance(details, Mapping) else None
    if reason == "content_filter":
        return ModelInvocationError(
            "model provider filtered the structured response",
            classification=ModelErrorClass.PERMANENT,
            code="safety_refusal",
            provider_metadata=_response_metadata(
                requested_model,
                provider_model,
                request_id,
                finish_reason="content_filter",
            ),
        )
    if reason == "max_output_tokens":
        return ModelInvocationError(
            "model provider exhausted the configured output token limit",
            classification=ModelErrorClass.PERMANENT,
            code="output_token_limit",
            provider_metadata=_response_metadata(
                requested_model,
                provider_model,
                request_id,
                finish_reason="max_output_tokens",
            ),
        )
    return ModelInvocationError(
        "model provider returned an incomplete response",
        classification=ModelErrorClass.RECOVERABLE,
        code="incomplete_response",
        provider_metadata=_response_metadata(
            requested_model, provider_model, request_id, finish_reason="incomplete"
        ),
    )


def _malformed(*, request_id: str | None = None, model: str | None = None) -> ModelInvocationError:
    metadata = {"provider": "openai", "requestId": request_id}
    if model is not None:
        metadata["requestedModel"] = model
    return ModelInvocationError(
        "model provider returned a malformed structured response",
        classification=ModelErrorClass.PERMANENT,
        code="malformed_response",
        provider_metadata=metadata,
    )


def _response_metadata(
    requested_model: str,
    provider_model: str,
    request_id: str | None,
    *,
    finish_reason: str,
) -> dict[str, Any]:
    return {
        "provider": "openai",
        "requestedModel": requested_model,
        "providerModel": provider_model,
        "requestId": request_id,
        "finishReason": finish_reason,
    }


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
            parsed = _parsed_retry_after_ms(value)
            return parsed if parsed is not None else 0
    return 0


def _parsed_retry_after_ms(value: Any) -> int | None:
    try:
        seconds = float(value)
        if not isfinite(seconds):
            return None
        return max(0, round(seconds * 1000))
    except (OverflowError, TypeError, ValueError):
        return None


def _backoff_ms(base_ms: int, attempt: int, jitter: float) -> int:
    safe_jitter = min(1.0, max(0.0, jitter if isfinite(jitter) else 0.0))
    exponential = min(_MAX_BACKOFF_MS, base_ms * (2 ** max(0, attempt - 1)))
    return min(_MAX_BACKOFF_MS, round(exponential * (1 + safe_jitter / 2)))


def _future_timestamp(now: datetime, delay_ms: int) -> str:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    value = now.astimezone(timezone.utc) + timedelta(milliseconds=delay_ms)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _provider_error_reason(body: bytes) -> tuple[str | None, str]:
    try:
        document = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return None, "unknown"
    error = document.get("error") if isinstance(document, Mapping) else None
    if not isinstance(error, Mapping):
        return None, "unknown"
    values = {error.get("type"), error.get("code")}
    known = {value for value in values if isinstance(value, str)}
    mappings = {
        "insufficient_quota": ("quota", "quota"),
        "billing_hard_limit_reached": ("billing", "billing"),
        "invalid_api_key": ("authentication", "access"),
        "authentication_error": ("authentication", "access"),
        "permission_denied": ("authorization", "access"),
        "invalid_request_error": ("invalid_request", "request"),
        "context_length_exceeded": ("invalid_request", "tokens"),
        "model_not_found": ("unsupported_model", "model"),
        "unsupported_model": ("unsupported_model", "model"),
        "tokens": ("temporary_throttle", "tokens"),
        "requests": ("temporary_throttle", "requests"),
        "rate_limit_exceeded": ("temporary_throttle", "unknown"),
    }
    for value in sorted(known):
        if value in mappings:
            return mappings[value]
    return None, "unknown"


def _safe_invalid_request_evidence(body: bytes) -> dict[str, Any]:
    """Extract only fixed classifications and a sanitized schema parameter."""

    try:
        document = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return {}
    error = document.get("error") if isinstance(document, Mapping) else None
    if not isinstance(error, Mapping):
        return {}
    error_type = error.get("type")
    error_code = error.get("code")
    if error_type != "invalid_request_error" and error_code not in {
        "invalid_json_schema", "invalid_response_format"
    }:
        return {}
    evidence: dict[str, Any] = {"providerErrorReason": "invalid_response_format"}
    if error_type == "invalid_request_error":
        evidence["providerErrorType"] = "invalid_request_error"
    if error_code in {"invalid_json_schema", "invalid_response_format"}:
        evidence["providerErrorCode"] = error_code
    parameter = error.get("param")
    if isinstance(parameter, str) and re.fullmatch(
        r"(?:text\.format\.schema|response_format\.json_schema\.schema)(?:\.(?:properties|items|required|anyOf|oneOf|allOf|\$defs|[A-Za-z0-9_-]+))*",
        parameter,
    ):
        evidence["schemaParameter"] = parameter
    return evidence


def _safe_rate_limit_evidence(
    response: ProviderHttpResponse | None,
) -> dict[str, Any]:
    if response is None:
        return {}
    evidence: dict[str, Any] = {"httpStatus": response.status}
    retry_after_header = next(
        (
            value
            for key, value in response.headers.items()
            if key.casefold() == "retry-after"
        ),
        None,
    )
    retry_after = _parsed_retry_after_ms(retry_after_header)
    evidence["retryAfterReceived"] = retry_after_header is not None
    if retry_after is not None:
        evidence["retryAfterMs"] = retry_after
    header_fields = {
        "x-ratelimit-limit-requests": "limitRequests",
        "x-ratelimit-remaining-requests": "remainingRequests",
        "x-ratelimit-limit-tokens": "limitTokens",
        "x-ratelimit-remaining-tokens": "remainingTokens",
        "x-ratelimit-reset-requests": "resetRequestsMs",
        "x-ratelimit-reset-tokens": "resetTokensMs",
    }
    for key, raw in response.headers.items():
        field = header_fields.get(key.casefold())
        if field is None or not isinstance(raw, str):
            continue
        parsed = _numeric_hint_ms(raw) if field.startswith("reset") else _integer_hint(raw)
        if parsed is not None:
            evidence[field] = parsed
    failure = _http_failure(response)
    if failure is not None:
        for key in ("providerErrorReason", "rateLimitScope"):
            if key in failure.provider_metadata:
                evidence[key] = failure.provider_metadata[key]
    return evidence


def _integer_hint(value: str) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _numeric_hint_ms(value: str) -> int | None:
    normalized = value.strip().casefold()
    multiplier = 1000
    if normalized.endswith("ms"):
        normalized, multiplier = normalized[:-2], 1
    elif normalized.endswith("s"):
        normalized = normalized[:-1]
    try:
        parsed = float(normalized)
    except (OverflowError, TypeError, ValueError):
        return None
    if not isfinite(parsed) or parsed < 0:
        return None
    return round(parsed * multiplier)


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
        projected: dict[str, Any] = {}
        for key, item in value.items():
            if key in _UNSUPPORTED_SCHEMA_KEYWORDS:
                continue
            if key in _SCHEMA_MAP_KEYWORDS and isinstance(item, Mapping):
                projected[str(key)] = {
                    str(name): _provider_schema(schema)
                    for name, schema in item.items()
                }
            else:
                projected[str(key)] = _provider_schema(item)
        return projected
    if isinstance(value, (list, tuple)):
        return [_provider_schema(item) for item in value]
    return value


def _provider_input(assembled_input: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    prompt = assembled_input.get("prompt")
    context_package = assembled_input.get("contextPackage")
    if not isinstance(prompt, Mapping) or not isinstance(context_package, Mapping):
        raise ValueError("assembled input is missing prompt or context")
    system = prompt.get("system")
    formatting = prompt.get("formatting")
    examples = prompt.get("examples")
    if not isinstance(system, str) or not system:
        raise ValueError("assembled prompt is missing system instructions")
    if formatting is not None and (not isinstance(formatting, str) or not formatting):
        raise ValueError("assembled prompt formatting is invalid")
    instruction_parts = [system]
    if formatting is not None:
        instruction_parts.append(formatting)
    if examples is not None:
        instruction_parts.append(
            "Examples:\n"
            + json.dumps(
                examples,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
        )
    return "\n\n".join(instruction_parts), {
        "contextPackage": dict(context_package),
    }
