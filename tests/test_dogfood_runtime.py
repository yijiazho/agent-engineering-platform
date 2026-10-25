from __future__ import annotations

from pathlib import Path
import logging
from threading import RLock
from types import SimpleNamespace

import pytest

from aep.execution_checkout import CheckoutFailureClass, CheckoutProvisionError
from aep.execution_replay import ReplayError
from aep.context_identity import context_identity
from aep.dogfood_runtime import (
    DogfoodReconciliationConsumer,
    DogfoodReconciliationError,
    DogfoodWorkflowRunner,
    _historical_replay_inputs,
    _replay_resource_collection,
    _reconciliation_revision,
)
from aep.resource_loader import Resource, ResourceCollection, ResourceRef
from aep.runtime_store import InMemoryRuntimeObjectStore
from aep.runtime_store import DurableJsonRuntimeObjectStore
from aep.webhook_dispatch import SQLiteReconciliationDispatcher


class RecordingRunner:
    def __init__(self, failure: Exception | None = None, *, approval_pending: bool = False) -> None:
        self.failure = failure
        self.approval_pending = approval_pending
        self.requests = []

    def run(self, request):
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        return "workflowexecution-test"

    def is_approval_pending(self, request):
        return self.approval_pending


def test_workflow_runner_preserves_resource_checkout_crlf_policy(
    tmp_path: Path, monkeypatch
) -> None:
    captured: dict[str, object] = {}

    class VerificationComplete(Exception):
        pass

    def verify(repository_root, revision, *, require_detached, autocrlf):
        captured.update(
            repository_root=repository_root,
            revision=revision,
            require_detached=require_detached,
            autocrlf=autocrlf,
        )
        raise VerificationComplete

    monkeypatch.setattr("aep.dogfood_runtime.verify_resource_checkout", verify)
    environment = {
        "AEP_STATE_ROOT": str(tmp_path / "state"),
        "AEP_REPOSITORY_ROOT": str(tmp_path / "resources"),
        "AEP_RESOURCE_REVISION": "a" * 40,
        "AEP_RESOURCE_SCHEMA_ROOT": str(tmp_path / "schemas"),
        "AEP_RESOURCE_GIT_AUTOCRLF": "true",
    }

    with pytest.raises(VerificationComplete):
        DogfoodWorkflowRunner(environment)

    assert captured == {
        "repository_root": Path(environment["AEP_REPOSITORY_ROOT"]),
        "revision": "a" * 40,
        "require_detached": True,
        "autocrlf": "true",
    }


def test_replay_resource_override_rewrites_pinned_resource_dependencies() -> None:
    workspace = Resource(
        ResourceRef("Workspace", "platform", "1.0.0"), Path("workspace.yaml"),
        {"kind": "Workspace", "metadata": {"name": "platform", "version": "1.0.0"}}, (),
    )
    old_prompt = Resource(
        ResourceRef("Prompt", "plan", "1.0.0"), Path("plan-v1.yaml"),
        {"kind": "Prompt", "metadata": {"name": "plan", "version": "1.0.0"}}, (),
    )
    new_prompt = Resource(
        ResourceRef("Prompt", "plan", "2.0.0"), Path("plan-v2.yaml"),
        {"kind": "Prompt", "metadata": {"name": "plan", "version": "2.0.0"}}, (),
    )
    agent = Resource(
        ResourceRef("Agent", "planner", "1.0.0"), Path("planner.yaml"),
        {"kind": "Agent", "metadata": {"name": "planner", "version": "1.0.0"},
         "spec": {"promptRef": {"kind": "Prompt", "name": "plan", "version": "1.0.0"}}},
        (old_prompt.ref,),
    )
    historical = ResourceCollection(workspace, (workspace, agent, old_prompt))
    available = ResourceCollection(workspace, (workspace, agent, old_prompt, new_prompt))
    provenance = {
        "inheritedResourceRefs": [
            {"kind": "Agent", "name": "planner", "version": "1.0.0"},
            {"kind": "Prompt", "name": "plan", "version": "1.0.0"},
        ],
        "resourceRefs": [
            {"kind": "Agent", "name": "planner", "version": "1.0.0"},
            {"kind": "Prompt", "name": "plan", "version": "2.0.0"},
        ],
        "overrides": [{"kind": "Prompt", "name": "plan", "version": "2.0.0"}],
    }

    replay_resources = _replay_resource_collection(historical, available, provenance)

    replay_agent = replay_resources.get(agent.ref)
    assert replay_agent.data["spec"]["promptRef"]["version"] == "2.0.0"
    assert replay_agent.references == (new_prompt.ref,)
    assert agent.data["spec"]["promptRef"]["version"] == "1.0.0"


