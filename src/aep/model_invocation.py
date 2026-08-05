"""Provider-neutral model invocation boundary.

Vendor adapters translate their SDK requests and responses at this boundary. Model
providers deliberately do not participate in the Tool Platform.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any

from aep.observability import CorrelationContext, bind_correlation


JsonObject = Mapping[str, Any]


def _mapping(value: Mapping[str, Any]) -> JsonObject:
    return MappingProxyType(deepcopy(dict(value)))


@dataclass(frozen=True)
class ModelConfiguration:
    """Immutable configuration resolved from a versioned Model Resource."""

    model_ref: JsonObject
    provider: str
    model: str
    parameters: JsonObject = field(default_factory=dict)
    token_limit: int | None = None
    timeout_ms: int | None = None

    def __post_init__(self) -> None:
        if not self.provider or not self.model:
            raise ValueError("provider and model must not be empty")
        if self.model_ref.get("kind") != "Model":
            raise ValueError("model_ref.kind must be 'Model'")
        for key in ("name", "version"):
            if not self.model_ref.get(key):
                raise ValueError(f"model_ref.{key} must not be empty")
        if self.token_limit is not None and self.token_limit < 1:
            raise ValueError("token_limit must be positive")
        if self.timeout_ms is not None and self.timeout_ms < 1:
            raise ValueError("timeout_ms must be positive")
        object.__setattr__(self, "model_ref", _mapping(self.model_ref))
        object.__setattr__(self, "parameters", _mapping(self.parameters))


@dataclass(frozen=True)
class ModelRequest:
    """A fully assembled input plus its immutable Model configuration."""

    configuration: ModelConfiguration
    input: JsonObject
    correlation: CorrelationContext | Mapping[str, Any]

    def __post_init__(self) -> None:
        context = bind_correlation(self.correlation)
        if context.task_execution_id is None:
            raise ValueError("correlation requires taskExecutionId")
        object.__setattr__(self, "input", _mapping(self.input))
        object.__setattr__(self, "correlation", context)


@dataclass(frozen=True)
class ModelUsage:
    input_tokens: int
    output_tokens: int

    def __post_init__(self) -> None:
        if self.input_tokens < 0 or self.output_tokens < 0:
            raise ValueError("token usage must not be negative")

    def as_record(self) -> dict[str, int]:
        return {"input": self.input_tokens, "output": self.output_tokens}


@dataclass(frozen=True)
class ModelResponse:
    """Normalized model output and execution evidence."""

    output: Any
    usage: ModelUsage
    latency_ms: int
    provider_metadata: JsonObject = field(default_factory=dict)
    cost: float | None = None

    def __post_init__(self) -> None:
        if self.latency_ms < 0:
            raise ValueError("latency_ms must not be negative")
        if self.cost is not None and self.cost < 0:
            raise ValueError("cost must not be negative")
        object.__setattr__(self, "output", deepcopy(self.output))
        object.__setattr__(self, "provider_metadata", _mapping(self.provider_metadata))


class ModelErrorClass(str, Enum):
    RECOVERABLE = "RECOVERABLE"
    PERMANENT = "PERMANENT"


class ModelInvocationError(Exception):
    """Normalized provider failure used by orchestration retry decisions."""

    def __init__(
        self,
        message: str,
        *,
        classification: ModelErrorClass,
        code: str,
        provider_metadata: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.classification = classification
        self.code = code
        self.provider_metadata = _mapping(provider_metadata or {})

    @property
    def recoverable(self) -> bool:
        return self.classification is ModelErrorClass.RECOVERABLE


class ModelAdapter(ABC):
    """Extension point implemented by each real model provider."""

    @abstractmethod
    def invoke(self, request: ModelRequest) -> ModelResponse:
        """Invoke a provider and return normalized evidence, or a classified error."""


class FakeModelAdapter(ModelAdapter):
    """Deterministic configurable adapter for tests and local execution."""

    def __init__(self, outcomes: Sequence[ModelResponse | ModelInvocationError]) -> None:
        if not outcomes:
            raise ValueError("outcomes must not be empty")
        self._outcomes = tuple(outcomes)
        self.requests: list[ModelRequest] = []

    def invoke(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self._outcomes) - 1)
        outcome = self._outcomes[index]
        if isinstance(outcome, ModelInvocationError):
            raise outcome
        return outcome


def model_invocation_record(
    *,
    invocation_id: str,
    agent_invocation_id: str,
    request: ModelRequest,
    response: ModelResponse,
    started_at: str,
    completed_at: str,
    input_address: str | None = None,
    output_address: str | None = None,
    schema_validation: str = "NOT_RUN",
    provenance: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the persistence-ready successful ModelInvocation runtime record."""

    context = bind_correlation(request.correlation, provenance=provenance)
    record: dict[str, Any] = {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "ModelInvocation",
        "id": invocation_id,
        "traceId": context.trace_id,
        "createdAt": started_at,
        "updatedAt": completed_at,
        "agentInvocationId": agent_invocation_id,
        "modelRef": dict(request.configuration.model_ref),
        "status": "SUCCEEDED",
        "tokenUsage": response.usage.as_record(),
        "latencyMs": response.latency_ms,
        "providerMetadata": dict(response.provider_metadata),
        "schemaValidation": schema_validation,
        "startedAt": started_at,
        "completedAt": completed_at,
    }
    record["provenance"] = deepcopy(dict(provenance))
    if input_address is not None:
        record["inputAddress"] = input_address
    if output_address is not None:
        record["outputAddress"] = output_address
    if response.cost is not None:
        record["cost"] = response.cost
    return record
