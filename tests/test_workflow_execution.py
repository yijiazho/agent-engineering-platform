import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from aep.resource_loader import ResourceLoader
from aep.runtime_store import InMemoryRuntimeObjectStore
from aep.workflow_execution import (
    InvalidWorkflowExecutionInputError,
    WorkflowExecutionCreator,
    _runtime_validator,
)


REPO_ROOT = Path(__file__).parents[1]
RESOURCE_FIXTURE = REPO_ROOT / "fixtures" / "resource-loader" / "workflow-resolution"
GITHUB_FIXTURE = REPO_ROOT / "fixtures" / "github" / "issue-created.json"
CREATED_AT = "2026-07-30T17:00:00Z"


class MutationRecordingStore(InMemoryRuntimeObjectStore):
    def __init__(self) -> None:
        super().__init__()
        self.create_calls = 0
        self.append_event_calls = 0

    def create(self, runtime_object, *, deterministic_key):
        self.create_calls += 1
        return super().create(runtime_object, deterministic_key=deterministic_key)

    def append_event(self, event):
        self.append_event_calls += 1
        return super().append_event(event)


def execution_inputs() -> dict[str, object]:
    resources = ResourceLoader(RESOURCE_FIXTURE).load()
    payload = json.loads(GITHUB_FIXTURE.read_text(encoding="utf-8"))
    return {
        "event": {
            "id": "event-123456789abc",
            "source": "github",
            "type": "github.issue.created",
            "repository": payload["repository"],
            "issue": payload["issue"],
            "sender": payload["sender"],
            "receivedAt": CREATED_AT,
            "deduplicationKey": "github:delivery:delivery-123",
        },
        "workflow": resources.by_kind("Workflow")[0],
        "event_resource": resources.by_kind("Event")[0],
        "repository_revision": "abc1234",
        "knowledge_graph_version": "kg-20260730-0001",
        "timestamp": CREATED_AT,
    }


def test_create_persists_running_trace_root_with_complete_provenance() -> None:
    store = InMemoryRuntimeObjectStore()

    execution = WorkflowExecutionCreator(store).create(**execution_inputs())

    assert execution["kind"] == "WorkflowExecution"
    assert execution["status"] == "RUNNING"
    assert execution["startedAt"] == CREATED_AT
    assert execution["createdAt"] == execution["updatedAt"] == CREATED_AT
    assert execution["taskExecutionIds"] == []
    assert execution["traceId"].startswith("trace-")
    assert execution["workflowRef"] == {
        "kind": "Workflow",
        "name": "issue-to-pr",
        "version": "1.0.0",
    }
    assert execution["eventRef"] == {
        "kind": "Event",
        "name": "github-issue-created",
        "version": "1.0.0",
    }
    assert execution["eventId"] == "event-123456789abc"
    assert execution["repositoryRevision"] == "abc1234"
    assert execution["knowledgeGraphVersion"] == "kg-20260730-0001"
    assert execution["provenance"] == {
        "actor": "workflow-controller",
        "repositoryRevision": "abc1234",
        "knowledgeGraphVersion": "kg-20260730-0001",
        "resourceRefs": [execution["workflowRef"], execution["eventRef"]],
    }
    assert store.get(execution["id"]) == execution


def test_retry_returns_the_original_execution_and_one_creation_event() -> None:
    store = InMemoryRuntimeObjectStore()
    creator = WorkflowExecutionCreator(store)
    inputs = execution_inputs()

    first = creator.create(**inputs)
    retried_inputs = dict(inputs)
    retried_inputs["repository_revision"] = "def5678"
    retried_inputs["timestamp"] = "2026-07-30T18:00:00Z"
    retry = creator.create(**retried_inputs)

    assert retry == first
    records = store.list_by_workflow_execution(first["id"])
    assert [record["kind"] for record in records] == [
        "WorkflowExecution",
        "ExecutionEvent",
    ]
    creation_event = records[1]
    assert creation_event["eventType"] == "WorkflowExecutionStarted"
    assert creation_event["subject"] == {
        "kind": "WorkflowExecution",
        "id": first["id"],
    }
    assert creation_event["traceId"] == first["traceId"]
    assert creation_event["sequence"] == 1
    assert creation_event["emittedAt"] == CREATED_AT
    assert creation_event["payload"]["status"] == "RUNNING"
    assert creation_event["payload"]["eventId"] == "event-123456789abc"
    assert creation_event["provenance"]["workflowExecutionId"] == first["id"]
    assert creation_event["provenance"]["repositoryRevision"] == "abc1234"


def test_success_records_conform_to_authoritative_runtime_schemas() -> None:
    store = InMemoryRuntimeObjectStore()

    execution = WorkflowExecutionCreator(store).create(**execution_inputs())
    creation_event = store.list_by_workflow_execution(execution["id"])[1]

    _runtime_validator("workflowexecution.schema.json").validate(dict(execution))
    _runtime_validator("executionevent.schema.json").validate(dict(creation_event))


@pytest.mark.parametrize(
    ("field", "invalid_value", "schema_path"),
    [
        ("repository_revision", "x", "$.provenance.repositoryRevision"),
        ("timestamp", "not-a-timestamp", "$.createdAt"),
    ],
)
def test_schema_invalid_input_creates_neither_execution_nor_event(
    field: str,
    invalid_value: str,
    schema_path: str,
) -> None:
    store = MutationRecordingStore()
    inputs = execution_inputs()
    inputs[field] = invalid_value

    with pytest.raises(InvalidWorkflowExecutionInputError) as raised:
        WorkflowExecutionCreator(store).create(**inputs)

    assert f"invalid WorkflowExecution at {schema_path}" in str(raised.value)
    assert store.create_calls == 0
    assert store.append_event_calls == 0


def test_concurrent_creation_converges_on_one_execution_and_event() -> None:
    store = InMemoryRuntimeObjectStore()
    inputs = execution_inputs()

    def create(_: int):
        return WorkflowExecutionCreator(store).create(**inputs)

    with ThreadPoolExecutor(max_workers=8) as executor:
        executions = list(executor.map(create, range(32)))

    assert len({execution["id"] for execution in executions}) == 1
    assert len({execution["traceId"] for execution in executions}) == 1
    records = store.list_by_workflow_execution(executions[0]["id"])
    assert [record["kind"] for record in records].count("WorkflowExecution") == 1
    assert [record["kind"] for record in records].count("ExecutionEvent") == 1