def test_exact_replay_preserves_multiple_inherited_versions_of_same_resource() -> None:
    workspace = Resource(
        ResourceRef("Workspace", "platform", "1.0.0"), Path("workspace.yaml"),
        {"kind": "Workspace", "metadata": {"name": "platform", "version": "1.0.0"}}, (),
    )
    model_v1 = Resource(
        ResourceRef("Model", "reasoner", "1.0.0"), Path("model-v1.yaml"),
        {"kind": "Model", "metadata": {"name": "reasoner", "version": "1.0.0"}}, (),
    )
    model_v2 = Resource(
        ResourceRef("Model", "reasoner", "2.0.0"), Path("model-v2.yaml"),
        {"kind": "Model", "metadata": {"name": "reasoner", "version": "2.0.0"}}, (),
    )
    agents = tuple(
        Resource(
            ResourceRef("Agent", name, "1.0.0"), Path(f"{name}.yaml"),
            {
                "kind": "Agent",
                "metadata": {"name": name, "version": "1.0.0"},
                "spec": {"modelRef": {
                    "kind": "Model", "name": "reasoner", "version": model.ref.version,
                }},
            },
            (model.ref,),
        )
        for name, model in (("analyzer", model_v1), ("planner", model_v2))
    )
    historical = ResourceCollection(
        workspace, (workspace, model_v1, model_v2, *agents),
    )
    inherited = [
        {"kind": resource.kind, "name": resource.name, "version": resource.version}
        for resource in historical.resources if resource.kind != "Workspace"
    ]
    provenance = {
        "inheritedResourceRefs": inherited,
        "resourceRefs": inherited,
        "overrides": [],
    }

    replay_resources = _replay_resource_collection(historical, historical, provenance)

    assert replay_resources.get(agents[0].ref).references == (model_v1.ref,)
    assert replay_resources.get(agents[1].ref).references == (model_v2.ref,)


def test_replay_resource_resolution_fails_closed_for_missing_override() -> None:
    workspace = Resource(
        ResourceRef("Workspace", "platform", "1.0.0"), Path("workspace.yaml"),
        {"kind": "Workspace", "metadata": {"name": "platform", "version": "1.0.0"}}, (),
    )
    historical = ResourceCollection(workspace, (workspace,))
    provenance = {
        "inheritedResourceRefs": [{"kind": "Prompt", "name": "plan", "version": "1.0.0"}],
        "resourceRefs": [{"kind": "Prompt", "name": "plan", "version": "2.0.0"}],
        "overrides": [{"kind": "Prompt", "name": "plan", "version": "2.0.0"}],
    }

    with pytest.raises(ReplayError, match="overridden Resource version"):
        _replay_resource_collection(historical, historical, provenance)


def test_replay_recovers_only_complete_historical_event_context() -> None:
    event_record = {
        "id": "event-historical", "source": "github",
        "type": "github.issue.created", "receivedAt": "2026-01-01T00:00:00Z",
        "deduplicationKey": "github:delivery:1", "repository": {"full_name": "octo/repo"},
    }
    source = {"id": "workflow-source", "eventId": event_record["id"], "taskExecutionIds": ["task-source"]}
    task = {
        "id": "task-source", "kind": "TaskExecution",
        "workflowExecutionId": source["id"], "taskRef": {"kind": "Task", "name": "analyze", "version": "1.0.0"},
        "contextPackageId": "context-source",
    }
    context = {
        "id": "context-source", "kind": "ContextPackage", "taskRef": task["taskRef"],
        "elements": [{"type": "event", "truncated": False, "content": event_record}],
    }

    event, contexts = _historical_replay_inputs(source, (task, context))

    assert event == event_record
    assert contexts[("Task", "analyze", "1.0.0")] is context
    context["elements"][0]["truncated"] = True
    with pytest.raises(ReplayError, match="truncated"):
        _historical_replay_inputs(source, (task, context))


