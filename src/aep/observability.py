"""Provider-neutral structured logging and trace correlation helpers."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
import re
from typing import Any

from aep.runtime_validation import is_rfc3339_timestamp


REDACTED = "[REDACTED]"
OMITTED = "[OMITTED]"
BOUNDARY_FIELDS = ("traceId", "workflowExecutionId", "taskExecutionId")

_SECRET_KEYS = {
    "apikey",
    "authorization",
    "clientsecret",
    "cookie",
    "credential",
    "credentials",
    "password",
    "passwd",
    "privatekey",
    "proxyauthorization",
    "refreshtoken",
    "secret",
    "setcookie",
    "token",
    "accesstoken",
}
_BODY_KEYS = {"artifactbody", "body", "content", "rawcontent"}
_ARTIFACT_CONTAINERS = {"artifact", "artifacts", "generatedartifact", "generatedartifacts"}
_ARTIFACT_BODY_KEYS = {
    "body",
    "bytes",
    "content",
    "data",
    "diff",
    "patch",
    "payload",
    "rawcontent",
    "text",
}
_SAFE_TOKEN_KEYS = {"tokencount", "tokenestimate", "tokenusage", "tokenbudget"}
_CONTAINER_KEYS = {"environment", "env", "headers", "requestheaders", "responseheaders"}
_ENV_SECRET_KEYS = {
    "connectionstring",
    "databaseurl",
    "dburl",
    "dsn",
    "jdbcurl",
    "redisurl",
}
_SECRET_VALUE = re.compile(
    r"(?:bearer\s+\S+|gh[pousr]_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16})",
    re.IGNORECASE,
)
_EVENT_NAME = re.compile(r"^[A-Z][A-Za-z0-9]+$")
_CREDENTIAL_URL = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@")

_LIFECYCLE_SEMANTICS: dict[str, tuple[str, frozenset[str], bool]] = {
    "WorkflowExecutionStarted": ("WorkflowExecution", frozenset({"RUNNING"}), False),
    "WorkflowExecutionCompleted": ("WorkflowExecution", frozenset({"SUCCEEDED"}), False),
    "WorkflowExecutionFailed": ("WorkflowExecution", frozenset({"FAILED"}), True),
    "WorkflowExecutionCancelled": ("WorkflowExecution", frozenset({"CANCELLED"}), False),
    "TaskExecutionQueued": ("TaskExecution", frozenset({"PENDING", "QUEUED"}), False),
    "TaskExecutionStarted": ("TaskExecution", frozenset({"RUNNING"}), False),
    "TaskExecutionSucceeded": ("TaskExecution", frozenset({"SUCCEEDED"}), False),
    "TaskExecutionFailed": ("TaskExecution", frozenset({"FAILED"}), True),
    "TaskExecutionCancelled": ("TaskExecution", frozenset({"CANCELLED"}), False),
    "TaskExecutionAwaitingApproval": (
        "TaskExecution",
        frozenset({"AWAITING_APPROVAL"}),
        False,
    ),
    "ContextPackageCreated": ("ContextPackage", frozenset({"CREATED"}), False),
    "AgentResolved": ("ResolvedAgent", frozenset({"CREATED"}), False),
    "AgentInvocationStarted": ("AgentInvocation", frozenset({"RUNNING"}), False),
    "AgentInvocationCompleted": ("AgentInvocation", frozenset({"SUCCEEDED"}), False),
    "AgentInvocationFailed": ("AgentInvocation", frozenset({"FAILED"}), True),
    "ModelInvocationStarted": ("ModelInvocation", frozenset({"RUNNING"}), False),
    "ModelInvocationCompleted": ("ModelInvocation", frozenset({"SUCCEEDED"}), False),
    "ModelInvocationFailed": ("ModelInvocation", frozenset({"FAILED"}), True),
    "ToolInvocationStarted": ("ToolInvocation", frozenset({"RUNNING"}), False),
    "ToolInvocationCompleted": ("ToolInvocation", frozenset({"SUCCEEDED"}), False),
    "ToolInvocationFailed": ("ToolInvocation", frozenset({"FAILED"}), True),
    "EvaluationCompleted": ("EvaluationResult", frozenset({"SUCCEEDED"}), False),
    "EvaluationFailed": ("EvaluationResult", frozenset({"FAILED"}), True),
    "PolicyDecisionRecorded": ("PolicyDecision", frozenset({"RECORDED"}), False),
    "ApprovalRequested": ("Approval", frozenset({"PENDING"}), False),
    "ApprovalRecorded": (
        "Approval",
        frozenset({"APPROVED", "REJECTED", "EXPIRED"}),
        False,
    ),
    "GeneratedArtifactCreated": ("GeneratedArtifact", frozenset({"CREATED"}), False),
}
_FAILURE_CLASSES = {
    "RECOVERABLE",
    "CONFIGURATION",
    "EVALUATION",
    "POLICY",
    "PERMANENT",
}


class ObservabilityContractError(ValueError):
    """Raised when correlation or lifecycle telemetry is incomplete."""


@dataclass(frozen=True, slots=True)
class CorrelationContext:
    """The provider-neutral fields propagated across AEP service boundaries."""

    trace_id: str
    workflow_execution_id: str
    task_execution_id: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.trace_id, "traceId")
        _require_text(self.workflow_execution_id, "workflowExecutionId")
        if self.task_execution_id is not None:
            _require_text(self.task_execution_id, "taskExecutionId")

    def to_boundary_fields(self) -> dict[str, str]:
        fields = {
            "traceId": self.trace_id,
            "workflowExecutionId": self.workflow_execution_id,
        }
        if self.task_execution_id is not None:
            fields["taskExecutionId"] = self.task_execution_id
        return fields

    @classmethod
    def from_boundary_fields(cls, fields: Mapping[str, Any]) -> "CorrelationContext":
        return cls(
            trace_id=_required_mapping_text(fields, "traceId"),
            workflow_execution_id=_required_mapping_text(
                fields, "workflowExecutionId"
            ),
            task_execution_id=_optional_mapping_text(fields, "taskExecutionId"),
        )

    @classmethod
    def from_runtime_object(cls, value: Mapping[str, Any]) -> "CorrelationContext":
        provenance = value.get("provenance")
        provenance = provenance if isinstance(provenance, Mapping) else {}
        kind = value.get("kind")
        workflow_candidates = [
            candidate
            for candidate in (
                value.get("id") if kind == "WorkflowExecution" else None,
                value.get("workflowExecutionId"),
                provenance.get("workflowExecutionId"),
            )
            if candidate is not None
        ]
        task_candidates = [
            candidate
            for candidate in (
                value.get("id") if kind == "TaskExecution" else None,
                value.get("taskExecutionId"),
                provenance.get("taskExecutionId"),
            )
            if candidate is not None
        ]
        workflow_execution_id = _one_identity(
            workflow_candidates, "workflowExecutionId"
        )
        task_execution_id = (
            _one_identity(task_candidates, "taskExecutionId")
            if task_candidates
            else None
        )
        return cls(
            trace_id=_required_mapping_text(value, "traceId"),
            workflow_execution_id=_require_text(
                workflow_execution_id, "workflowExecutionId"
            ),
            task_execution_id=(
                _require_text(task_execution_id, "taskExecutionId")
                if task_execution_id is not None
                else None
            ),
        )


def bind_correlation(
    correlation: CorrelationContext | Mapping[str, Any],
    *,
    task_execution_id: str | None = None,
    provenance: Mapping[str, Any] | None = None,
) -> CorrelationContext:
    """Validate one boundary context against explicit and provenance identities."""

    context = (
        correlation
        if isinstance(correlation, CorrelationContext)
        else CorrelationContext.from_boundary_fields(correlation)
    )
    if task_execution_id is not None and context.task_execution_id != task_execution_id:
        raise ObservabilityContractError(
            "taskExecutionId conflicts with the correlation context"
        )
    if provenance is not None:
        provenance_workflow = provenance.get("workflowExecutionId")
        provenance_task = provenance.get("taskExecutionId")
        if (
            provenance_workflow is not None
            and provenance_workflow != context.workflow_execution_id
        ):
            raise ObservabilityContractError(
                "provenance.workflowExecutionId conflicts with the correlation context"
            )
        if provenance_task is not None and provenance_task != context.task_execution_id:
            raise ObservabilityContractError(
                "provenance.taskExecutionId conflicts with the correlation context"
            )
    return context


def propagation_fields(
    parent: Mapping[str, Any], *, task_execution_id: str | None = None
) -> dict[str, str]:
    """Return correlation metadata for constructing a downstream request/object."""

    context = CorrelationContext.from_runtime_object(parent)
    if task_execution_id is not None:
        context = CorrelationContext(
            context.trace_id, context.workflow_execution_id, task_execution_id
        )
    return context.to_boundary_fields()


def assert_trace_continuity(runtime_objects: Iterable[Mapping[str, Any]]) -> str:
    """Verify that a runtime-object chain carries one non-empty trace identifier."""

    records = tuple(runtime_objects)
    if not records:
        raise ObservabilityContractError("at least one runtime object is required")
    trace_ids = {_required_mapping_text(record, "traceId") for record in records}
    if len(trace_ids) != 1:
        raise ObservabilityContractError(
            f"runtime objects do not share one traceId: {sorted(trace_ids)!r}"
        )
    return next(iter(trace_ids))


def redact(value: Any, *, max_string_length: int = 4096) -> Any:
    """Return JSON-compatible telemetry with secrets and large bodies removed."""

    return _redact(value, max_string_length=max_string_length, container=None)


def _redact(value: Any, *, max_string_length: int, container: str | None) -> Any:
    if isinstance(value, Mapping):
        cleaned: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            if _is_secret_key(normalized, container=container):
                cleaned[key] = REDACTED
            elif _is_body_key(normalized, container=container):
                cleaned[key] = OMITTED
            else:
                next_container = (
                    normalized
                    if normalized in _CONTAINER_KEYS or normalized in _ARTIFACT_CONTAINERS
                    else container
                )
                cleaned[key] = _redact(
                    item,
                    max_string_length=max_string_length,
                    container=next_container,
                )
        return cleaned
    if isinstance(value, (list, tuple)):
        return [
            _redact(item, max_string_length=max_string_length, container=container)
            for item in value
        ]
    if isinstance(value, str):
        if _SECRET_VALUE.search(value) or _CREDENTIAL_URL.search(value):
            return REDACTED
        if len(value) > max_string_length:
            return OMITTED
        return value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def _is_secret_key(key: str, *, container: str | None) -> bool:
    if key in _SAFE_TOKEN_KEYS:
        return False
    if key in _SECRET_KEYS or any(
        marker in key
        for marker in (
            "apikey",
            "authentication",
            "authorization",
            "credential",
            "password",
            "privatekey",
            "secret",
            "token",
        )
    ):
        return True
    if key.endswith("token") or key.endswith("secret") or key.endswith("credential"):
        return True
    if container in {"headers", "requestheaders", "responseheaders"}:
        return any(
            marker in key
            for marker in ("auth", "cookie", "credential", "key", "secret", "token")
        )
    if container in {"env", "environment"}:
        return key in _ENV_SECRET_KEYS or any(
            marker in key
            for marker in ("password", "secret", "token", "credential", "privatekey")
        )
    return False


def _is_body_key(key: str, *, container: str | None) -> bool:
    if key in _BODY_KEYS:
        return True
    if container in _ARTIFACT_CONTAINERS and key in _ARTIFACT_BODY_KEYS:
        return True
    return any(
        key == f"artifact{alias}" or key == f"generatedartifact{alias}"
        for alias in _ARTIFACT_BODY_KEYS
    )


class StructuredLifecycleLogger:
    """Build and emit deterministic lifecycle records through an injected sink."""

    def __init__(self, sink: Callable[[Mapping[str, Any]], None]) -> None:
        self._sink = sink

    def emit(
        self,
        *,
        event_name: str,
        service: str,
        runtime_object: Mapping[str, Any],
        emitted_at: str,
        status: str | None = None,
        duration_ms: int | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        record = lifecycle_log(
            event_name=event_name,
            service=service,
            runtime_object=runtime_object,
            emitted_at=emitted_at,
            status=status,
            duration_ms=duration_ms,
            attributes=attributes,
        )
        self._sink(deepcopy(record))
        return record


def lifecycle_log(
    *,
    event_name: str,
    service: str,
    runtime_object: Mapping[str, Any],
    emitted_at: str,
    status: str | None = None,
    duration_ms: int | None = None,
    attributes: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Construct one schema-ready, redacted AEP lifecycle log record."""

    event_name = _require_text(event_name, "eventName")
    if _EVENT_NAME.fullmatch(event_name) is None:
        raise ObservabilityContractError("eventName must use PascalCase alphanumerics")
    if event_name not in _LIFECYCLE_SEMANTICS:
        raise ObservabilityContractError(f"unsupported lifecycle eventName {event_name!r}")
    _require_text(service, "service")
    emitted_at = _require_text(emitted_at, "emittedAt")
    if not is_rfc3339_timestamp(emitted_at):
        raise ObservabilityContractError("emittedAt must be an RFC3339 timestamp")
    if duration_ms is not None and (
        isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms < 0
    ):
        raise ObservabilityContractError("durationMs must be a non-negative integer")

    correlation = CorrelationContext.from_runtime_object(runtime_object)
    object_id = _required_mapping_text(runtime_object, "id")
    kind = _required_mapping_text(runtime_object, "kind")
    resolved_status = status or runtime_object.get("status")
    resolved_status = _require_text(resolved_status, "status")
    provenance = runtime_object.get("provenance")
    provenance = provenance if isinstance(provenance, Mapping) else {}
    repository_revision = (
        runtime_object.get("repositoryRevision")
        or provenance.get("repositoryRevision")
    )
    repository_revision = _require_text(
        repository_revision, "repositoryRevision"
    )
    refs = _resource_refs(runtime_object, provenance)
    if not refs:
        raise ObservabilityContractError(
            "resourceVersions must contain at least one immutable Resource reference"
        )
    failure = runtime_object.get("failure")
    failure_class = failure.get("class") if isinstance(failure, Mapping) else None
    _validate_lifecycle_semantics(
        event_name=event_name,
        runtime_kind=kind,
        status=resolved_status,
        failure_class=failure_class,
    )

    record: dict[str, Any] = {
        "schemaVersion": "aep.dev/observability/v1alpha1",
        "eventName": event_name,
        "emittedAt": emitted_at,
        "service": service,
        "level": "ERROR" if failure_class else "INFO",
        "traceId": correlation.trace_id,
        "executionId": correlation.workflow_execution_id,
        "taskId": correlation.task_execution_id,
        "runtimeKind": kind,
        "runtimeObjectId": object_id,
        "resourceVersions": refs,
        "repositoryRevision": repository_revision,
        "status": resolved_status,
        "failureClass": failure_class,
    }
    if duration_ms is not None:
        record["durationMs"] = duration_ms
    if attributes:
        record["attributes"] = redact(attributes)
    return record


