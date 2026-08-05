from concurrent.futures import ThreadPoolExecutor

import pytest

from aep.runtime_store import InMemoryRuntimeObjectStore, StatusConflictError
from aep.task_execution import (
    FailureClass,
    InvalidTaskReferenceError,
    InvalidTaskTransitionError,
    TaskExecutionLifecycle,
    TaskRetryNotAllowedError,
)


CREATED = "2026-07-11T00:00:00Z"
STARTED = "2026-07-11T00:00:01Z"
COMPLETED = "2026-07-11T00:00:02Z"


def lifecycle_with_pending():
    store = InMemoryRuntimeObjectStore()
    lifecycle = TaskExecutionLifecycle(store)
    execution = lifecycle.create(
        execution_id="taskexecution-123456789abc",
        workflow_execution_id="workflowexecution-123456789abc",
        task_ref={"kind": "Task", "name": "analyze-issue", "version": "1.0.0"},
        attempt=1,
        trace_id="trace-123",
        timestamp=CREATED,
        provenance={
            "actor": "workflow-runtime",
            "workflowExecutionId": "workflowexecution-123456789abc",
            "resourceRefs": [],
        },
    )
    return store, lifecycle, execution


def test_success_records_attempt_timestamps_and_trace_provenance() -> None:
    _, lifecycle, pending = lifecycle_with_pending()

    running = lifecycle.start(pending["id"], timestamp=STARTED)
    succeeded = lifecycle.succeed(pending["id"], timestamp=COMPLETED)

    assert pending["attempt"] == 1
    assert running["startedAt"] == STARTED
    assert succeeded["status"] == "SUCCEEDED"
    assert succeeded["completedAt"] == COMPLETED
    assert succeeded["traceId"] == "trace-123"
    assert succeeded["provenance"]["actor"] == "workflow-runtime"


def test_attempt_must_be_a_positive_integer() -> None:
    _, lifecycle, _ = lifecycle_with_pending()

    with pytest.raises(ValueError, match="attempt must be positive"):
        lifecycle.create(
            execution_id="taskexecution-abcdef123456",
            workflow_execution_id="workflowexecution-123456789abc",
            task_ref={"kind": "Task", "name": "analyze-issue", "version": "1.0.0"},
            attempt=True,
            trace_id="trace-123",
            timestamp=CREATED,
            provenance={"actor": "workflow-runtime", "resourceRefs": []},
        )


@pytest.mark.parametrize(
    "timestamp",
    ["not-a-timestamp", "2026-08-04T20:00:00+0000", "2026-08-04T20:00:00+00:00:30"],
)
def test_schema_invalid_timestamp_is_rejected_before_persistence(
    timestamp: str,
) -> None:
    store = InMemoryRuntimeObjectStore()
    lifecycle = TaskExecutionLifecycle(store)

    with pytest.raises(ValueError, match=r"invalid TaskExecution at \$.createdAt"):
        lifecycle.create(
            execution_id="taskexecution-abcdef123456",
            workflow_execution_id="workflowexecution-123456789abc",
            task_ref={"kind": "Task", "name": "analyze-issue", "version": "1.0.0"},
            attempt=1,
            trace_id="trace-123",
            timestamp=timestamp,
            provenance={"actor": "workflow-runtime", "resourceRefs": []},
        )

    assert store.get("taskexecution-abcdef123456") is None


@pytest.mark.parametrize(
    "task_ref",
    [
        {"kind": "Task", "name": "analyze-issue"},
        {"kind": "Task", "name": "analyze-issue", "version": "latest"},
        {"kind": "Agent", "name": "analyze-issue", "version": "1.0.0"},
    ],
)
def test_task_reference_must_be_versioned_and_task_specific(
    task_ref: dict[str, str],
) -> None:
    store = InMemoryRuntimeObjectStore()
    lifecycle = TaskExecutionLifecycle(store)

    with pytest.raises(InvalidTaskReferenceError):
        lifecycle.create(
            execution_id="taskexecution-abcdef123456",
            workflow_execution_id="workflowexecution-123456789abc",
            task_ref=task_ref,
            attempt=1,
            trace_id="trace-123",
            timestamp=CREATED,
            provenance={"actor": "workflow-runtime", "resourceRefs": []},
        )

    assert store.get("taskexecution-abcdef123456") is None


