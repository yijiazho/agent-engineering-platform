import json

from aep.execution_replay import ExecutionReplayService, ReplayError
from aep.inspection_cli import main
from aep.runtime_store import InMemoryRuntimeObjectStore
from aep.workflow_execution import _runtime_validator


def _store():
    store = InMemoryRuntimeObjectStore()
    source = {"apiVersion": "aep.dev/v1alpha1", "kind": "WorkflowExecution", "id": "wf-source",
        "traceId": "trace-source", "createdAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:00Z",
        "workflowRef": {"kind": "Workflow", "name": "build", "version": "1.0.0"},
        "eventRef": {"kind": "Event", "name": "trigger", "version": "1.0.0"}, "eventId": "event-1",
        "repositoryRevision": "a" * 40, "knowledgeGraphVersion": "snapshot-1", "status": "SUCCEEDED",
        "taskExecutionIds": [], "provenance": {"actor": "test", "resourceRefs": [
            {"kind": "Workflow", "name": "build", "version": "1.0.0"},
            {"kind": "Event", "name": "trigger", "version": "1.0.0"},
            {"kind": "Prompt", "name": "plan", "version": "1.0.0"}], "repositoryRevision": "a" * 40}}
    store.create(source, deterministic_key="source")
    return store, source


def test_exact_replay_is_distinct_and_preserves_lineage_and_inputs():
    store, source = _store()
    replay = ExecutionReplayService(store).replay("wf-source", timestamp="2026-01-02T00:00:00Z")
    assert replay["id"] != source["id"]
    assert replay["sourceExecutionId"] == source["id"]
    assert replay["repositoryRevision"] == source["repositoryRevision"]
    assert replay["replayProvenance"]["publicationMode"] == "disabled"
    assert store.get("wf-source") == source


def test_override_requires_versioned_known_resource_and_records_provenance():
    store, _ = _store()
    replay = ExecutionReplayService(store).replay("wf-source", overrides=["Prompt:plan:2.0.0"])
    assert replay["replayProvenance"]["overrides"] == [{"kind": "Prompt", "name": "plan", "version": "2.0.0"}]
    assert next(ref for ref in replay["replayProvenance"]["resourceRefs"] if ref["kind"] == "Prompt")["version"] == "2.0.0"
    try:
        ExecutionReplayService(store).replay("wf-source", overrides=["Model:current:latest"])
    except ReplayError:
        pass
    else:
        raise AssertionError("floating override was accepted")
    for invalid in ("Prompt:plan:not-a-version", "Prompt:plan:LATEST"):
        try:
            ExecutionReplayService(store).replay("wf-source", overrides=[invalid])
        except ReplayError:
            pass
        else:
            raise AssertionError(f"invalid override version was accepted: {invalid}")


def test_exact_replay_fails_closed_without_historical_dependencies():
    store, source = _store()
    broken = dict(source); broken["id"] = "wf-broken"; broken["repositoryRevision"] = ""
    store.create(broken, deterministic_key="broken")
    try:
        ExecutionReplayService(store).replay("wf-broken")
    except ReplayError as error:
        assert "historical" in str(error)
    else:
        raise AssertionError("missing revision was accepted")


def test_reuse_context_rejects_missing_historical_context():
    store = InMemoryRuntimeObjectStore()
    _, source = _store()
    source["taskExecutionIds"] = ["task-source"]
    store.create(source, deterministic_key="source")
    task = {"id": "task-source", "kind": "TaskExecution", "workflowExecutionId": source["id"],
            "contextPackageId": "context-missing"}
    store.create(task, deterministic_key="task")
    try:
        ExecutionReplayService(store).replay("wf-source")
    except ReplayError as error:
        assert "ContextPackage" in str(error)
    else:
        raise AssertionError("missing historical ContextPackage was accepted")


def test_replay_requires_terminal_source_and_collects_related_resource_refs():
    store, source = _store()
    running = dict(source); running.update({"id": "wf-running", "status": "RUNNING"})
    store.create(running, deterministic_key="running")
    try:
        ExecutionReplayService(store).replay("wf-running")
    except ReplayError as error:
        assert "terminal" in str(error)
    else:
        raise AssertionError("nonterminal source was replayed")
    related = {"id": "resolved-1", "kind": "ResolvedAgent", "workflowExecutionId": "wf-source",
               "provenance": {"resourceRefs": [{"kind": "Agent", "name": "planner", "version": "1.0.0"},
                   {"kind": "Model", "name": "reasoner", "version": "2.1.0"}]},
               "promptRef": {"kind": "Prompt", "name": "plan", "version": "1.0.0"}}
    store.create(related, deterministic_key="resolved")
    replay = ExecutionReplayService(store).replay("wf-source", overrides=["Prompt:plan:3.0.0"])
    refs = replay["replayProvenance"]["resourceRefs"]
    assert {ref["kind"] for ref in refs} >= {"Agent", "Model", "Prompt", "Workflow", "Event"}
    assert next(ref for ref in refs if ref["kind"] == "Prompt")["version"] == "3.0.0"
    assert "resolvedTaskPlan" not in replay


