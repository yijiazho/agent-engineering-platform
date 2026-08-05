"""Deterministic, retry-safe scheduling for resolved Workflow Task DAGs."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import NAMESPACE_URL, uuid5

from aep.resource_loader import Resource, ResourceRef
from aep.runtime_store import RuntimeObject, RuntimeObjectStore
from aep.runtime_store import StatusConflictError
from aep.runtime_validation import is_rfc3339_timestamp
from aep.task_dag import TaskDagPlan, TaskPlanNode
from aep.task_execution import FailureClass, TaskExecutionLifecycle, TaskStatus
from aep.task_execution import InvalidTaskTransitionError
from aep.workflow_execution import _runtime_validator


class InvalidSchedulerInputError(ValueError):
    """Raised when scheduler inputs do not identify one valid execution plan."""


@dataclass(frozen=True)
class TaskExecutionResult:
    """Provider-neutral result returned by a Task executor."""

    succeeded: bool
    failure_class: FailureClass | None = None
    message: str | None = None

    @classmethod
    def success(cls) -> TaskExecutionResult:
        return cls(succeeded=True)

    @classmethod
    def failure(
        cls, classification: FailureClass, message: str
    ) -> TaskExecutionResult:
        if not isinstance(classification, FailureClass):
            raise TypeError("classification must be a FailureClass")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("failure message must not be empty")
        return cls(
            succeeded=False,
            failure_class=classification,
            message=message.strip(),
        )

    def validate(self) -> None:
        if self.succeeded:
            if self.failure_class is not None or self.message is not None:
                raise ValueError("successful Task result cannot contain failure details")
            return
        if self.failure_class is None or not self.message:
            raise ValueError("failed Task result requires classification and message")


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
        workflow_id, trace_id = _validate_inputs(
            plan, workflow_execution, self._store
        )
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
            elif _retry_is_ready(latest, self._max_attempts):
                ready.append((node, latest, dependency_ids))

        scheduled: list[RuntimeObject] = []
        for node, latest, dependency_ids in ready:
            if latest is None:
                attempt = self._create_attempt(
                    node,
                    workflow_id=workflow_id,
                    trace_id=trace_id,
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
                running = self._lifecycle.start(
                    str(attempt["id"]), timestamp=timestamp
                )
            except (InvalidTaskTransitionError, StatusConflictError):
                # Another reconciler acquired this PENDING attempt atomically.
                continue
            except Exception:
                # Without owner-token evidence, an ambiguous RUNNING record may
                # belong to another live reconciler and must not be reclaimed.
                continue
            try:
                self._emit(
                    running,
                    "TaskExecutionStarted",
                    sequence=2,
                    timestamp=timestamp,
                )
            except Exception as error:
                self._fail_running(
                    running,
                    FailureClass.RECOVERABLE,
                    f"could not record TaskExecutionStarted: {type(error).__name__}",
                    timestamp,
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
                terminal = self._complete_success(running, timestamp)
                event_type = "TaskExecutionSucceeded"
            else:
                terminal = self._fail_running(
                    running,
                    result.failure_class,  # type: ignore[arg-type]
                    result.message or "Task execution failed",
                    timestamp,
                )
                event_type = "TaskExecutionFailed"
            self._emit(terminal, event_type, sequence=3, timestamp=timestamp)

        persisted = tuple(
            self._store.get(str(attempt["id"])) for attempt in scheduled
        )
        return SchedulerReconciliation(
            task_executions=tuple(
                attempt for attempt in persisted if attempt is not None
            )
        )

    def _create_attempt(
        self,
        node: TaskPlanNode,
        *,
        workflow_id: str,
        trace_id: str,
        attempt: int,
        dependency_ids: tuple[str, ...],
        timestamp: str,
    ) -> RuntimeObject:
        task_ref = _ref_record(node.task_ref)
        return self._lifecycle.create(
            execution_id=_task_execution_id(workflow_id, node.task_ref, attempt),
            workflow_execution_id=workflow_id,
            task_ref=task_ref,
            attempt=attempt,
            trace_id=trace_id,
            timestamp=timestamp,
            provenance={
                "actor": "workflow-scheduler",
                "workflowExecutionId": workflow_id,
                "resourceRefs": [_ref_record(node.task_ref)],
            },
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
        _validate_runtime_record(event, "executionevent.schema.json")
        return self._store.append_event(event)

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
    ) -> RuntimeObject:
        try:
            return self._lifecycle.fail(
                str(running["id"]),
                classification=classification,
                message=message,
                timestamp=timestamp,
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
) -> tuple[str, str]:
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
    return workflow_id, trace_id


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


def _retry_is_ready(attempt: RuntimeObject, max_attempts: int) -> bool:
    failure = attempt.get("failure")
    return (
        attempt.get("status") == TaskStatus.FAILED.value
        and isinstance(failure, Mapping)
        and failure.get("class") == FailureClass.RECOVERABLE.value
        and int(attempt["attempt"]) < max_attempts
    )


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
