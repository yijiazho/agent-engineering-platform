from concurrent.futures import ThreadPoolExecutor
import errno
from pathlib import Path
import traceback

import pytest

import aep.runtime_store as runtime_store_module

from aep.runtime_store import (
    DurableJsonRuntimeObjectStore,
    ImmutableRuntimeObjectError,
    InMemoryRuntimeObjectStore,
    RuntimeObjectAlreadyExistsError,
    RuntimeStoreError,
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
        "provenance": {"actor": "workflow-runtime", "resourceRefs": []},
        "workflowExecutionId": WORKFLOW_ID,
        "taskRef": {"kind": "Task", "name": "analyze-issue", "version": "1.0.0"},
        "attempt": 1,
        "dependencyTaskExecutionIds": [],
        "contextPackageId": "contextpackage-123456789abc",
        "resolvedAgentId": "resolvedagent-123456789abc",
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


def test_task_execution_ids_are_attached_atomically_and_idempotently() -> None:
    store = InMemoryRuntimeObjectStore()
    workflow = {
        "kind": "WorkflowExecution",
        "id": WORKFLOW_ID,
        "taskExecutionIds": [],
    }
    first = runtime_object("taskexecution-123456789abc")
    second = runtime_object("taskexecution-abcdef123456")
    store.create(workflow, deterministic_key="workflow")
    store.create(first, deterministic_key="first-task")
    store.create(second, deterministic_key="second-task")

    task_ids = [first["id"], second["id"]] * 20
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(
            executor.map(
                lambda task_id: store.append_task_execution_id(
                    WORKFLOW_ID, task_id
                ),
                task_ids,
            )
        )

    attached = store.get(WORKFLOW_ID)["taskExecutionIds"]
    assert len(attached) == 2
    assert set(attached) == {first["id"], second["id"]}


def test_task_execution_membership_rejects_wrong_owner() -> None:
    store = InMemoryRuntimeObjectStore()
    store.create(
        {"kind": "WorkflowExecution", "id": WORKFLOW_ID, "taskExecutionIds": []},
        deterministic_key="workflow",
    )
    task = runtime_object("taskexecution-123456789abc")
    task["workflowExecutionId"] = "workflowexecution-abcdef123456"
    store.create(task, deterministic_key="task")

    with pytest.raises(ValueError, match="does not belong"):
        store.append_task_execution_id(WORKFLOW_ID, task["id"])


def test_terminal_workflow_membership_is_immutable_but_idempotent() -> None:
    store = InMemoryRuntimeObjectStore()
    first = runtime_object("taskexecution-123456789abc")
    second = runtime_object("taskexecution-abcdef123456")
    workflow = {
        "kind": "WorkflowExecution",
        "id": WORKFLOW_ID,
        "status": "SUCCEEDED",
        "taskExecutionIds": [first["id"]],
    }
    store.create(workflow, deterministic_key="workflow")
    store.create(first, deterministic_key="first-task")
    store.create(second, deterministic_key="second-task")

    assert store.append_task_execution_id(WORKFLOW_ID, first["id"])[
        "taskExecutionIds"
    ] == [first["id"]]
    with pytest.raises(ImmutableRuntimeObjectError):
        store.append_task_execution_id(WORKFLOW_ID, second["id"])


def test_list_by_task_execution_uses_persisted_metadata_index() -> None:
    store = InMemoryRuntimeObjectStore()
    artifact = {
        "kind": "GeneratedArtifact",
        "id": "generatedartifact-123456789abc",
        "taskExecutionId": "taskexecution-123456789abc",
    }
    store.create(artifact, deterministic_key="artifact")

    assert store.list_by_task_execution("taskexecution-123456789abc") == (artifact,)


def test_list_by_task_execution_falls_back_to_provenance() -> None:
    store = InMemoryRuntimeObjectStore()
    invocation = {
        "kind": "ToolInvocation",
        "id": "toolinvocation-123456789abc",
        "provenance": {
            "taskExecutionId": "taskexecution-123456789abc",
        },
    }
    store.create(invocation, deterministic_key="invocation")

    assert store.list_by_task_execution("taskexecution-123456789abc") == (
        invocation,
    )


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("traceId", "different-trace"),
        ("provenance", {"actor": "different", "resourceRefs": []}),
        ("workflowExecutionId", "workflowexecution-abcdef123456"),
        ("taskRef", {"kind": "Task", "name": "other-task", "version": "1.0.0"}),
        ("attempt", 2),
        ("dependencyTaskExecutionIds", ["taskexecution-abcdef123456"]),
        ("contextPackageId", "contextpackage-abcdef123456"),
        ("resolvedAgentId", "resolvedagent-abcdef123456"),
    ],
)
def test_task_execution_identity_fields_cannot_change(
    field: str, replacement: object
) -> None:
    store = InMemoryRuntimeObjectStore()
    object_id = "taskexecution-123456789abc"
    original = runtime_object(object_id, status="RUNNING")
    store.create(original, deterministic_key="task")

    with pytest.raises(ValueError, match="protected fields"):
        store.update_status(
            object_id,
            "SUCCEEDED",
            expected_status="RUNNING",
            changes={field: replacement},
        )

    persisted = store.get(object_id)
    assert persisted["status"] == "RUNNING"
    assert persisted[field] == original[field]