def test_replay_collects_unstarted_plan_task_refs_and_preserves_plan():
    _, source = _store()
    source["resolvedTaskPlan"] = [
        {"taskRef": {"kind": "Task", "name": "compile", "version": "1.0.0"}, "dependencies": []},
        {"taskRef": {"kind": "Task", "name": "publish", "version": "2.0.0"},
         "dependencies": [{"kind": "Task", "name": "compile", "version": "1.0.0"}]},
    ]
    store = InMemoryRuntimeObjectStore()
    store.create(source, deterministic_key="source-with-plan")
    replay = ExecutionReplayService(store).replay("wf-source")
    assert replay["resolvedTaskPlan"] == source["resolvedTaskPlan"]
    task_versions = {(ref["name"], ref["version"]) for ref in replay["replayProvenance"]["resourceRefs"] if ref["kind"] == "Task"}
    assert task_versions == {("compile", "1.0.0"), ("publish", "2.0.0")}


def test_context_reuse_reads_provenance_ownership_and_collects_knowledgebase_refs():
    store = InMemoryRuntimeObjectStore()
    _, source = _store()
    source["taskExecutionIds"] = ["task-source"]
    store.create(source, deterministic_key="source")
    store.create({"id": "task-source", "kind": "TaskExecution", "workflowExecutionId": "wf-source",
                  "contextPackageId": "context-source", "taskRef": {"kind": "Task", "name": "plan", "version": "1.0.0"}},
                 deterministic_key="task")
    store.create({"id": "context-source", "kind": "ContextPackage", "provenance": {
                    "workflowExecutionId": "wf-source", "taskExecutionId": "task-source",
                    "resourceRefs": [{"kind": "Task", "name": "plan", "version": "1.0.0"}]},
                  "elements": [{"type": "knowledge", "provenance": {"resourceRefs": [
                      {"kind": "KnowledgeBase", "name": "repo-docs", "version": "1.2.0"}]}}]},
                 deterministic_key="context")
    replay = ExecutionReplayService(store).replay("wf-source")
    refs = replay["replayProvenance"]["resourceRefs"]
    assert {ref["kind"] for ref in refs} >= {"KnowledgeBase", "Task"}


def test_invalid_timestamp_fails_before_store_mutation():
    store, _ = _store()
    before = len(store.list_by_workflow_execution("wf-source"))
    try:
        ExecutionReplayService(store).replay("wf-source", timestamp="not-rfc3339")
    except ReplayError as error:
        assert "RFC3339" in str(error)
    else:
        raise AssertionError("invalid replay timestamp was accepted")
    assert len(store.list_by_workflow_execution("wf-source")) == before


def test_atomic_runtime_create_rolls_back_if_initial_event_is_invalid():
    store = InMemoryRuntimeObjectStore()
    value = {"id": "execution-1", "kind": "WorkflowExecution"}
    try:
        store.create_with_event(value, {"id": "event-1", "kind": "WrongKind"}, deterministic_key="execution-1")
    except ValueError:
        pass
    else:
        raise AssertionError("invalid initial event was accepted")
    assert store.get("execution-1") is None


def test_cli_replay_persists_execution(tmp_path, capsys):
    store, source = _store()
    path = tmp_path / "objects.json"
    path.write_text(json.dumps({"objects": {source["id"]: source}, "deterministicKeys": {"source": source["id"]}, "claims": {}, "objectOrder": [source["id"]]}), encoding="utf-8")
    assert main(["--state-file", str(path), "--output", "json", "replay", "wf-source"]) == 0
    result = json.loads(capsys.readouterr().out)["workflowExecution"]
    assert result["sourceExecutionId"] == "wf-source"
    checkpoint = json.loads(path.read_text(encoding="utf-8"))
    assert len(checkpoint["objects"]) == 3
    events = [value for value in checkpoint["objects"].values() if value["kind"] == "ExecutionEvent"]
    assert len(events) == 1
    replay_record = next(value for value in checkpoint["objects"].values() if value.get("sourceExecutionId") == "wf-source")
    _runtime_validator("workflowexecution.schema.json").validate(replay_record)
    _runtime_validator("executionevent.schema.json").validate(events[0])


def test_cli_translates_durable_checkpoint_errors_to_json(tmp_path, capsys):
    path = tmp_path / "malformed.json"
    path.write_text("not-json", encoding="utf-8")
    assert main(["--state-file", str(path), "--output", "json", "replay", "wf-source"]) == 2
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "RUNTIME_STORE_ERROR"
    assert error["message"] == "runtime store operation failed"


def test_cli_replay_redacts_sensitive_extension_fields_by_default(tmp_path, capsys):
    _, source = _store()
    source["prompt"] = "private prompt text"
    path = tmp_path / "objects.json"
    path.write_text(json.dumps({"objects": {source["id"]: source}, "deterministicKeys": {"source": source["id"]}, "claims": {}, "objectOrder": [source["id"]]}), encoding="utf-8")
    assert main(["--state-file", str(path), "--output", "json", "replay", "wf-source"]) == 0
    safe = json.loads(capsys.readouterr().out)["workflowExecution"]
    assert safe["prompt"] == "[REDACTED]"
    assert main(["--state-file", str(path), "--unsafe-debug", "--output", "json", "replay", "wf-source"]) == 0
    unsafe = json.loads(capsys.readouterr().out)["workflowExecution"]
    assert unsafe["prompt"] == "private prompt text"