@pytest.mark.parametrize("context_mode", ["reuse", "rebuild"])
def test_replay_runner_resolves_pinned_inputs_before_dispatch(
    tmp_path: Path, monkeypatch, context_mode: str,
) -> None:
    revision = "a" * 40
    refs = {
        "workflow": {"kind": "Workflow", "name": "issue-to-pr", "version": "1.0.0"},
        "event": {"kind": "Event", "name": "github-issue-created", "version": "1.0.0"},
        "task": {"kind": "Task", "name": "analyze", "version": "1.0.0"},
        "prompt": {"kind": "Prompt", "name": "plan", "version": "1.0.0"},
    }
    event_record = {
        "id": "event-history", "source": "github", "type": "github.issue.created",
        "receivedAt": "2026-01-01T00:00:00Z", "deduplicationKey": "github:delivery:history",
        "repository": {"full_name": "octo/repo"},
    }
    workflow_ref = ResourceRef.from_mapping(refs["workflow"])
    event_ref = ResourceRef.from_mapping(refs["event"])
    task_ref = ResourceRef.from_mapping(refs["task"])
    prompt_ref = ResourceRef.from_mapping(refs["prompt"])
    workspace = Resource(
        ResourceRef("Workspace", "platform", "1.0.0"), Path("workspace.yaml"),
        {"kind": "Workspace", "metadata": {"name": "platform", "version": "1.0.0"}}, (),
    )
    workflow = Resource(
        workflow_ref, Path("workflow.yaml"),
        {"kind": "Workflow", "metadata": {"name": "issue-to-pr", "version": "1.0.0"},
         "spec": {"triggers": [{"eventRef": refs["event"]}], "tasks": [{"taskRef": refs["task"]}]}},
        (event_ref, task_ref),
    )
    event_resource = Resource(
        event_ref, Path("event.yaml"),
        {"kind": "Event", "metadata": {"name": "github-issue-created", "version": "1.0.0"},
         "spec": {"source": "github", "type": "github.issue.created"}}, (),
    )
    task_resource = Resource(
        task_ref, Path("task.yaml"),
        {"kind": "Task", "metadata": {"name": "analyze", "version": "1.0.0"}, "spec": {}},
        (prompt_ref,),
    )
    prompt = Resource(
        prompt_ref, Path("prompt.yaml"),
        {"kind": "Prompt", "metadata": {"name": "plan", "version": "1.0.0"}}, (),
    )
    resources = ResourceCollection(workspace, (workspace, workflow, event_resource, task_resource, prompt))
    store = InMemoryRuntimeObjectStore()
    source_id = "workflowexecution-source"
    source = {
        "apiVersion": "aep.dev/v1alpha1", "kind": "WorkflowExecution", "id": source_id,
        "traceId": "trace-source", "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-01-01T00:00:01Z", "completedAt": "2026-01-01T00:00:01Z",
        "workflowRef": refs["workflow"], "eventRef": refs["event"], "eventId": event_record["id"],
        "repositoryRevision": revision, "knowledgeGraphVersion": "snapshot-source",
        "status": "SUCCEEDED", "taskExecutionIds": ["task-execution-source"],
        "resolvedTaskPlan": [{"taskRef": refs["task"], "dependencies": []}],
        "provenance": {"actor": "test", "repositoryRevision": revision,
                       "knowledgeGraphVersion": "snapshot-source",
                       "resourceRefs": [refs["workflow"], refs["event"], refs["task"], refs["prompt"]]},
    }
    task_execution = {
        "id": "task-execution-source", "kind": "TaskExecution", "traceId": "trace-source",
        "workflowExecutionId": source_id, "taskRef": refs["task"], "attempt": 1,
        "status": "SUCCEEDED", "contextPackageId": "context-source",
    }
    context = {
        "id": "context-source", "kind": "ContextPackage", "traceId": "trace-source",
        "workflowExecutionId": source_id, "taskExecutionId": task_execution["id"],
        "taskRef": refs["task"], "repositoryRevision": revision,
        "provenance": {"actor": "context-builder", "workflowExecutionId": source_id,
                       "taskExecutionId": task_execution["id"], "repositoryRevision": revision,
                       "resourceRefs": [refs["task"]]},
        "elements": [{"type": "event", "truncated": False, "content": event_record}],
        "tokenBudget": 100, "tokenCount": 10, "tokenEstimate": {"count": 10},
        "truncation": "NONE", "selection": {"selected": ["event"], "discarded": []},
    }
    for value, key in ((source, "source"), (task_execution, "task"), (context, "context")):
        store.create(value, deterministic_key=key)
    captured: dict[str, object] = {}
    class FakeManager:
        def provision_orchestration(self, request):
            captured["checkout_request"] = request
            return SimpleNamespace(workspace_path=tmp_path / "pinned"), object()
    runner = object.__new__(DogfoodWorkflowRunner)
    runner._resource_lock = RLock()
    runner._store = store
    runner._resources = resources
    runner._state_root = tmp_path / "state"
    runner._environment = {"AEP_REPOSITORY_OWNER": "octo", "AEP_REPOSITORY_NAME": "repo",
                           "AEP_REPOSITORY_DEFAULT_BRANCH": "main"}
    runner._github = SimpleNamespace(credentials=object())
    runner._checkout_manager = lambda: FakeManager()
    class FakeLoader:
        def __init__(self, repo, schema_root):
            assert schema_root == tmp_path / "pinned" / "schemas" / "resources" / "v1"
        def load(self):
            return resources
    monkeypatch.setattr("aep.dogfood_runtime.ResourceLoader", FakeLoader)
    monkeypatch.setattr(
        "aep.dogfood_runtime.resolve_workflow_for_event",
        lambda event, collection: SimpleNamespace(workflow_ref=workflow_ref),
    )
    runner.run = lambda request: captured.setdefault("dispatch", request) and str(request["workflowExecutionId"])

    replay_id = runner.replay(source_id, context_mode=context_mode)

    assert replay_id != source_id
    assert captured["checkout_request"].base_revision == revision
    dispatch = captured["dispatch"]
    assert dispatch["event"] == event_record
    assert dispatch["publicationEnabled"] is False
    key = ("Task", "analyze", "1.0.0")
    if context_mode == "reuse":
        assert dispatch["reusedContexts"][key]["id"] == "context-source"
    else:
        assert dispatch["expectedContextIdentities"][key] == context_identity(context)
    assert store.get(source_id) == source
    assert store.get(task_execution["id"]) == task_execution
    assert store.get(context["id"]) == context
    assert store.get(replay_id)["status"] == "PENDING"


