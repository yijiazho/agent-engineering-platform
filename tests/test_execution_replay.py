import json

from aep.execution_replay import ExecutionReplayService, ReplayError
from aep.inspection_cli import main
from aep.runtime_store import InMemoryRuntimeObjectStore


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
    assert replay["replayProvenance"]["resourceRefs"][-1]["version"] == "2.0.0"
    try:
        ExecutionReplayService(store).replay("wf-source", overrides=["Model:current:latest"])
    except ReplayError:
        pass
    else:
        raise AssertionError("floating override was accepted")


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
    store, source = _store()
    task = {"id": "task-source", "kind": "TaskExecution", "workflowExecutionId": source["id"],
            "contextPackageId": "context-missing"}
    store.create(task, deterministic_key="task")
    try:
        ExecutionReplayService(store).replay("wf-source")
    except ReplayError as error:
        assert "ContextPackage" in str(error)
    else:
        raise AssertionError("missing historical ContextPackage was accepted")


def test_cli_replay_persists_execution(tmp_path, capsys):
    store, source = _store()
    path = tmp_path / "objects.json"
    path.write_text(json.dumps({"objects": {source["id"]: source}, "deterministicKeys": {"source": source["id"]}, "claims": {}, "objectOrder": [source["id"]]}), encoding="utf-8")
    assert main(["--state-file", str(path), "--output", "json", "replay", "wf-source"]) == 0
    result = json.loads(capsys.readouterr().out)["workflowExecution"]
    assert result["sourceExecutionId"] == "wf-source"
    checkpoint = json.loads(path.read_text(encoding="utf-8"))
    assert len(checkpoint["objects"]) == 2
