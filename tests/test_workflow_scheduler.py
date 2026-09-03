from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from aep.resource_loader import Resource, ResourceCollection, ResourceRef
from aep.runtime_store import InMemoryRuntimeObjectStore
from aep.task_dag import resolve_task_dag
from aep.task_execution import FailureClass
from aep.workflow_execution import _runtime_validator
from aep.workflow_scheduler import TaskExecutionResult, WorkflowScheduler
from aep.workflow_scheduler import _retry_is_ready
from aep.workflow_scheduler import InvalidSchedulerInputError


TIMESTAMP = "2026-08-04T20:00:00Z"
WORKFLOW_ID = "workflowexecution-010000000001"
TRACE_ID = "trace-aep-010"


class FakeExecutor:
    def __init__(
        self,
        outcomes: dict[str, list[TaskExecutionResult]] | None = None,
        on_execute: Callable[[str], None] | None = None,
    ) -> None:
        self.outcomes = outcomes or {}
        self.on_execute = on_execute
        self.calls: list[tuple[str, int]] = []

    def execute(self, task, task_execution):
        name = task.name
        self.calls.append((name, task_execution["attempt"]))
        if self.on_execute is not None:
            self.on_execute(name)
        configured = self.outcomes.get(name, [])
        if configured:
            return configured.pop(0)
        return TaskExecutionResult.success()


def test_parallel_ready_tasks_are_created_before_fake_execution() -> None:
    store, plan, execution = scheduler_inputs(
        [node("analyze"), node("inventory"), node("plan", ("analyze", "inventory"))]
    )

    def assert_parallel_attempts_exist(_: str) -> None:
        tasks = task_executions(store)
        assert {task["taskRef"]["name"] for task in tasks} == {
            "analyze",
            "inventory",
        }

    executor = FakeExecutor(on_execute=assert_parallel_attempts_exist)
    result = scheduler(store, executor).reconcile(plan, execution)

    assert [task["taskRef"]["name"] for task in result.task_executions] == [
        "analyze",
        "inventory",
    ]
    assert [task["status"] for task in result.task_executions] == [
        "SUCCEEDED",
        "SUCCEEDED",
    ]
    assert executor.calls == [("analyze", 1), ("inventory", 1)]
    assert all(
        task["provenance"]["repositoryRevision"] == execution["repositoryRevision"]
        and task["provenance"]["knowledgeGraphVersion"]
        == execution["knowledgeGraphVersion"]
        for task in result.task_executions
    )


def test_dependent_waits_for_success_and_records_dependency_attempts() -> None:
    store, plan, execution = scheduler_inputs(
        [node("analyze"), node("plan", ("analyze",))]
    )
    executor = FakeExecutor()
    runtime = scheduler(store, executor)

    first = runtime.reconcile(plan, execution)
    assert [task["taskRef"]["name"] for task in first.task_executions] == [
        "analyze"
    ]

    second = runtime.reconcile(plan, execution)
    assert [task["taskRef"]["name"] for task in second.task_executions] == ["plan"]
    assert second.task_executions[0]["dependencyTaskExecutionIds"] == [
        first.task_executions[0]["id"]
    ]
    assert executor.calls == [("analyze", 1), ("plan", 1)]


def test_recoverable_failure_retries_with_next_attempt_number() -> None:
    store, plan, execution = scheduler_inputs([node("analyze")])
    executor = FakeExecutor(
        {
            "analyze": [
                TaskExecutionResult.failure(
                    FailureClass.RECOVERABLE, "temporary model timeout"
                ),
                TaskExecutionResult.success(),
            ]
        }
    )
    runtime = scheduler(store, executor, max_attempts=2)

    first = runtime.reconcile(plan, execution)
    retry = runtime.reconcile(plan, execution)

    assert first.task_executions[0]["attempt"] == 1
    assert first.task_executions[0]["status"] == "FAILED"
    assert retry.task_executions[0]["attempt"] == 2
    assert retry.task_executions[0]["status"] == "SUCCEEDED"
    assert retry.task_executions[0]["provenance"]["parentId"] == first.task_executions[0]["id"]
    assert executor.calls == [("analyze", 1), ("analyze", 2)]