def event() -> dict[str, object]:
    return {
        "id": "event-dogfood",
        "deduplicationKey": "github:delivery:dogfood",
    }


def test_consumer_retires_outbox_only_after_runner_completes(tmp_path: Path) -> None:
    dispatcher = SQLiteReconciliationDispatcher(tmp_path / "webhook.sqlite3")
    dispatcher.submit(event(), trace_id="trace-dogfood")
    runner = RecordingRunner()
    consumer = DogfoodReconciliationConsumer(dispatcher, runner)

    assert consumer.run_once() == 1

    assert len(runner.requests) == 1
    assert dispatcher.pending_requests() == ()
    assert dispatcher.failure("event-dogfood") is None


def test_consumer_retains_outbox_until_approval_wait_resumes(tmp_path: Path) -> None:
    dispatcher = SQLiteReconciliationDispatcher(tmp_path / "webhook.sqlite3")
    dispatcher.submit(event(), trace_id="trace-dogfood")
    runner = RecordingRunner(approval_pending=True)
    consumer = DogfoodReconciliationConsumer(dispatcher, runner)

    assert consumer.run_once() == 0
    assert len(dispatcher.pending_requests()) == 1
    runner.approval_pending = False
    assert consumer.run_once() == 1
    assert dispatcher.pending_requests() == ()


def test_configuration_failure_is_persisted_and_not_hot_retried(tmp_path: Path) -> None:
    dispatcher = SQLiteReconciliationDispatcher(tmp_path / "webhook.sqlite3")
    dispatcher.submit(event(), trace_id="trace-dogfood")
    runner = RecordingRunner(DogfoodReconciliationError("unsafe details"))
    consumer = DogfoodReconciliationConsumer(dispatcher, runner)

    assert consumer.run_once() == 0

    assert dispatcher.pending_requests() == ()
    assert dispatcher.failure("event-dogfood")["failureClass"] == "CONFIGURATION"
    assert dispatcher.failure("event-dogfood")["message"] == (
        "dogfood reconciliation failed configuration checks"
    )


