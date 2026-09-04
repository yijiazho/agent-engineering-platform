"""TaskExecution lifecycle and retry boundary."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from enum import Enum
from functools import cache
import json
from pathlib import Path
from typing import Any, Final

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource as SchemaResource
from referencing.jsonschema import DRAFT202012

from aep.runtime_store import RuntimeObject, RuntimeObjectStore
from aep.runtime_validation import is_rfc3339_timestamp
from aep.observability import (
    CorrelationContext,
    StructuredLifecycleLogger,
    bind_correlation,
)


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"


class FailureClass(str, Enum):
    RECOVERABLE = "RECOVERABLE"
    CONFIGURATION = "CONFIGURATION"
    EVALUATION = "EVALUATION"
    POLICY = "POLICY"
    PERMANENT = "PERMANENT"


TERMINAL_TASK_STATUSES: Final = frozenset(
    {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)

TRANSITIONS: Final = {
    TaskStatus.PENDING: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED}),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.AWAITING_APPROVAL,
        }
    ),
    TaskStatus.AWAITING_APPROVAL: frozenset(
        {TaskStatus.RUNNING, TaskStatus.FAILED, TaskStatus.CANCELLED}
    ),
    TaskStatus.SUCCEEDED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}


class InvalidTaskTransitionError(ValueError):
    """Raised when a TaskExecution lifecycle transition is not permitted."""


class TaskRetryNotAllowedError(ValueError):
    """Raised when retry is requested for a non-recoverable attempt."""


class InvalidTaskReferenceError(ValueError):
    """Raised when a TaskExecution does not bind an immutable Task reference."""


class TaskExecutionLifecycle:
    """Persist explicit TaskExecution state changes through a runtime store."""

    def __init__(
        self,
        store: RuntimeObjectStore,
        *,
        lifecycle_logger: StructuredLifecycleLogger | None = None,
    ) -> None:
        self._store = store
        self._lifecycle_logger = lifecycle_logger

    def create(
        self,
        *,
        execution_id: str,
        workflow_execution_id: str,
        task_ref: Mapping[str, Any],
        attempt: int,
        correlation: CorrelationContext | Mapping[str, Any],
        timestamp: str,
        provenance: Mapping[str, Any],
        dependency_task_execution_ids: tuple[str, ...] = (),
    ) -> RuntimeObject:
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
            raise ValueError("attempt must be positive")
        _validate_task_ref(task_ref)
        context = bind_correlation(
            correlation,
            task_execution_id=execution_id,
            provenance=provenance,
        )
        if context.workflow_execution_id != workflow_execution_id:
            raise ValueError(
                "workflow_execution_id conflicts with the correlation context"
            )
        record = {
            "apiVersion": "aep.dev/v1alpha1",
            "kind": "TaskExecution",
            "id": execution_id,
            "traceId": context.trace_id,
            "createdAt": timestamp,
            "updatedAt": timestamp,
            "provenance": deepcopy(dict(provenance)),
            "workflowExecutionId": workflow_execution_id,
            "taskRef": deepcopy(dict(task_ref)),
            "attempt": attempt,
            "status": TaskStatus.PENDING.value,
            "dependencyTaskExecutionIds": list(dependency_task_execution_ids),
        }
        task_identity = ":".join(
            str(task_ref[field]) for field in ("kind", "name", "version")
        )
        _validate_task_execution(record)
        key = f"task-execution:{workflow_execution_id}:{task_identity}:{attempt}"
        created = self._store.create(record, deterministic_key=key)
        self._emit("TaskExecutionQueued", created, timestamp)
        return created

    def start(self, execution_id: str, *, timestamp: str) -> RuntimeObject:
        return self._transition(
            execution_id,
            TaskStatus.RUNNING,
            timestamp=timestamp,
            changes={"startedAt": timestamp},
        )

    def succeed(self, execution_id: str, *, timestamp: str) -> RuntimeObject:
        return self._transition(execution_id, TaskStatus.SUCCEEDED, timestamp=timestamp)

    def cancel(self, execution_id: str, *, timestamp: str) -> RuntimeObject:
        return self._transition(execution_id, TaskStatus.CANCELLED, timestamp=timestamp)

    def await_approval(self, execution_id: str, *, timestamp: str) -> RuntimeObject:
        return self._transition(
            execution_id, TaskStatus.AWAITING_APPROVAL, timestamp=timestamp
        )

    def resume(self, execution_id: str, *, timestamp: str) -> RuntimeObject:
        return self._transition(execution_id, TaskStatus.RUNNING, timestamp=timestamp)

    def fail(
        self,
        execution_id: str,
        *,
        classification: FailureClass,
        message: str,
        timestamp: str,
        retry_not_before: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> RuntimeObject:
        if not message:
            raise ValueError("failure message must not be empty")
        failure = {
            "class": classification.value,
            "message": message,
            "retryable": classification is FailureClass.RECOVERABLE,
        }
        if retry_not_before is not None:
            if (
                classification is not FailureClass.RECOVERABLE
                or not is_rfc3339_timestamp(retry_not_before)
            ):
                raise ValueError(
                    "retry_not_before requires a recoverable failure and RFC3339 timestamp"
                )
            failure["retryNotBefore"] = retry_not_before
        if details is not None:
            failure["details"] = deepcopy(dict(details))
        return self._transition(
            execution_id,
            TaskStatus.FAILED,
            timestamp=timestamp,
            changes={"failure": failure},
        )

    def retry(
        self,
        execution_id: str,
        *,
        new_execution_id: str,
        timestamp: str,
    ) -> RuntimeObject:
        previous = self._require(execution_id)
        failure = previous.get("failure")
        if (
            previous.get("status") != TaskStatus.FAILED.value
            or not isinstance(failure, Mapping)
            or failure.get("class") != FailureClass.RECOVERABLE.value
        ):
            raise TaskRetryNotAllowedError(
                "only failed recoverable TaskExecutions may be retried"
            )
        provenance = deepcopy(dict(previous["provenance"]))
        provenance["parentId"] = execution_id
        previous_context = CorrelationContext.from_runtime_object(previous)
        return self.create(
            execution_id=new_execution_id,
            workflow_execution_id=previous["workflowExecutionId"],
            task_ref=previous["taskRef"],
            attempt=previous["attempt"] + 1,
            correlation=CorrelationContext(
                previous_context.trace_id,
                previous_context.workflow_execution_id,
                new_execution_id,
            ),
            timestamp=timestamp,
            provenance=provenance,
            dependency_task_execution_ids=tuple(
                previous.get("dependencyTaskExecutionIds", ())
            ),
        )

    def _transition(
        self,
        execution_id: str,
        target: TaskStatus,
        *,
        timestamp: str,
        changes: Mapping[str, Any] | None = None,
    ) -> RuntimeObject:
        current_record = self._require(execution_id)
        current = TaskStatus(current_record["status"])
        if target not in TRANSITIONS[current]:
            raise InvalidTaskTransitionError(
                f"TaskExecution cannot transition from {current.value} to {target.value}"
            )
        updated = self._store.update_status(
            execution_id,
            target.value,
            expected_status=current.value,
            updated_at=timestamp,
            changes=changes,
        )
        event_name = {
            TaskStatus.RUNNING: "TaskExecutionStarted",
            TaskStatus.SUCCEEDED: "TaskExecutionSucceeded",
            TaskStatus.FAILED: "TaskExecutionFailed",
            TaskStatus.CANCELLED: "TaskExecutionCancelled",
            TaskStatus.AWAITING_APPROVAL: "TaskExecutionAwaitingApproval",
        }[target]
        self._emit(event_name, updated, timestamp)
        return updated

    def _emit(
        self, event_name: str, runtime_object: RuntimeObject, timestamp: str
    ) -> None:
        if self._lifecycle_logger is not None:
            self._lifecycle_logger.emit(
                event_name=event_name,
                service="workflow-runtime",
                runtime_object=runtime_object,
                emitted_at=timestamp,
            )

    def _require(self, execution_id: str) -> RuntimeObject:
        record = self._store.get(execution_id)
        if record is None:
            raise ValueError(f"TaskExecution {execution_id!r} was not found")
        if record.get("kind") != "TaskExecution":
            raise ValueError(f"runtime object {execution_id!r} is not a TaskExecution")
        return record


def _validate_task_ref(task_ref: Mapping[str, Any]) -> None:
    errors = sorted(
        _task_ref_validator().iter_errors(dict(task_ref)),
        key=lambda error: (list(error.absolute_path), error.message),
    )
    if errors:
        error = errors[0]
        path = "$" + "".join(f".{part}" for part in error.absolute_path)
        raise InvalidTaskReferenceError(f"invalid Task reference at {path}: {error.message}")


def _validate_task_execution(record: Mapping[str, Any]) -> None:
    errors = sorted(
        _task_execution_validator().iter_errors(dict(record)),
        key=lambda error: (list(error.absolute_path), error.message),
    )
    if errors:
        error = errors[0]
        path = "$" + "".join(f".{part}" for part in error.absolute_path)
        raise ValueError(f"invalid TaskExecution at {path}: {error.message}")


@cache
def _task_ref_validator() -> Draft202012Validator:
    schema_path = (
        Path(__file__).parents[2]
        / "schemas"
        / "resources"
        / "v1"
        / "resource-definitions.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    registry = Registry().with_resource(
        schema["$id"],
        SchemaResource.from_contents(schema, default_specification=DRAFT202012),
    )
    return Draft202012Validator(
        {"$ref": f"{schema['$id']}#/$defs/taskRef"}, registry=registry
    )


@cache
def _task_execution_validator() -> Draft202012Validator:
    schema_root = Path(__file__).parents[2] / "schemas"
    schema_paths = (
        schema_root / "resources" / "v1" / "resource-definitions.schema.json",
        schema_root / "runtime" / "v1" / "runtime-definitions.schema.json",
        schema_root / "runtime" / "v1" / "taskexecution.schema.json",
    )
    schemas = [
        json.loads(schema_path.read_text(encoding="utf-8"))
        for schema_path in schema_paths
    ]
    registry = Registry().with_resources(
        (
            schema["$id"],
            SchemaResource.from_contents(schema, default_specification=DRAFT202012),
        )
        for schema in schemas
    )
    format_checker = FormatChecker()
    format_checker.checkers = dict(format_checker.checkers)
    format_checker.checkers["date-time"] = (is_rfc3339_timestamp, ())
    return Draft202012Validator(
        schemas[-1], registry=registry, format_checker=format_checker
    )
