"""Deterministic, retry-safe scheduling for resolved Workflow Task DAGs."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import NAMESPACE_URL, uuid5

from aep.resource_loader import Resource, ResourceRef
from aep.runtime_store import RuntimeObject, RuntimeObjectStore, RuntimeStoreError
from aep.runtime_store import StatusConflictError
from aep.runtime_validation import is_rfc3339_timestamp
from aep.task_dag import TaskDagPlan, TaskPlanNode
from aep.task_execution import FailureClass, TaskExecutionLifecycle, TaskStatus
from aep.task_execution import InvalidTaskTransitionError
from aep.workflow_execution import _runtime_validator


_FAILURE_DETAIL_FIELDS = frozenset({
    "reason", "path", "declaredMaxBytesHint", "blobSize",
    "appliedTrustedCeiling", "predicateType", "inspectionStrategy",
    "evaluationComplete", "operation", "phase", "category", "errno",
})


def _validate_failure_details(details: Mapping[str, Any]) -> None:
    if len(details) > 8:
        raise ValueError("failure details must contain at most eight properties")
    unexpected = set(details) - _FAILURE_DETAIL_FIELDS
    if unexpected:
        raise ValueError("failure details contain unapproved fields")
    for field in ("reason", "path", "predicateType", "inspectionStrategy"):
        value = details.get(field)
        if value is not None and (
            not isinstance(value, str) or not value or len(value) > 4096
        ):
            raise ValueError(f"failure details {field} must be bounded text or null")
    for field in ("operation", "phase", "category"):
        value = details.get(field)
        if value is not None and (
            not isinstance(value, str) or not value or len(value) > 128
        ):
            raise ValueError(f"failure details {field} must be bounded text or null")
    for field in ("declaredMaxBytesHint", "blobSize", "appliedTrustedCeiling"):
        value = details.get(field)
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value < 0
        ):
            raise ValueError(f"failure details {field} must be a non-negative integer or null")
    if "evaluationComplete" in details and not isinstance(
        details["evaluationComplete"], bool
    ):
        raise ValueError("failure details evaluationComplete must be boolean")
    if "errno" in details and details["errno"] is not None and (
        not isinstance(details["errno"], int)
        or isinstance(details["errno"], bool)
        or details["errno"] < 0
    ):
        raise ValueError("failure details errno must be a non-negative integer or null")


def _safe_persistence_details(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Allow only bounded store diagnostics into runtime failure evidence."""
    if not isinstance(value, Mapping):
        return {"operation": "append_execution_event", "phase": "unknown", "category": "store_error"}
    result = {}
    for key in ("operation", "phase", "category"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate:
            result[key] = candidate[:128]
    candidate_errno = value.get("errno")
    if (
        candidate_errno is None
        or isinstance(candidate_errno, int) and not isinstance(candidate_errno, bool)
        and candidate_errno >= 0
    ):
        result["errno"] = candidate_errno
    result.setdefault("operation", "append_execution_event")
    result.setdefault("phase", "unknown")
    result.setdefault("category", "store_error")
    return result


def _append_persistence_diagnostic(
    runtime_object: Mapping[str, Any], details: Mapping[str, Any]
) -> list[dict[str, Any]]:
    existing = runtime_object.get("persistenceDiagnostics", ())
    prior = [dict(item) for item in existing if isinstance(item, Mapping)]
    return [*prior, dict(details)][-8:]


def _can_recover_start(diagnostic: Mapping[str, Any]) -> bool:
    operation = diagnostic.get("operation")
    phase = diagnostic.get("phase")
    return (
        operation == "checkpoint"
        and phase in {
            "temporary_create", "temporary_write", "file_sync", "replace",
            "directory_sync",
        }
    ) or operation == "writer_fence" and phase == "release"


def _can_adopt_committed_start(diagnostic: Mapping[str, Any]) -> bool:
    return (
        diagnostic.get("operation") == "checkpoint"
        and diagnostic.get("phase") == "directory_sync"
    ) or (
        diagnostic.get("operation") == "writer_fence"
        and diagnostic.get("phase") == "release"
    )


class InvalidSchedulerInputError(ValueError):
    """Raised when scheduler inputs do not identify one valid execution plan."""


@dataclass(frozen=True)
class TaskExecutionResult:
    """Provider-neutral result returned by a Task executor."""

    succeeded: bool
    failure_class: FailureClass | None = None
    message: str | None = None
    retry_not_before: str | None = None
    details: Mapping[str, Any] | None = None

    @classmethod
    def success(cls) -> TaskExecutionResult:
        return cls(succeeded=True)

    @classmethod
    def failure(
        cls,
        classification: FailureClass,
        message: str,
        *,
        retry_not_before: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> TaskExecutionResult:
        if not isinstance(classification, FailureClass):
            raise TypeError("classification must be a FailureClass")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("failure message must not be empty")
        return cls(
            succeeded=False,
            failure_class=classification,
            message=message.strip(),
            retry_not_before=retry_not_before,
            details=dict(details) if details is not None else None,
        )

    def validate(self) -> None:
        if self.succeeded:
            if (
                self.failure_class is not None
                or self.message is not None
                or self.retry_not_before is not None
                or self.details is not None
            ):
                raise ValueError("successful Task result cannot contain failure details")
            return
        if self.failure_class is None or not self.message:
            raise ValueError("failed Task result requires classification and message")
        if self.details is not None and not isinstance(self.details, Mapping):
            raise ValueError("failure details must be an object")
        if self.details is not None:
            _validate_failure_details(self.details)
        if self.retry_not_before is not None and (
            self.failure_class is not FailureClass.RECOVERABLE
            or not is_rfc3339_timestamp(self.retry_not_before)
        ):
            raise ValueError(
                "retry_not_before requires a recoverable failure and RFC3339 timestamp"
            )


class TaskExecutor(Protocol):
    """Execution boundary implemented by Task handlers or deterministic fakes."""

    def execute(
        self, task: Resource, task_execution: RuntimeObject
    ) -> TaskExecutionResult:
        """Execute one already-started TaskExecution attempt."""


@dataclass(frozen=True)
class SchedulerReconciliation:
    """Task attempts created or resumed during one scheduler reconciliation."""

    task_executions: tuple[RuntimeObject, ...]


class WorkflowScheduler:
    """Advance one validated DAG by a single parallel-ready wave.

    Every reconciliation computes readiness from persisted TaskExecution state.
    All attempts in the ready wave are created before any executor is invoked.
    Callers reconcile again after the wave to schedule newly unblocked Tasks or
    recoverable retries.
    """

    def __init__(
        self,
        store: RuntimeObjectStore,
        executor: TaskExecutor,
        *,
        max_attempts: int = 1,
        clock: Callable[[], str],
    ) -> None:
        if not isinstance(max_attempts, int) or isinstance(max_attempts, bool):
            raise TypeError("max_attempts must be an integer")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self._store = store
        self._executor = executor
        self._max_attempts = max_attempts
        self._clock = clock
        self._lifecycle = TaskExecutionLifecycle(store)

    def reconcile(
        self,
        plan: TaskDagPlan,
        workflow_execution: Mapping[str, Any],
    ) -> SchedulerReconciliation:
        """Create and execute the next ready wave from persisted runtime state."""
        timestamp = self._timestamp()
        (
            workflow_id,
            trace_id,
            repository_revision,
            knowledge_graph_version,
        ) = _validate_inputs(
            plan, workflow_execution, self._store
        )
        self._ensure_resolved_task_plan(workflow_id, plan, timestamp)
        self._repair_attempt_evidence(workflow_id, timestamp)
        attempts_by_ref = _attempts_by_task(
            self._store.list_by_workflow_execution(workflow_id)
        )

        ready: list[tuple[TaskPlanNode, RuntimeObject | None, tuple[str, ...]]] = []
        for node in plan.nodes:
            attempts = attempts_by_ref.get(node.task_ref, ())
            latest = attempts[-1] if attempts else None
            dependency_ids = _succeeded_dependency_ids(node, attempts_by_ref)
            if dependency_ids is None:
                continue
            if latest is None:
                ready.append((node, None, dependency_ids))
            elif latest.get("status") == TaskStatus.PENDING.value:
                ready.append((node, latest, dependency_ids))
            elif _retry_is_ready(latest, self._max_attempts, timestamp):
                ready.append((node, latest, dependency_ids))

        scheduled: list[RuntimeObject] = []
        for node, latest, dependency_ids in ready:
            if latest is None:
                attempt = self._create_attempt(
                    node,
                    workflow_id=workflow_id,
                    trace_id=trace_id,
                    repository_revision=repository_revision,
                    knowledge_graph_version=knowledge_graph_version,
                    attempt=1,
                    dependency_ids=dependency_ids,
                    timestamp=timestamp,
                )
            elif latest.get("status") == TaskStatus.PENDING.value:
                attempt = latest
            else:
                next_attempt = int(latest["attempt"]) + 1
                attempt = self._lifecycle.retry(
                    str(latest["id"]),
                    new_execution_id=_task_execution_id(
                        workflow_id, node.task_ref, next_attempt
                    ),
                    timestamp=timestamp,
                )
            self._store.append_task_execution_id(
                workflow_id, str(attempt["id"]), updated_at=timestamp
            )
            self._emit(
                attempt, "TaskExecutionQueued", sequence=1, timestamp=timestamp
            )
            scheduled.append(attempt)

        for attempt in scheduled:
            try:
                running = self._transition_and_emit(
                    attempt, TaskStatus.RUNNING,
                    event_type="TaskExecutionStarted", timestamp=timestamp,
                    changes={"startedAt": timestamp},
                )
            except (InvalidTaskTransitionError, StatusConflictError):
                # Another reconciler acquired this PENDING attempt atomically.
                continue
            except Exception as error:
                if (
                    getattr(self._store, "supports_atomic_task_transitions", False)
                    and isinstance(error, RuntimeStoreError)
                    and error.diagnostic
                    and _can_recover_start(error.diagnostic)
                ):
                    running = self._recover_durable_start(
                        attempt, timestamp, error.diagnostic
                    )
                else:
                    # Without owner-token evidence, an ambiguous RUNNING record
                    # may belong to another live reconciler and is not reclaimed.
                    continue
            if not getattr(self._store, "supports_atomic_task_transitions", False):
                try:
                    self._emit(running, "TaskExecutionStarted", sequence=2, timestamp=timestamp)
                except Exception as error:
                    diagnostic = getattr(error, "diagnostic", {})
                    self._fail_running(
                        running, FailureClass.RECOVERABLE,
                        "ExecutionEvent persistence failure",
                        timestamp,
                        details=_safe_persistence_details(diagnostic),
                    )
                    raise
            node = plan.get_node(_ref_from_record(running["taskRef"]))
            if node is None:  # Plan validation above makes this defensive only.
                raise InvalidSchedulerInputError("TaskExecution is not present in plan")
            try:
                result = self._executor.execute(node.task, running)
                if not isinstance(result, TaskExecutionResult):
                    raise TypeError("Task executor must return TaskExecutionResult")
                result.validate()
            except Exception as error:
                result = TaskExecutionResult.failure(
                    FailureClass.RECOVERABLE,
                    f"Task executor failed: {type(error).__name__}",
                )
            if result.succeeded:
                terminal_changes: dict[str, Any] = {}
                event_type = "TaskExecutionSucceeded"
            else:
                failure: dict[str, Any] = {
                    "class": result.failure_class.value,  # type: ignore[union-attr]
                    "message": result.message or "Task execution failed",
                    "retryable": result.failure_class is FailureClass.RECOVERABLE,
                }
                if result.retry_not_before is not None:
                    failure["retryNotBefore"] = result.retry_not_before
                if result.details is not None:
                    failure["details"] = dict(result.details)
                terminal_changes = {"failure": failure}
                event_type = "TaskExecutionFailed"
            if getattr(self._store, "supports_atomic_task_transitions", False):
                running = self._persist_terminal_evidence(
                    running,
                    {
                        "status": "SUCCEEDED" if result.succeeded else "FAILED",
                        **terminal_changes,
                    },
                    timestamp,
                )
                terminal = self._transition_and_emit(
                    running,
                    TaskStatus.SUCCEEDED if result.succeeded else TaskStatus.FAILED,
                    event_type=event_type, timestamp=timestamp,
                    changes=terminal_changes,
                )
            elif result.succeeded:
                terminal = self._complete_success(running, timestamp)
                self._emit(terminal, event_type, sequence=3, timestamp=timestamp)
            else:
                terminal = self._fail_running(
                    running, result.failure_class, result.message or "Task execution failed",
                    timestamp, retry_not_before=result.retry_not_before,
                    details=result.details,
                )
                self._emit(terminal, event_type, sequence=3, timestamp=timestamp)

        persisted = tuple(
            self._store.get(str(attempt["id"])) for attempt in scheduled
        )
        return SchedulerReconciliation(
            task_executions=tuple(
                attempt for attempt in persisted if attempt is not None
            )
        )

    def _recover_durable_start(
        self,
        attempt: RuntimeObject,
        timestamp: str,
        diagnostic: Mapping[str, Any],
    ) -> RuntimeObject:
        """Recover one diagnosed start checkpoint before task dispatch."""
        details = _safe_persistence_details(diagnostic)
        persisted = self._store.get(str(attempt["id"]))
        if persisted is None:
            raise RuntimeStoreError(
                "durable start recovery lost its TaskExecution",
                diagnostic=details,
            )
        diagnostics = _append_persistence_diagnostic(persisted, details)
        if persisted.get("status") == TaskStatus.PENDING.value:
            return self._transition_and_emit(
                persisted, TaskStatus.RUNNING,
                event_type="TaskExecutionStarted", timestamp=timestamp,
                changes={
                    "startedAt": timestamp,
                    "persistenceDiagnostics": diagnostics,
                },
            )
        if (
            _can_adopt_committed_start(diagnostic)
            and persisted.get("status") == TaskStatus.RUNNING.value
            and self._task_event_exists(persisted, "TaskExecutionStarted")
        ):
            recovered = dict(persisted)
            recovered["persistenceDiagnostics"] = diagnostics
            return recovered
        raise RuntimeStoreError(
            "durable start checkpoint outcome is ambiguous",
            diagnostic=details,
        )

    def _persist_terminal_evidence(
        self,
        running: RuntimeObject,
        evidence: Mapping[str, Any],
        timestamp: str,
    ) -> RuntimeObject:
        """Persist post-handler evidence, recovering one transient checkpoint fault."""
        try:
            changes: dict[str, Any] = {"terminalEvidence": dict(evidence)}
            diagnostics = running.get("persistenceDiagnostics")
            if isinstance(diagnostics, list):
                changes["persistenceDiagnostics"] = [
                    dict(item) for item in diagnostics if isinstance(item, Mapping)
                ][-8:]
            return self._store.update_status(
                str(running["id"]), TaskStatus.RUNNING.value,
                expected_status=TaskStatus.RUNNING.value,
                updated_at=timestamp,
                changes=changes,
            )
        except RuntimeStoreError as error:
            if not error.diagnostic:
                raise
            details = _safe_persistence_details(error.diagnostic)
            persisted = self._store.get(str(running["id"]))
            if persisted is None or persisted.get("status") != TaskStatus.RUNNING.value:
                raise
            diagnostics = _append_persistence_diagnostic(persisted, details)
            changes: dict[str, Any] = {"persistenceDiagnostics": diagnostics}
            if persisted.get("terminalEvidence") != evidence:
                changes["terminalEvidence"] = dict(evidence)
            return self._store.update_status(
                str(persisted["id"]), TaskStatus.RUNNING.value,
                expected_status=TaskStatus.RUNNING.value,
                updated_at=timestamp, changes=changes,
            )

    def _task_event_exists(
        self, task_execution: RuntimeObject, event_type: str
    ) -> bool:
        return any(
            record.get("kind") == "ExecutionEvent"
            and record.get("eventType") == event_type
            for record in self._store.list_by_task_execution(
                str(task_execution["id"])
            )
        )

    def _ensure_resolved_task_plan(
        self, workflow_id: str, plan: TaskDagPlan, timestamp: str
    ) -> None:
        """Persist one immutable plan, rejecting any stale reconciler's plan."""

        expected = _resolved_task_plan(plan)
        persisted = self._store.get(workflow_id)
        if persisted is None:
            raise InvalidSchedulerInputError("WorkflowExecution must exist in the runtime store")
        existing = persisted.get("resolvedTaskPlan")
        if existing is not None:
            if existing != expected:
                raise InvalidSchedulerInputError("resolved task plan does not match persisted evidence")
            return
        candidate = dict(persisted)
        candidate["resolvedTaskPlan"] = expected
        candidate["updatedAt"] = timestamp
        _validate_runtime_record(candidate, "workflowexecution.schema.json")
        try:
            self._store.update_status(
                workflow_id, str(persisted["status"]),
                expected_status=str(persisted["status"]), updated_at=timestamp,
                changes={"resolvedTaskPlan": expected},
            )
        except ValueError:
            # A concurrent reconciler may have persisted the write-once field.
            persisted = self._store.get(workflow_id)
            if persisted is None or persisted.get("resolvedTaskPlan") != expected:
                raise InvalidSchedulerInputError("resolved task plan does not match persisted evidence") from None

    def _create_attempt(
        self,
        node: TaskPlanNode,
        *,
        workflow_id: str,
        trace_id: str,
        repository_revision: str,
        knowledge_graph_version: object,
        attempt: int,
        dependency_ids: tuple[str, ...],
        timestamp: str,
        retry_not_before: str | None = None,
    ) -> RuntimeObject:
        task_ref = _ref_record(node.task_ref)
        task_execution_id = _task_execution_id(
            workflow_id, node.task_ref, attempt
        )
        provenance = {
            "actor": "workflow-scheduler",
            "workflowExecutionId": workflow_id,
            "repositoryRevision": repository_revision,
            "resourceRefs": [_ref_record(node.task_ref)],
        }
        if isinstance(knowledge_graph_version, str) and knowledge_graph_version:
            provenance["knowledgeGraphVersion"] = knowledge_graph_version
        return self._lifecycle.create(
            execution_id=task_execution_id,
            workflow_execution_id=workflow_id,
            task_ref=task_ref,
            attempt=attempt,
            correlation={
                "traceId": trace_id,
                "workflowExecutionId": workflow_id,
                "taskExecutionId": task_execution_id,
            },
            timestamp=timestamp,
            provenance=provenance,
            dependency_task_execution_ids=dependency_ids,
        )

    def _emit(
        self,
        task_execution: RuntimeObject,
        event_type: str,
        *,
        sequence: int,
        timestamp: str,
    ) -> RuntimeObject:
        event = self._event_record(task_execution, event_type, sequence=sequence, timestamp=timestamp)
        _validate_runtime_record(event, "executionevent.schema.json")
        return self._store.append_event(event)

    def _event_record(
        self,
        task_execution: RuntimeObject,
        event_type: str,
        *,
        sequence: int,
        timestamp: str,
    ) -> dict[str, Any]:
        task_execution_id = str(task_execution["id"])
        event_id = _event_id(task_execution_id, event_type)
        event = {
            "apiVersion": "aep.dev/v1alpha1",
            "kind": "ExecutionEvent",
            "id": event_id,
            "traceId": task_execution["traceId"],
            "createdAt": timestamp,
            "updatedAt": timestamp,
            "provenance": {
                "actor": "workflow-scheduler",
                "workflowExecutionId": task_execution["workflowExecutionId"],
                "taskExecutionId": task_execution_id,
                "resourceRefs": [dict(task_execution["taskRef"])],
            },
            "eventType": event_type,
            "subject": {"kind": "TaskExecution", "id": task_execution_id},
            "sequence": sequence,
            "emittedAt": timestamp,
            "payload": {
                "status": {
                    "TaskExecutionQueued": "PENDING",
                    "TaskExecutionStarted": "RUNNING",
                    "TaskExecutionSucceeded": "SUCCEEDED",
                    "TaskExecutionFailed": "FAILED",
                }[event_type],
                "attempt": task_execution["attempt"],
            },
        }
        failure = task_execution.get("failure")
        if event_type == "TaskExecutionFailed" and isinstance(failure, Mapping):
            event["payload"]["failureClass"] = failure["class"]
            event["payload"]["retryable"] = failure.get("retryable", False)
        return event

    def _transition_and_emit(
        self,
        task_execution: RuntimeObject,
        status: TaskStatus,
        *,
        event_type: str,
        timestamp: str,
        changes: Mapping[str, Any] | None = None,
    ) -> RuntimeObject:
        """Commit a lifecycle transition and its audit event in one checkpoint."""
        if not getattr(self._store, "supports_atomic_task_transitions", False):
            if status is TaskStatus.RUNNING:
                updated = self._lifecycle.start(str(task_execution["id"]), timestamp=timestamp)
                return updated
            if status is TaskStatus.SUCCEEDED:
                updated = self._complete_success(task_execution, timestamp)
            else:
                failure = (changes or {}).get("failure", {})
                updated = self._fail_running(
                    task_execution,
                    FailureClass(str(failure.get("class", FailureClass.RECOVERABLE.value))),
                    str(failure.get("message", "Task execution failed")), timestamp,
                    retry_not_before=failure.get("retryNotBefore"),
                    details=failure.get("details"),
                )
            self._emit(updated, event_type, sequence=3, timestamp=timestamp)
            return updated

        preview = dict(task_execution)
        preview.update(dict(changes or {}))
        preview["status"] = status.value
        preview["updatedAt"] = timestamp
        if status is TaskStatus.RUNNING:
            preview.setdefault("startedAt", timestamp)
        if status in {TaskStatus.SUCCEEDED, TaskStatus.FAILED}:
            preview.setdefault("completedAt", timestamp)
        event = self._event_record(
            preview, event_type,
            sequence=2 if status is TaskStatus.RUNNING else 3,
            timestamp=timestamp,
        )
        _validate_runtime_record(event, "executionevent.schema.json")
        return self._store.commit_task_execution_transition(
            str(task_execution["id"]), status.value,
            workflow_execution_id=str(task_execution["workflowExecutionId"]),
            event=event, expected_status=str(task_execution["status"]),
            updated_at=timestamp, changes=changes,
        )

    def _repair_attempt_evidence(self, workflow_id: str, timestamp: str) -> None:
        records = self._store.list_by_workflow_execution(workflow_id)
        for attempt in records:
            if attempt.get("kind") != "TaskExecution":
                continue
            self._store.append_task_execution_id(
                workflow_id, str(attempt["id"]), updated_at=timestamp
            )
            self._emit(
                attempt, "TaskExecutionQueued", sequence=1, timestamp=timestamp
            )
            if attempt.get("startedAt") is not None:
                self._emit(
                    attempt,
                    "TaskExecutionStarted",
                    sequence=2,
                    timestamp=timestamp,
                )
            evidence = attempt.get("terminalEvidence")
            if attempt.get("status") == TaskStatus.RUNNING.value and isinstance(evidence, Mapping):
                terminal_status = evidence.get("status")
                if terminal_status in {TaskStatus.SUCCEEDED.value, TaskStatus.FAILED.value}:
                    changes = {key: value for key, value in evidence.items() if key != "status"}
                    repaired = self._store.update_status(
                        str(attempt["id"]), terminal_status,
                        expected_status=TaskStatus.RUNNING.value,
                        updated_at=timestamp, changes=changes,
                    )
                    attempt = repaired
            status = attempt.get("status")
            event_type = {
                TaskStatus.SUCCEEDED.value: "TaskExecutionSucceeded",
                TaskStatus.FAILED.value: "TaskExecutionFailed",
            }.get(status)
            if event_type is not None:
                self._emit(attempt, event_type, sequence=3, timestamp=timestamp)

    def _complete_success(
        self, running: RuntimeObject, timestamp: str
    ) -> RuntimeObject:
        try:
            return self._lifecycle.succeed(str(running["id"]), timestamp=timestamp)
        except Exception as error:
            persisted = self._store.get(str(running["id"]))
            if persisted is not None and persisted.get("status") == "SUCCEEDED":
                return persisted
            if persisted is not None and persisted.get("status") == "RUNNING":
                return self._fail_running(
                    persisted,
                    FailureClass.RECOVERABLE,
                    f"could not persist Task success: {type(error).__name__}",
                    timestamp,
                )
            raise

    def _fail_running(
        self,
        running: RuntimeObject,
        classification: FailureClass,
        message: str,
        timestamp: str,
        retry_not_before: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> RuntimeObject:
        try:
            return self._lifecycle.fail(
                str(running["id"]),
                classification=classification,
                message=message,
                timestamp=timestamp,
                retry_not_before=retry_not_before,
                details=details,
            )
        except Exception:
            persisted = self._store.get(str(running["id"]))
            if persisted is not None and persisted.get("status") == "FAILED":
                return persisted
            raise

    def _timestamp(self) -> str:
        timestamp = self._clock()
        if not isinstance(timestamp, str) or not is_rfc3339_timestamp(timestamp):
            raise InvalidSchedulerInputError(
                "clock must return an RFC3339 timestamp"
            )
        return timestamp


def _validate_inputs(
    plan: TaskDagPlan,
    workflow_execution: Mapping[str, Any],
    store: RuntimeObjectStore,
) -> tuple[str, str, str, object]:
    if not isinstance(plan, TaskDagPlan):
        raise TypeError("plan must be a TaskDagPlan")
    if not isinstance(workflow_execution, Mapping):
        raise TypeError("workflow_execution must be a mapping")
    if workflow_execution.get("kind") != "WorkflowExecution":
        raise InvalidSchedulerInputError(
            "workflow_execution must be a WorkflowExecution"
        )
    _validate_runtime_record(workflow_execution, "workflowexecution.schema.json")
    workflow_id = workflow_execution.get("id")
    if not isinstance(workflow_id, str) or not workflow_id:
        raise InvalidSchedulerInputError("WorkflowExecution id is required")
    persisted = store.get(workflow_id)
    if persisted is None or persisted.get("kind") != "WorkflowExecution":
        raise InvalidSchedulerInputError(
            "WorkflowExecution must exist in the runtime store"
        )
    _validate_runtime_record(persisted, "workflowexecution.schema.json")
    for field in (
        "id",
        "traceId",
        "workflowRef",
        "eventRef",
        "repositoryRevision",
        "knowledgeGraphVersion",
        "provenance",
    ):
        if workflow_execution.get(field) != persisted.get(field):
            raise InvalidSchedulerInputError(
                f"caller WorkflowExecution {field} does not match persisted evidence"
            )
    if persisted.get("status") != "RUNNING":
        raise InvalidSchedulerInputError("WorkflowExecution must be RUNNING")
    if _ref_from_record(persisted.get("workflowRef")) != plan.workflow_ref:
        raise InvalidSchedulerInputError(
            "WorkflowExecution workflowRef does not match the resolved plan"
        )
    trace_id = persisted.get("traceId")
    if not isinstance(trace_id, str) or not trace_id:
        raise InvalidSchedulerInputError("WorkflowExecution traceId is required")
    repository_revision = persisted.get("repositoryRevision")
    if not isinstance(repository_revision, str) or not repository_revision:
        raise InvalidSchedulerInputError(
            "WorkflowExecution repositoryRevision is required"
        )
    return (
        workflow_id,
        trace_id,
        repository_revision,
        persisted.get("knowledgeGraphVersion"),
    )


def _resolved_task_plan(plan: TaskDagPlan) -> list[dict[str, Any]]:
    return [
        {
            "taskRef": _ref_record(node.task_ref),
            "dependencies": [_ref_record(ref) for ref in node.dependencies],
        }
        for node in plan.nodes
    ]


def _validate_runtime_record(record: Mapping[str, Any], schema_name: str) -> None:
    errors = sorted(
        _runtime_validator(schema_name).iter_errors(dict(record)),
        key=lambda error: (list(error.absolute_path), error.message),
    )
    if errors:
        error = errors[0]
        path = "$" + "".join(f".{part}" for part in error.absolute_path)
        raise InvalidSchedulerInputError(
            f"invalid {record.get('kind', 'runtime object')} at {path}: {error.message}"
        )


def _attempts_by_task(
    records: tuple[RuntimeObject, ...],
) -> dict[ResourceRef, tuple[RuntimeObject, ...]]:
    grouped: dict[ResourceRef, list[RuntimeObject]] = {}
    for record in records:
        if record.get("kind") != "TaskExecution":
            continue
        ref = _ref_from_record(record.get("taskRef"))
        if ref is None:
            continue
        grouped.setdefault(ref, []).append(record)
    result: dict[ResourceRef, tuple[RuntimeObject, ...]] = {}
    for ref, attempts in grouped.items():
        attempts.sort(key=lambda value: int(value["attempt"]))
        result[ref] = tuple(attempts)
    return result


def _succeeded_dependency_ids(
    node: TaskPlanNode,
    attempts_by_ref: Mapping[ResourceRef, tuple[RuntimeObject, ...]],
) -> tuple[str, ...] | None:
    dependency_ids: list[str] = []
    for dependency_ref in node.dependencies:
        attempts = attempts_by_ref.get(dependency_ref, ())
        succeeded = next(
            (
                attempt
                for attempt in reversed(attempts)
                if attempt.get("status") == TaskStatus.SUCCEEDED.value
            ),
            None,
        )
        if succeeded is None:
            return None
        dependency_ids.append(str(succeeded["id"]))
    return tuple(dependency_ids)


def _retry_is_ready(
    attempt: RuntimeObject, max_attempts: int, timestamp: str
) -> bool:
    failure = attempt.get("failure")
    not_before = failure.get("retryNotBefore") if isinstance(failure, Mapping) else None
    return (
        attempt.get("status") == TaskStatus.FAILED.value
        and isinstance(failure, Mapping)
        and failure.get("class") == FailureClass.RECOVERABLE.value
        and int(attempt["attempt"]) < max_attempts
        and (
            not isinstance(not_before, str)
            or _parse_timestamp(timestamp) >= _parse_timestamp(not_before)
        )
    )


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _task_execution_id(
    workflow_id: str, task_ref: ResourceRef, attempt: int
) -> str:
    identity = f"{workflow_id}:{task_ref.kind}/{task_ref.name}:{task_ref.version}:{attempt}"
    return f"taskexecution-{uuid5(NAMESPACE_URL, f'task-execution:{identity}')}"


def _event_id(task_execution_id: str, event_type: str) -> str:
    value = uuid5(
        NAMESPACE_URL, f"execution-event:{task_execution_id}:{event_type}"
    )
    return f"executionevent-{value}"


def _ref_from_record(value: Any) -> ResourceRef | None:
    if not isinstance(value, Mapping):
        return None
    try:
        return ResourceRef.from_mapping(dict(value))
    except (KeyError, TypeError, ValueError):
        return None


def _ref_record(ref: ResourceRef) -> dict[str, str]:
    return {"kind": ref.kind, "name": ref.name, "version": ref.version}
