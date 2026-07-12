"""Provider-neutral contract for controlled non-model Tool execution."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from types import MappingProxyType
from time import monotonic
from typing import Any

from jsonschema import Draft202012Validator


JsonObject = Mapping[str, Any]


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return deepcopy(value)


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return deepcopy(value)


def _mapping(value: Mapping[str, Any]) -> JsonObject:
    return _freeze(value)


class ToolLifecycleState(str, Enum):
    REQUESTED = "REQUESTED"
    VALIDATED = "VALIDATED"
    AUTHORIZED = "AUTHORIZED"
    SCHEDULED = "SCHEDULED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ToolResultStatus(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    DENIED = "DENIED"
    TIMED_OUT = "TIMED_OUT"


class ToolFailureClass(str, Enum):
    VALIDATION = "VALIDATION"
    POLICY = "POLICY"
    TIMEOUT = "TIMEOUT"
    ADAPTER = "ADAPTER"


@dataclass(frozen=True)
class ToolCaller:
    """Runtime identity requesting the Tool invocation."""

    kind: str
    id: str

    def __post_init__(self) -> None:
        if not self.kind or not self.id:
            raise ValueError("caller kind and id must not be empty")

    def as_record(self) -> dict[str, str]:
        return {"kind": self.kind, "id": self.id}


@dataclass(frozen=True)
class ToolRequest:
    """A validated-by-contract request for one non-model Tool execution."""

    tool_ref: JsonObject
    input: JsonObject
    caller: ToolCaller
    capabilities: Sequence[str]
    timeout_ms: int
    trace_id: str

    def __post_init__(self) -> None:
        if self.tool_ref.get("kind") != "Tool":
            raise ValueError("tool_ref.kind must be 'Tool'; Model provider calls are excluded")
        for key in ("name", "version"):
            if not self.tool_ref.get(key):
                raise ValueError(f"tool_ref.{key} must not be empty")
        if self.tool_ref["version"] == "latest":
            raise ValueError("tool_ref.version must be an immutable version, not 'latest'")
        capabilities = tuple(self.capabilities)
        if not capabilities or any(not capability for capability in capabilities):
            raise ValueError("capabilities must contain non-empty values")
        if len(set(capabilities)) != len(capabilities):
            raise ValueError("capabilities must be unique")
        if self.timeout_ms < 1:
            raise ValueError("timeout_ms must be positive")
        if not self.trace_id:
            raise ValueError("trace_id must not be empty")
        object.__setattr__(self, "tool_ref", _mapping(self.tool_ref))
        object.__setattr__(self, "input", _mapping(self.input))
        object.__setattr__(self, "capabilities", capabilities)


@dataclass(frozen=True)
class ToolMetrics:
    duration_ms: int
    cpu_ms: int | None = None
    memory_bytes: int | None = None

    def __post_init__(self) -> None:
        if self.duration_ms < 0:
            raise ValueError("duration_ms must not be negative")
        if self.cpu_ms is not None and self.cpu_ms < 0:
            raise ValueError("cpu_ms must not be negative")
        if self.memory_bytes is not None and self.memory_bytes < 0:
            raise ValueError("memory_bytes must not be negative")

    def as_record(self) -> dict[str, int]:
        record = {"durationMs": self.duration_ms}
        if self.cpu_ms is not None:
            record["cpuMs"] = self.cpu_ms
        if self.memory_bytes is not None:
            record["memoryBytes"] = self.memory_bytes
        return record


@dataclass(frozen=True)
class ToolResult:
    """Normalized Tool output and the evidence needed for persistence."""

    status: ToolResultStatus
    output: Any
    logs_ref: str | None
    metrics: ToolMetrics
    started_at: str
    completed_at: str
    failure_class: ToolFailureClass | None = None
    failure_message: str | None = None

    def __post_init__(self) -> None:
        if not self.started_at or not self.completed_at:
            raise ValueError("result timing must include started_at and completed_at")
        if self.status is ToolResultStatus.SUCCEEDED and self.failure_class is not None:
            raise ValueError("successful results must not include a failure class")
        if self.status is not ToolResultStatus.SUCCEEDED and self.failure_class is None:
            raise ValueError("unsuccessful results must include a failure class")
        object.__setattr__(self, "output", _freeze(self.output))

    def output_record(self) -> Any:
        """Return a mutable JSON-compatible copy for persistence or transport."""

        return _thaw(self.output)


class ToolSchemaValidationError(ValueError):
    def __init__(self, phase: str, messages: Sequence[str]) -> None:
        self.phase = phase
        self.messages = tuple(messages)
        super().__init__(f"{phase} schema validation failed: {'; '.join(self.messages)}")


class ToolSchemaValidator(ABC):
    """Hook for validating Tool input and output contracts."""

    @abstractmethod
    def validate_input(self, value: Any) -> None:
        """Raise ToolSchemaValidationError when input is invalid."""

    @abstractmethod
    def validate_output(self, value: Any) -> None:
        """Raise ToolSchemaValidationError when output is invalid."""


class JsonSchemaToolValidator(ToolSchemaValidator):
    def __init__(self, input_schema: Mapping[str, Any], output_schema: Mapping[str, Any]) -> None:
        Draft202012Validator.check_schema(input_schema)
        Draft202012Validator.check_schema(output_schema)
        self._input = Draft202012Validator(deepcopy(dict(input_schema)))
        self._output = Draft202012Validator(deepcopy(dict(output_schema)))

    @staticmethod
    def _validate(validator: Draft202012Validator, phase: str, value: Any) -> None:
        json_value = _thaw(value)
        errors = sorted(
            validator.iter_errors(json_value), key=lambda error: list(error.path)
        )
        if errors:
            raise ToolSchemaValidationError(phase, [error.message for error in errors])

    def validate_input(self, value: Any) -> None:
        self._validate(self._input, "input", value)

    def validate_output(self, value: Any) -> None:
        self._validate(self._output, "output", value)


class ToolAdapter(ABC):
    """Extension boundary implemented by Filesystem, Git, Docker, and GitHub adapters."""

    @abstractmethod
    def invoke(self, request: ToolRequest) -> ToolResult:
        """Execute in a sandbox and return normalized evidence."""


class FakeToolAdapter(ToolAdapter):
    """Deterministic adapter for contract tests and local execution."""

    def __init__(self, outcomes: Sequence[ToolResult | Exception]) -> None:
        if not outcomes:
            raise ValueError("outcomes must not be empty")
        self._outcomes = tuple(outcomes)
        self.requests: list[ToolRequest] = []

    def invoke(self, request: ToolRequest) -> ToolResult:
        self.requests.append(request)
        outcome = self._outcomes[min(len(self.requests) - 1, len(self._outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


AuthorizationHook = Callable[[ToolRequest], bool]


def invoke_tool(
    request: ToolRequest,
    *,
    validator: ToolSchemaValidator,
    authorize: AuthorizationHook,
    adapter: ToolAdapter,
) -> ToolResult:
    """Validate, authorize, execute by deadline, and normalize contract failures."""

    started_at = datetime.now(UTC)
    started_clock = monotonic()

    def failure(
        status: ToolResultStatus,
        classification: ToolFailureClass,
        message: str,
    ) -> ToolResult:
        completed_at = datetime.now(UTC)
        return ToolResult(
            status=status,
            output=None,
            logs_ref=None,
            metrics=ToolMetrics(
                duration_ms=max(0, round((monotonic() - started_clock) * 1000))
            ),
            started_at=started_at.isoformat().replace("+00:00", "Z"),
            completed_at=completed_at.isoformat().replace("+00:00", "Z"),
            failure_class=classification,
            failure_message=message,
        )

    try:
        validator.validate_input(request.input)
    except ToolSchemaValidationError as error:
        return failure(ToolResultStatus.FAILED, ToolFailureClass.VALIDATION, str(error))

    if not authorize(request):
        return failure(
            ToolResultStatus.DENIED,
            ToolFailureClass.POLICY,
            "requested capabilities were denied",
        )

    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="aep-tool")
    future = executor.submit(adapter.invoke, request)
    try:
        result = future.result(timeout=request.timeout_ms / 1000)
    except FutureTimeoutError:
        future.cancel()
        return failure(
            ToolResultStatus.TIMED_OUT,
            ToolFailureClass.TIMEOUT,
            f"adapter exceeded timeout of {request.timeout_ms}ms",
        )
    except Exception as error:
        return failure(
            ToolResultStatus.FAILED,
            ToolFailureClass.ADAPTER,
            str(error) or type(error).__name__,
        )
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    if result.status is ToolResultStatus.SUCCEEDED:
        try:
            validator.validate_output(result.output)
        except ToolSchemaValidationError as error:
            return failure(
                ToolResultStatus.FAILED, ToolFailureClass.VALIDATION, str(error)
            )
    return result