def test_permanent_failure_blocks_dependents_and_does_not_retry() -> None:
    store, plan, execution = scheduler_inputs(
        [node("analyze"), node("plan", ("analyze",))]
    )
    executor = FakeExecutor(
        {
            "analyze": [
                TaskExecutionResult.failure(
                    FailureClass.PERMANENT, "unsupported task"
                )
            ]
        }
    )
    runtime = scheduler(store, executor, max_attempts=3)

    runtime.reconcile(plan, execution)
    blocked = runtime.reconcile(plan, execution)

    assert blocked.task_executions == ()
    assert executor.calls == [("analyze", 1)]
    assert {task["taskRef"]["name"] for task in task_executions(store)} == {
        "analyze"
    }


def test_failure_persists_safe_structured_details() -> None:
    store, plan, execution = scheduler_inputs([node("analyze")])
    details = {
        "reason": "SIZE_LIMIT_EXCEEDED", "path": "docs/task.md",
        "blobSize": 300_000, "appliedTrustedCeiling": 262_144,
        "evaluationComplete": False,
    }
    executor = FakeExecutor({"analyze": [TaskExecutionResult.failure(
        FailureClass.CONFIGURATION, "planning evidence inspection failed",
        details=details,
    )]})

    result = scheduler(store, executor).reconcile(plan, execution)

    assert result.task_executions[0]["failure"]["details"] == details


def test_reconciliation_is_idempotent_after_success() -> None:
    store, plan, execution = scheduler_inputs([node("analyze")])
    executor = FakeExecutor()
    runtime = scheduler(store, executor)

    runtime.reconcile(plan, execution)
    repeated = runtime.reconcile(plan, execution)

    assert repeated.task_executions == ()
    assert executor.calls == [("analyze", 1)]
    assert len(task_executions(store)) == 1


def test_events_cover_queued_started_succeeded_and_failed_states() -> None:
    store, plan, execution = scheduler_inputs(
        [node("pass"), node("fail")]
    )
    executor = FakeExecutor(
        {
            "fail": [
                TaskExecutionResult.failure(
                    FailureClass.EVALUATION, "acceptance failed"
                )
            ]
        }
    )

    scheduler(store, executor).reconcile(plan, execution)

    events = execution_events(store)
    assert [event["eventType"] for event in events] == [
        "TaskExecutionQueued",
        "TaskExecutionQueued",
        "TaskExecutionStarted",
        "TaskExecutionSucceeded",
        "TaskExecutionStarted",
        "TaskExecutionFailed",
    ]
    failed = events[-1]
    assert failed["payload"] == {
        "status": "FAILED",
        "attempt": 1,
        "failureClass": "EVALUATION",
        "retryable": False,
    }

    task_validator = _runtime_validator("taskexecution.schema.json")
    event_validator = _runtime_validator("executionevent.schema.json")
    for task in task_executions(store):
        task_validator.validate(dict(task))
    for event in events:
        event_validator.validate(dict(event))


def test_concurrent_reconcilers_execute_attempt_once_and_link_it_once() -> None:
    store, plan, execution = scheduler_inputs([node("analyze")])
    executor = FakeExecutor()

    def reconcile(_: int):
        return scheduler(store, executor).reconcile(plan, execution)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(reconcile, range(32)))

    assert executor.calls == [("analyze", 1)]
    tasks = task_executions(store)
    persisted_workflow = store.get(WORKFLOW_ID)
    assert len(tasks) == 1
    assert persisted_workflow["taskExecutionIds"] == [tasks[0]["id"]]