def test_late_bound_task_identity_can_only_be_attached_once() -> None:
    store = InMemoryRuntimeObjectStore()
    object_id = "taskexecution-123456789abc"
    pending = runtime_object(object_id)
    del pending["contextPackageId"]
    store.create(pending, deterministic_key="task")

    attached = store.update_status(
        object_id,
        "RUNNING",
        expected_status="PENDING",
        changes={"contextPackageId": "contextpackage-abcdef123456"},
    )

    assert attached["contextPackageId"] == "contextpackage-abcdef123456"
    with pytest.raises(ValueError, match="contextPackageId"):
        store.update_status(
            object_id,
            "AWAITING_APPROVAL",
            expected_status="RUNNING",
            changes={"contextPackageId": "contextpackage-fedcba654321"},
        )


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


def test_durable_json_store_restores_objects_claims_and_indexes(tmp_path) -> None:
    path = tmp_path / "runtime/objects.json"
    store = DurableJsonRuntimeObjectStore(path)
    value = runtime_object("taskexecution-durable", status="RUNNING")
    store.create(value, deterministic_key="durable-task")
    store.claim("event:delivery", {"id": "event-durable"})

    restarted = DurableJsonRuntimeObjectStore(path)

    assert restarted.get("taskexecution-durable")["status"] == "RUNNING"
    assert restarted.list_by_workflow_execution(WORKFLOW_ID)[0]["id"] == (
        "taskexecution-durable"
    )
    accepted, prior = restarted.claim(
        "event:delivery", {"id": "event-replacement"}
    )
    assert accepted is False
    assert prior["id"] == "event-durable"


def test_durable_json_store_persists_events_and_uses_collision_safe_checkpoint_names(tmp_path) -> None:
    path = tmp_path / "runtime/objects.json"
    store = DurableJsonRuntimeObjectStore(path)
    store.create(runtime_object("taskexecution-event"), deterministic_key="task")
    store.append_event({
        "kind": "ExecutionEvent", "id": "executionevent-event",
        "provenance": {"workflowExecutionId": WORKFLOW_ID}, "sequence": 1,
    })

    restarted = DurableJsonRuntimeObjectStore(path)
    assert restarted.get("executionevent-event") is not None
    assert not (path.parent / "objects.json.tmp").exists()