def test_recoverable_failure_logs_only_safe_diagnostic_fields(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    dispatcher = SQLiteReconciliationDispatcher(tmp_path / "webhook.sqlite3")
    dispatcher.submit(event(), trace_id="trace-dogfood")
    runner = RecordingRunner(RuntimeError("provider-secret-must-not-appear"))
    consumer = DogfoodReconciliationConsumer(dispatcher, runner)

    with caplog.at_level(logging.WARNING, logger="aep.dogfood_runtime"):
        assert consumer.run_once() == 0

    assert len(caplog.messages) == 1
    assert "event_id=event-dogfood" in caplog.messages[0]
    assert "failure_class=RECOVERABLE" in caplog.messages[0]
    assert "failure_code=reconciliation_failed" in caplog.messages[0]
    assert "exception_type=RuntimeError" in caplog.messages[0]
    assert "provider-secret-must-not-appear" not in caplog.text


def test_recoverable_failure_logs_only_first_occurrence_and_transitions(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    dispatcher = SQLiteReconciliationDispatcher(tmp_path / "webhook.sqlite3")
    dispatcher.submit(event(), trace_id="trace-dogfood")
    runner = RecordingRunner(RuntimeError("first unsafe provider detail"))
    consumer = DogfoodReconciliationConsumer(dispatcher, runner)

    with caplog.at_level(logging.WARNING, logger="aep.dogfood_runtime"):
        assert consumer.run_once() == 0
        assert consumer.run_once() == 0
        runner.failure = OSError("second unsafe provider detail")
        assert consumer.run_once() == 0
        assert consumer.run_once() == 0

    assert len(caplog.messages) == 2
    assert "exception_type=RuntimeError" in caplog.messages[0]
    assert "exception_type=OSError" in caplog.messages[1]
    assert "unsafe provider detail" not in caplog.text
    assert len(runner.requests) == 4


def test_reported_failure_state_is_cleared_when_event_leaves_pending_outbox(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    dispatcher = SQLiteReconciliationDispatcher(tmp_path / "webhook.sqlite3")
    dispatcher.submit(event(), trace_id="trace-dogfood")
    runner = RecordingRunner(RuntimeError("unsafe details"))
    consumer = DogfoodReconciliationConsumer(dispatcher, runner)

    with caplog.at_level(logging.WARNING, logger="aep.dogfood_runtime"):
        assert consumer.run_once() == 0
        runner.failure = None
        assert consumer.run_once() == 1
        assert consumer.run_once() == 0

    assert len(caplog.messages) == 1
    assert consumer._reported_failures == {}


def test_reconciliation_reuses_recorded_revision_and_skips_terminal_execution(
    tmp_path: Path,
) -> None:
    store = DurableJsonRuntimeObjectStore(tmp_path / "runtime.json")
    execution_id = "workflowexecution-existing"
    store.create(
        {
            "id": execution_id,
            "kind": "WorkflowExecution",
            "status": "RUNNING",
            "repositoryRevision": "a" * 40,
        },
        deterministic_key="execution-key",
    )
    resolver_called = False

    def resolver() -> str:
        nonlocal resolver_called
        resolver_called = True
        return "b" * 40

    assert _reconciliation_revision(store, execution_id, resolver) == "a" * 40
    assert resolver_called is False
    store.update_status(execution_id, "SUCCEEDED")
    assert _reconciliation_revision(store, execution_id, resolver) is None
    assert resolver_called is False


def test_checkout_configuration_failure_is_terminal(tmp_path: Path) -> None:
    dispatcher = SQLiteReconciliationDispatcher(tmp_path / "webhook.sqlite3")
    dispatcher.submit(event(), trace_id="trace-dogfood")
    runner = RecordingRunner(
        CheckoutProvisionError(
            CheckoutFailureClass.CONFIGURATION,
            "stale_revision",
            "provider details must not be persisted",
        )
    )
    consumer = DogfoodReconciliationConsumer(dispatcher, runner)

    assert consumer.run_once() == 0
    assert dispatcher.pending_requests() == ()
    assert dispatcher.failure("event-dogfood")["failureClass"] == "CONFIGURATION"
    assert dispatcher.failure("event-dogfood")["message"] == (
        "dogfood checkout provisioning failed configuration checks"
    )


def test_polling_failure_does_not_kill_consumer_and_is_visible(tmp_path: Path) -> None:
    dispatcher = SQLiteReconciliationDispatcher(tmp_path / "webhook.sqlite3")
    consumer = DogfoodReconciliationConsumer(dispatcher, RecordingRunner(), poll_seconds=0)
    calls = 0

    def failing_poll() -> None:
        nonlocal calls
        calls += 1
        consumer._stop.set()
        raise OSError("database is locked")

    consumer.run_once = failing_poll  # type: ignore[method-assign]
    consumer._run()

    assert calls == 1
    assert consumer.liveness()["status"] == "degraded"
    assert "database is locked" in consumer.liveness()["lastPollError"]