@pytest.mark.parametrize(
    "failure_mode",
    ["terminal_before_commit", "started_event"],
)
def test_fallible_scheduler_operations_recover_as_numbered_retry(
    failure_mode: str,
) -> None:
    store, plan, execution = scheduler_inputs(
        [node("analyze")], store_factory=lambda: FallibleStore(failure_mode)
    )
    executor = FakeExecutor()
    runtime = scheduler(store, executor, max_attempts=2)

    if failure_mode == "started_event":
        with pytest.raises(OSError, match="injected event failure"):
            runtime.reconcile(plan, execution)
    else:
        runtime.reconcile(plan, execution)
    retry = runtime.reconcile(plan, execution)

    tasks = task_executions(store)
    assert [(task["attempt"], task["status"]) for task in tasks] == [
        (1, "FAILED"),
        (2, "SUCCEEDED"),
    ]
    assert retry.task_executions[0]["attempt"] == 2
    assert store.get(WORKFLOW_ID)["taskExecutionIds"] == [
        task["id"] for task in tasks
    ]


def test_ambiguous_start_is_not_reclaimed_without_owner_token() -> None:
    store, plan, execution = scheduler_inputs(
        [node("analyze")],
        store_factory=lambda: FallibleStore("start_after_commit"),
    )
    executor = FakeExecutor()
    runtime = scheduler(store, executor, max_attempts=2)

    first = runtime.reconcile(plan, execution)
    repeated = runtime.reconcile(plan, execution)

    assert first.task_executions[0]["status"] == "RUNNING"
    assert repeated.task_executions == ()
    assert executor.calls == []


def test_executor_exception_is_recoverable_and_does_not_strand_running_attempt() -> None:
    store, plan, execution = scheduler_inputs([node("analyze")])
    executor = RaisingExecutor()
    runtime = WorkflowScheduler(
        store,
        executor,
        max_attempts=2,
        clock=lambda: TIMESTAMP,
    )

    first = runtime.reconcile(plan, execution)
    second = runtime.reconcile(plan, execution)

    assert first.task_executions[0]["status"] == "FAILED"
    assert first.task_executions[0]["failure"]["class"] == "RECOVERABLE"
    assert second.task_executions[0]["attempt"] == 2
    assert second.task_executions[0]["status"] == "SUCCEEDED"


@pytest.mark.parametrize("failure_mode", ["queued_event", "membership"])
def test_pre_start_failures_leave_attempt_pending_for_safe_reconciliation(
    failure_mode: str,
) -> None:
    store, plan, execution = scheduler_inputs(
        [node("analyze")], store_factory=lambda: FallibleStore(failure_mode)
    )
    executor = FakeExecutor()
    runtime = scheduler(store, executor)

    with pytest.raises(OSError, match="injected"):
        runtime.reconcile(plan, execution)
    pending = task_executions(store)
    assert [(task["attempt"], task["status"]) for task in pending] == [
        (1, "PENDING")
    ]

    recovered = runtime.reconcile(plan, execution)

    assert recovered.task_executions[0]["attempt"] == 1
    assert recovered.task_executions[0]["status"] == "SUCCEEDED"
    assert executor.calls == [("analyze", 1)]
    assert store.get(WORKFLOW_ID)["taskExecutionIds"] == [pending[0]["id"]]


def test_missing_terminal_event_is_repaired_before_dependent_runs() -> None:
    store, plan, execution = scheduler_inputs(
        [node("analyze"), node("plan", ("analyze",))],
        store_factory=lambda: FallibleStore("terminal_event"),
    )
    runtime = scheduler(store, FakeExecutor())

    with pytest.raises(OSError, match="injected event failure"):
        runtime.reconcile(plan, execution)
    second = runtime.reconcile(plan, execution)

    assert second.task_executions[0]["taskRef"]["name"] == "plan"
    assert [event["eventType"] for event in execution_events(store)].count(
        "TaskExecutionSucceeded"
    ) == 2