def test_durable_checkpoint_failure_has_safe_diagnostic_without_raw_chain(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "runtime/objects.json"
    store = DurableJsonRuntimeObjectStore(path)
    def fail_replace(source, target):
        error = OSError(errno.EACCES, "sensitive host path should not leak")
        raise error

    monkeypatch.setattr("aep.runtime_store.os.replace", fail_replace)
    with pytest.raises(RuntimeStoreError) as raised:
        store.create(runtime_object("taskexecution-failure"), deterministic_key="task")

    assert raised.value.diagnostic == {
        "operation": "checkpoint", "phase": "replace",
        "category": "permission_denied", "errno": errno.EACCES,
    }
    assert "sensitive" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert "sensitive" not in "".join(traceback.format_exception(raised.value))


def test_independent_durable_stores_refresh_under_writer_fence_without_lost_claims(tmp_path) -> None:
    path = tmp_path / "runtime/objects.json"
    first = DurableJsonRuntimeObjectStore(path)
    second = DurableJsonRuntimeObjectStore(path)
    first.create(runtime_object("taskexecution-one"), deterministic_key="task-one")
    second.create(runtime_object("taskexecution-two"), deterministic_key="task-two")
    accepted, prior = first.claim("delivery", {"id": "first"})
    duplicate, same = second.claim("delivery", {"id": "second"})

    assert accepted is True
    assert duplicate is False
    assert prior == same == {"id": "first"}
    assert first.get("taskexecution-one") is not None
    assert first.get("taskexecution-two") is not None


def test_durable_refresh_preserves_creation_order_in_indexes(tmp_path) -> None:
    path = tmp_path / "runtime/objects.json"
    store = DurableJsonRuntimeObjectStore(path)
    store.create(runtime_object("taskexecution-z"), deterministic_key="task-z")
    store.create(runtime_object("taskexecution-a"), deterministic_key="task-a")

    assert [
        value["id"] for value in store.list_by_workflow_execution(WORKFLOW_ID)
    ] == ["taskexecution-z", "taskexecution-a"]

    restarted = DurableJsonRuntimeObjectStore(path)
    assert [
        value["id"] for value in restarted.list_by_workflow_execution(WORKFLOW_ID)
    ] == ["taskexecution-z", "taskexecution-a"]


def test_writer_fence_file_does_not_grow_on_repeated_operations(tmp_path) -> None:
    path = tmp_path / "runtime/objects.json"
    store = DurableJsonRuntimeObjectStore(path)
    lock_path = path.parent / ".objects.json.lock"
    initial_size = lock_path.stat().st_size

    for _ in range(50):
        store.get("missing")

    assert lock_path.stat().st_size == initial_size
    assert initial_size <= 1


def test_restore_os_failure_retains_safe_diagnostic_without_raw_chain(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "runtime/objects.json"
    DurableJsonRuntimeObjectStore(path).create(
        runtime_object("taskexecution-readable"), deterministic_key="task"
    )
    original_read_text = Path.read_text

    def fail_checkpoint_read(candidate, *args, **kwargs):
        if candidate.resolve() == path.resolve():
            raise OSError(errno.EIO, "sensitive mount path")
        return original_read_text(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_checkpoint_read)
    with pytest.raises(RuntimeStoreError) as raised:
        DurableJsonRuntimeObjectStore(path)

    assert raised.value.diagnostic == {
        "operation": "restore", "phase": "read",
        "category": "io_error", "errno": errno.EIO,
    }
    assert "sensitive" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert "sensitive" not in "".join(traceback.format_exception(raised.value))


def test_windows_writer_lock_conflict_is_classified_as_busy() -> None:
    diagnostic = runtime_store_module._persistence_diagnostic(
        "writer_fence", "acquire", OSError(errno.EACCES, "lock conflict"),
        lock_contention=True,
    )

    assert diagnostic == {
        "operation": "writer_fence", "phase": "acquire",
        "category": "busy", "errno": errno.EACCES,
    }


def test_writer_fence_acquisition_cleanup_failure_is_sanitized(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "runtime/objects.json"
    store = DurableJsonRuntimeObjectStore(path)

    class CloseFailingHandle:
        def __init__(self, descriptor: int) -> None:
            self.descriptor = descriptor

        def close(self) -> None:
            runtime_store_module.os.close(self.descriptor)
            raise OSError(errno.EIO, "sensitive cleanup mount path")

    monkeypatch.setattr(
        runtime_store_module.os, "fdopen",
        lambda descriptor, mode: CloseFailingHandle(descriptor),
    )
    monkeypatch.setattr(
        runtime_store_module, "_lock_file",
        lambda handle: (_ for _ in ()).throw(
            OSError(errno.EBUSY, "sensitive acquisition path")
        ),
    )

    with pytest.raises(RuntimeStoreError) as raised:
        store.get("missing")

    assert raised.value.diagnostic == {
        "operation": "writer_fence", "phase": "cleanup",
        "category": "io_error", "errno": errno.EIO,
    }
    formatted = "".join(traceback.format_exception(raised.value))
    assert "sensitive" not in str(raised.value)
    assert "sensitive" not in formatted


def test_constructor_removes_only_stale_sibling_checkpoints_under_fence(tmp_path) -> None:
    path = tmp_path / "runtime/objects.json"
    store = DurableJsonRuntimeObjectStore(path)
    store.create(runtime_object("taskexecution-cleanup"), deterministic_key="task")
    orphan = path.parent / ".objects.json.interrupted.tmp"
    unrelated = path.parent / "keep.tmp"
    orphan.write_text("orphan", encoding="utf-8")
    unrelated.write_text("keep", encoding="utf-8")

    restarted = DurableJsonRuntimeObjectStore(path)

    assert restarted.get("taskexecution-cleanup") is not None
    assert not orphan.exists()
    assert unrelated.read_text(encoding="utf-8") == "keep"


def test_writer_fence_release_failure_is_safe_and_preserves_committed_state(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "runtime/objects.json"
    store = DurableJsonRuntimeObjectStore(path)
    original_unlock = runtime_store_module._unlock_file

    def fail_after_unlock(handle):
        original_unlock(handle)
        raise OSError(errno.EIO, "sensitive lock path")

    monkeypatch.setattr(runtime_store_module, "_unlock_file", fail_after_unlock)
    with pytest.raises(RuntimeStoreError) as raised:
        store.claim("committed-claim", {"id": "claim-value"})

    assert raised.value.diagnostic == {
        "operation": "writer_fence", "phase": "release",
        "category": "io_error", "errno": errno.EIO,
    }
    assert "sensitive" not in str(raised.value)
    monkeypatch.setattr(runtime_store_module, "_unlock_file", original_unlock)
    restarted = DurableJsonRuntimeObjectStore(path)
    accepted, prior = restarted.claim("committed-claim", {"id": "replacement"})
    assert accepted is False
    assert prior == {"id": "claim-value"}