def test_task_versions_have_distinct_idempotency_keys() -> None:
    store = InMemoryRuntimeObjectStore()
    lifecycle = TaskExecutionLifecycle(store)
    common = {
        "workflow_execution_id": "workflowexecution-123456789abc",
        "attempt": 1,
        "trace_id": "trace-123",
        "timestamp": CREATED,
        "provenance": {"actor": "workflow-runtime", "resourceRefs": []},
    }

    version_one = lifecycle.create(
        execution_id="taskexecution-123456789abc",
        task_ref={"kind": "Task", "name": "analyze-issue", "version": "1.0.0"},
        **common,
    )
    version_two = lifecycle.create(
        execution_id="taskexecution-abcdef123456",
        task_ref={"kind": "Task", "name": "analyze-issue", "version": "2.0.0"},
        **common,
    )

    assert version_one["id"] != version_two["id"]
    assert version_one["taskRef"]["version"] == "1.0.0"
    assert version_two["taskRef"]["version"] == "2.0.0"


def test_cancellation_and_approval_waiting_are_explicit_transitions() -> None:
    _, lifecycle, pending = lifecycle_with_pending()
    lifecycle.start(pending["id"], timestamp=STARTED)

    waiting = lifecycle.await_approval(pending["id"], timestamp=COMPLETED)
    resumed = lifecycle.resume(pending["id"], timestamp=COMPLETED)
    cancelled = lifecycle.cancel(pending["id"], timestamp=COMPLETED)

    assert waiting["status"] == "AWAITING_APPROVAL"
    assert resumed["status"] == "RUNNING"
    assert cancelled["status"] == "CANCELLED"


@pytest.mark.parametrize(
    "classification",
    [
        FailureClass.RECOVERABLE,
        FailureClass.CONFIGURATION,
        FailureClass.EVALUATION,
        FailureClass.POLICY,
    ],
)
def test_failure_classes_are_persisted(classification: FailureClass) -> None:
    store, lifecycle, pending = lifecycle_with_pending()
    lifecycle.start(pending["id"], timestamp=STARTED)

    failed = lifecycle.fail(
        pending["id"],
        classification=classification,
        message=f"{classification.value} failure",
        timestamp=COMPLETED,
    )
    failed["failure"]["message"] = "mutated snapshot"

    persisted = store.get(pending["id"])
    assert persisted["failure"]["class"] == classification.value
    assert persisted["failure"]["retryable"] is (
        classification is FailureClass.RECOVERABLE
    )
    assert persisted["failure"]["message"] != "mutated snapshot"


def test_recoverable_failure_retries_as_a_new_attempt() -> None:
    _, lifecycle, pending = lifecycle_with_pending()
    lifecycle.start(pending["id"], timestamp=STARTED)
    lifecycle.fail(
        pending["id"],
        classification=FailureClass.RECOVERABLE,
        message="temporary failure",
        timestamp=COMPLETED,
    )

    retry = lifecycle.retry(
        pending["id"],
        new_execution_id="taskexecution-abcdef123456",
        timestamp="2026-07-11T00:01:00Z",
    )

    assert retry["attempt"] == 2
    assert retry["status"] == "PENDING"
    assert retry["provenance"]["parentId"] == pending["id"]


def test_policy_denial_cannot_retry() -> None:
    _, lifecycle, pending = lifecycle_with_pending()
    lifecycle.start(pending["id"], timestamp=STARTED)
    lifecycle.fail(
        pending["id"],
        classification=FailureClass.POLICY,
        message="capability denied",
        timestamp=COMPLETED,
    )

    with pytest.raises(TaskRetryNotAllowedError):
        lifecycle.retry(
            pending["id"],
            new_execution_id="taskexecution-abcdef123456",
            timestamp=COMPLETED,
        )


def test_invalid_and_terminal_transitions_are_rejected() -> None:
    _, lifecycle, pending = lifecycle_with_pending()

    with pytest.raises(InvalidTaskTransitionError):
        lifecycle.succeed(pending["id"], timestamp=COMPLETED)

    lifecycle.start(pending["id"], timestamp=STARTED)
    lifecycle.succeed(pending["id"], timestamp=COMPLETED)
    with pytest.raises(InvalidTaskTransitionError):
        lifecycle.cancel(pending["id"], timestamp=COMPLETED)


def test_concurrent_completion_uses_optimistic_status_check() -> None:
    _, lifecycle, pending = lifecycle_with_pending()
    lifecycle.start(pending["id"], timestamp=STARTED)

    def complete(outcome: str) -> str:
        try:
            if outcome == "success":
                lifecycle.succeed(pending["id"], timestamp=COMPLETED)
            else:
                lifecycle.fail(
                    pending["id"],
                    classification=FailureClass.EVALUATION,
                    message="tests failed",
                    timestamp=COMPLETED,
                )
            return outcome
        except (InvalidTaskTransitionError, StatusConflictError):
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(complete, ["success", "failure"]))

    assert results.count("conflict") == 1