def test_live_running_executor_is_not_reclaimed_after_large_clock_advance() -> None:
    store, plan, execution = scheduler_inputs([node("analyze")])
    executor = BlockingExecutor()
    clock = MutableClock("2026-08-04T20:00:00Z")
    first_runtime = WorkflowScheduler(
        store,
        executor,
        max_attempts=2,
        clock=clock,
    )
    second_runtime = WorkflowScheduler(
        store,
        executor,
        max_attempts=2,
        clock=clock,
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(first_runtime.reconcile, plan, execution)
        assert executor.started.wait(timeout=2)
        clock.value = "2036-08-04T20:00:00Z"
        concurrent = second_runtime.reconcile(plan, execution)
        executor.release.set()
        first = first_future.result(timeout=2)

    assert concurrent.task_executions == ()
    assert executor.calls == 1
    assert first.task_executions[0]["attempt"] == 1
    assert first.task_executions[0]["status"] == "SUCCEEDED"


def test_orphan_and_mismatched_workflow_evidence_are_rejected_without_mutation() -> None:
    store, plan, execution = scheduler_inputs([node("analyze")])
    orphan = dict(execution)
    orphan["id"] = "workflowexecution-010000000099"
    mismatched = dict(execution)
    mismatched["repositoryRevision"] = "fedcba0"
    mismatched_graph = dict(execution)
    mismatched_graph["knowledgeGraphVersion"] = "kg-forged-v1"
    missing_graph = dict(execution)
    del missing_graph["knowledgeGraphVersion"]

    with pytest.raises(InvalidSchedulerInputError, match="must exist"):
        scheduler(store, FakeExecutor()).reconcile(plan, orphan)
    with pytest.raises(InvalidSchedulerInputError, match="repositoryRevision"):
        scheduler(store, FakeExecutor()).reconcile(plan, mismatched)
    with pytest.raises(InvalidSchedulerInputError, match="knowledgeGraphVersion"):
        scheduler(store, FakeExecutor()).reconcile(plan, mismatched_graph)
    with pytest.raises(InvalidSchedulerInputError, match="knowledgeGraphVersion"):
        scheduler(store, FakeExecutor()).reconcile(plan, missing_graph)

    assert task_executions(store) == []


def test_schema_invalid_workflow_and_clock_fail_before_scheduler_mutation() -> None:
    store, plan, execution = scheduler_inputs([node("analyze")])
    invalid = dict(execution)
    invalid["traceId"] = "short"

    with pytest.raises(InvalidSchedulerInputError, match="invalid WorkflowExecution"):
        scheduler(store, FakeExecutor()).reconcile(plan, invalid)
    bad_clock = WorkflowScheduler(
        store,
        FakeExecutor(),
        clock=lambda: "not-a-timestamp",
    )
    with pytest.raises(InvalidSchedulerInputError, match="RFC3339"):
        bad_clock.reconcile(plan, execution)

    assert task_executions(store) == []


def scheduler(
    store: InMemoryRuntimeObjectStore,
    executor: FakeExecutor,
    *,
    max_attempts: int = 1,
) -> WorkflowScheduler:
    return WorkflowScheduler(
        store,
        executor,
        max_attempts=max_attempts,
        clock=lambda: TIMESTAMP,
    )


def scheduler_inputs(nodes, *, store_factory=InMemoryRuntimeObjectStore):
    workflow = resource("Workflow", "scheduler-test", {"tasks": nodes})
    tasks = tuple(
        resource(
            "Task",
            entry["taskRef"]["name"],
            {"objective": "Run test Task.", "outputs": {"type": "object"}},
        )
        for entry in nodes
    )
    workspace = resource(
        "Workspace",
        "test-workspace",
        {
            "repository": {
                "provider": "github",
                "owner": "example",
                "name": "repo",
                "defaultBranch": "main",
            },
            "resourceDiscovery": {"root": ".ai"},
        },
    )
    resources = ResourceCollection(
        workspace=workspace,
        resources=(workspace, workflow, *tasks),
    )
    plan = resolve_task_dag(workflow, resources)
    execution = {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "WorkflowExecution",
        "id": WORKFLOW_ID,
        "traceId": TRACE_ID,
        "createdAt": TIMESTAMP,
        "updatedAt": TIMESTAMP,
        "provenance": {
            "actor": "workflow-controller",
            "resourceRefs": [ref_record(workflow.ref)],
        },
        "workflowRef": ref_record(workflow.ref),
        "eventRef": {
            "kind": "Event",
            "name": "test-event",
            "version": "1.0.0",
        },
        "eventId": "event-scheduler-test",
        "repositoryRevision": "abcdef0",
        "knowledgeGraphVersion": "kg-scheduler-test-v1",
        "status": "RUNNING",
        "taskExecutionIds": [],
    }
    store = store_factory()
    store.create(execution, deterministic_key="workflow:scheduler-test")
    return store, plan, execution


def node(name: str, dependencies: tuple[str, ...] = ()):
    value = {"taskRef": ref_record(ResourceRef("Task", name, "1.0.0"))}
    if dependencies:
        value["dependsOn"] = [
            ref_record(ResourceRef("Task", dependency, "1.0.0"))
            for dependency in dependencies
        ]
    return value


def resource(kind: str, name: str, spec: dict[str, object]) -> Resource:
    return Resource(
        ref=ResourceRef(kind, name, "1.0.0"),
        path=Path(f"{name}.yaml"),
        data={
            "apiVersion": "aep.dev/v1alpha1",
            "kind": kind,
            "metadata": {"name": name, "version": "1.0.0"},
            "spec": spec,
        },
        references=(),
    )


def ref_record(ref: ResourceRef) -> dict[str, str]:
    return {"kind": ref.kind, "name": ref.name, "version": ref.version}


def task_executions(store: InMemoryRuntimeObjectStore):
    return [
        value
        for value in store.list_by_workflow_execution(WORKFLOW_ID)
        if value["kind"] == "TaskExecution"
    ]


def execution_events(store: InMemoryRuntimeObjectStore):
    return [
        value
        for value in store.list_by_workflow_execution(WORKFLOW_ID)
        if value["kind"] == "ExecutionEvent"
    ]


class FallibleStore(InMemoryRuntimeObjectStore):
    def __init__(self, failure_mode: str) -> None:
        super().__init__()
        self.failure_mode = failure_mode
        self.failed = False

    def update_status(self, object_id, status, **kwargs):
        if not self.failed and self.failure_mode == "start_after_commit" and status == "RUNNING":
            self.failed = True
            super().update_status(object_id, status, **kwargs)
            raise OSError("injected start failure")
        if not self.failed and self.failure_mode == "terminal_before_commit" and status == "SUCCEEDED":
            self.failed = True
            raise OSError("injected terminal failure")
        return super().update_status(object_id, status, **kwargs)

    def append_event(self, event):
        should_fail = (
            self.failure_mode == "started_event"
            and event["eventType"] == "TaskExecutionStarted"
        ) or (
            self.failure_mode == "terminal_event"
            and event["eventType"] == "TaskExecutionSucceeded"
        ) or (
            self.failure_mode == "queued_event"
            and event["eventType"] == "TaskExecutionQueued"
        )
        if not self.failed and should_fail:
            self.failed = True
            raise OSError("injected event failure")
        return super().append_event(event)

    def append_task_execution_id(self, workflow_execution_id, task_execution_id, **kwargs):
        if not self.failed and self.failure_mode == "membership":
            self.failed = True
            raise OSError("injected membership failure")
        return super().append_task_execution_id(
            workflow_execution_id, task_execution_id, **kwargs
        )


class RaisingExecutor:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, task, task_execution):
        self.calls += 1
        if self.calls == 1:
            raise OSError("temporary worker failure")
        return TaskExecutionResult.success()


class MutableClock:
    def __init__(self, value: str) -> None:
        self.value = value

    def __call__(self) -> str:
        return self.value


class BlockingExecutor:
    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()
        self.calls = 0

    def execute(self, task, task_execution):
        self.calls += 1
        self.started.set()
        if not self.release.wait(timeout=2):
            raise TimeoutError("test did not release fake executor")
        return TaskExecutionResult.success()
def test_recoverable_retry_waits_for_persisted_not_before():
    attempt = {
        "status": "FAILED",
        "attempt": 1,
        "failure": {
            "class": "RECOVERABLE",
            "retryable": True,
            "message": "provider throttle",
            "retryNotBefore": "2026-08-15T00:01:15Z",
        },
    }

    assert not _retry_is_ready(attempt, 2, "2026-08-15T00:01:14Z")
    assert _retry_is_ready(attempt, 2, "2026-08-15T00:01:15Z")
