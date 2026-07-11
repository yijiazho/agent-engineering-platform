from concurrent.futures import ThreadPoolExecutor

import pytest

from aep.runtime_store import (
    ImmutableRuntimeObjectError,
    InMemoryRuntimeObjectStore,
    RuntimeObjectAlreadyExistsError,
    StatusConflictError,
)


WORKFLOW_ID = "workflowexecution-123456789abc"


def runtime_object(object_id: str, *, status: str = "PENDING") -> dict[str, object]:
    return {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "TaskExecution",
        "id": object_id,
        "traceId": "trace-123",
        "createdAt": "2026-07-10T00:00:00Z",
        "updatedAt": "2026-07-10T00:00:00Z",
        "workflowExecutionId": WORKFLOW_ID,
        "status": status,
        "evidence": {"output": "original"},
    }


def test_create_is_idempotent_by_deterministic_key() -> None:
    store = InMemoryRuntimeObjectStore()
    first = store.create(runtime_object("taskexecution-123456789abc"), deterministic_key="event:task")
    second_value = runtime_object("taskexecution-abcdef123456")
    second_value["status"] = "RUNNING"

    second = store.create(second_value, deterministic_key="event:task")

    assert second == first
    assert store.get("taskexecution-abcdef123456") is None


def test_claim_is_atomic_and_returns_the_first_value() -> None:
    store = InMemoryRuntimeObjectStore()

    first = store.claim("event:delivery-123", {"id": "event-first"})
    duplicate = store.claim("event:delivery-123", {"id": "event-duplicate"})

    assert first == (True, {"id": "event-first"})
    assert duplicate == (False, {"id": "event-first"})


def test_duplicate_id_with_another_key_is_rejected() -> None:
    store = InMemoryRuntimeObjectStore()
    value = runtime_object("taskexecution-123456789abc")
    store.create(value, deterministic_key="first")

    with pytest.raises(RuntimeObjectAlreadyExistsError):
        store.create(value, deterministic_key="second")


def test_completed_object_and_returned_evidence_are_immutable() -> None:
    store = InMemoryRuntimeObjectStore()
    object_id = "taskexecution-123456789abc"
    source = runtime_object(object_id, status="RUNNING")
    created = store.create(source, deterministic_key="task")
    source["evidence"] = {"output": "changed"}

    completed = store.update_status(object_id, "SUCCEEDED", expected_status="RUNNING")

    assert created["evidence"] == {"output": "original"}
    assert completed["completedAt"]
    with pytest.raises(TypeError):
        completed["status"] = "FAILED"  # type: ignore[index]
    with pytest.raises(ImmutableRuntimeObjectError):
        store.update_status(object_id, "FAILED")


def test_append_event_and_list_by_workflow_execution() -> None:
    store = InMemoryRuntimeObjectStore()
    task = runtime_object("taskexecution-123456789abc")
    event = {
        "kind": "ExecutionEvent",
        "id": "executionevent-123456789abc",
        "provenance": {"workflowExecutionId": WORKFLOW_ID},
        "sequence": 1,
    }

    store.create(task, deterministic_key="task")
    store.append_event(event)

    assert [value["id"] for value in store.list_by_workflow_execution(WORKFLOW_ID)] == [
        task["id"],
        event["id"],
    ]


def test_concurrent_terminal_status_updates_have_one_winner() -> None:
    store = InMemoryRuntimeObjectStore()
    object_id = "taskexecution-123456789abc"
    store.create(runtime_object(object_id, status="RUNNING"), deterministic_key="task")

    def complete(status: str) -> str:
        try:
            store.update_status(object_id, status, expected_status="RUNNING")
            return status
        except (StatusConflictError, ImmutableRuntimeObjectError):
            return "CONFLICT"

    statuses = ["SUCCEEDED", "FAILED"] * 20
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(complete, statuses))

    winners = [result for result in results if result != "CONFLICT"]
    assert len(winners) == 1
    assert store.get(object_id)["status"] == winners[0]  # type: ignore[index]