def _validate_lifecycle_semantics(
    *, event_name: str, runtime_kind: str, status: str, failure_class: Any
) -> None:
    expected_kind, statuses, failure_required = _LIFECYCLE_SEMANTICS[event_name]
    if runtime_kind != expected_kind:
        raise ObservabilityContractError(
            f"{event_name} requires runtimeKind {expected_kind}, got {runtime_kind}"
        )
    if status not in statuses:
        raise ObservabilityContractError(
            f"{event_name} is incompatible with status {status!r}"
        )
    if failure_required:
        if failure_class not in _FAILURE_CLASSES:
            raise ObservabilityContractError(
                f"{event_name} requires a valid failure class"
            )
    elif failure_class is not None:
        raise ObservabilityContractError(
            f"{event_name} must not carry failure class {failure_class!r}"
        )


def _resource_refs(
    runtime_object: Mapping[str, Any], provenance: Mapping[str, Any]
) -> list[dict[str, str]]:
    candidates: list[Any] = []
    candidates.extend(provenance.get("resourceRefs", []))
    for key, value in runtime_object.items():
        if key.endswith("Ref"):
            candidates.append(value)
        elif key.endswith("Refs") and isinstance(value, list):
            candidates.extend(value)
    refs: dict[tuple[str, str, str], dict[str, str]] = {}
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        parts = tuple(candidate.get(field) for field in ("kind", "name", "version"))
        if all(isinstance(part, str) and part for part in parts):
            key = (str(parts[0]), str(parts[1]), str(parts[2]))
            if key[2] == "latest":
                raise ObservabilityContractError(
                    "resourceVersions must not contain floating latest references"
                )
            refs[key] = {"kind": key[0], "name": key[1], "version": key[2]}
    return [refs[key] for key in sorted(refs)]


def _required_mapping_text(value: Mapping[str, Any], field: str) -> str:
    return _require_text(value.get(field), field)


def _optional_mapping_text(value: Mapping[str, Any], field: str) -> str | None:
    candidate = value.get(field)
    return None if candidate is None else _require_text(candidate, field)


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ObservabilityContractError(f"{field} must be a non-empty string")
    return value.strip()


def _one_identity(candidates: list[Any], field: str) -> str:
    normalized = [_require_text(candidate, field) for candidate in candidates]
    if len(set(normalized)) != 1:
        raise ObservabilityContractError(f"conflicting {field} values")
    return normalized[0]
