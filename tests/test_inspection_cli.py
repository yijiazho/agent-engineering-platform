import json
from pathlib import Path

from aep.inspection_cli import main
from aep.workflow_execution import _runtime_validator


def _checkpoint(tmp_path, objects):
    path = tmp_path / "objects.json"
    path.write_text(json.dumps({"objects": {item["id"]: item for item in objects}, "deterministicKeys": {}, "claims": {}}), encoding="utf-8")
    return path


def _records(status="SUCCEEDED", policy="ALLOW", approval=None):
    workflow = {"id": "wf-1", "kind": "WorkflowExecution", "traceId": "trace-1", "status": status, "createdAt": "2026-01-01T00:00:00Z", "startedAt": "2026-01-01T00:00:00Z", "completedAt": "2026-01-01T00:00:01Z", "workflowRef": {"kind": "Workflow", "name": "issue-to-pr", "version": "1.0.0"}, "repositoryRevision": "a" * 40, "taskExecutionIds": ["task-1"]}
    task = {"id": "task-1", "kind": "TaskExecution", "traceId": "trace-1", "workflowExecutionId": "wf-1", "status": "FAILED" if status == "FAILED" else "SUCCEEDED", "taskRef": {"kind": "Task", "name": "validate", "version": "1.0.0"}, "createdAt": "2026-01-01T00:00:00Z"}
    context = {"id": "ctx-1", "kind": "ContextPackage", "traceId": "trace-1", "workflowExecutionId": "wf-1", "taskExecutionId": "task-1", "createdAt": "2026-01-01T00:00:00Z", "elements": [{"type": "repository", "content": "private source", "provenance": {"source": "test"}, "tokenCount": 4}], "selection": {"selected": ["repository"], "discarded": []}, "tokenEstimate": {"count": 4}, "truncation": "NONE"}
    artifact = {"id": "artifact-1", "kind": "GeneratedArtifact", "traceId": "trace-1", "workflowExecutionId": "wf-1", "taskExecutionId": "task-1", "artifactType": "PATCH", "mediaType": "text/x-diff", "contentAddress": "sha256:" + "b" * 64, "repositoryRevision": "a" * 40, "createdAt": "2026-01-01T00:00:00Z"}
    decision = {"id": "policy-1", "kind": "PolicyDecision", "traceId": "trace-1", "workflowExecutionId": "wf-1", "taskExecutionId": "task-1", "decision": policy, "reason": "publication denied", "createdAt": "2026-01-01T00:00:00Z"}
    records = [workflow, task, context, artifact, decision]
    if status == "FAILED":
        records.append({"id": "evaluation-1", "kind": "EvaluationResult", "workflowExecutionId": "wf-1", "taskExecutionId": "task-1", "status": "SUCCEEDED", "outcome": "FAIL", "evaluationRef": {"kind": "Evaluation", "name": "tests", "version": "1.0.0"}, "createdAt": "2026-01-01T00:00:00Z"})
    if approval:
        records.append({"id": "approval-1", "kind": "Approval", "workflowExecutionId": "wf-1", "policyDecisionId": "policy-1", "status": approval, "requester": "operator", "requestedAt": "2026-01-01T00:00:00Z", "createdAt": "2026-01-01T00:00:00Z"})
    return records


def test_execution_json_summary_and_context_redaction(tmp_path, capsys):
    records = _records()
    records[2]["elements"][0]["content"] = {
        "source": {"path": "private.py"},
        "selectionReasons": ["repository-inventory", "knowledge"],
    }
    state = _checkpoint(tmp_path, records)
    assert main(["--state-file", str(state), "--output", "json", "executions", "show", "wf-1"]) == 0
    execution = json.loads(capsys.readouterr().out)
    assert execution["workflowExecution"]["workflowRef"]["version"] == "1.0.0"
    assert execution["elapsed"]["milliseconds"] == 1000
    assert execution["tasks"][0]["status"] == "SUCCEEDED"
    assert main(["--state-file", str(state), "--output", "json", "contexts", "show", "ctx-1"]) == 0
    context = json.loads(capsys.readouterr().out)
    assert context["elements"][0]["content"] == "[REDACTED]"
    assert context["elements"][0]["selectionReasons"] == ["repository-inventory", "knowledge"]
    assert context["elements"][0]["tokenCount"] == 4


def test_execution_list_discovers_ids_in_deterministic_order_and_filters_status(tmp_path, capsys):
    records = _records()
    later = dict(records[0])
    later.update({"id": "wf-2", "status": "FAILED", "createdAt": "2026-01-02T00:00:00Z"})
    records.append(later)
    state = _checkpoint(tmp_path, records)
    assert main(["--state-file", str(state), "--output", "json", "executions", "list"]) == 0
    listed = json.loads(capsys.readouterr().out)["workflowExecutions"]
    assert [item["id"] for item in listed] == ["wf-1", "wf-2"]
    assert main(["--state-file", str(state), "--output", "json", "executions", "list", "--status", "FAILED"]) == 0
    assert [item["id"] for item in json.loads(capsys.readouterr().out)["workflowExecutions"]] == ["wf-2"]


def test_execution_list_orders_rfc3339_timestamps_by_instant(tmp_path, capsys):
    records = _records()
    later = dict(records[0])
    later.update({"id": "wf-later", "createdAt": "2026-01-01T00:00:00Z"})
    earlier = dict(records[0])
    earlier.update({"id": "wf-earlier", "createdAt": "2026-01-01T01:00:00+02:00"})
    records.extend([later, earlier])
    state = _checkpoint(tmp_path, records)
    assert main(["--state-file", str(state), "--output", "json", "executions", "list"]) == 0
    assert [item["id"] for item in json.loads(capsys.readouterr().out)["workflowExecutions"]] == ["wf-earlier", "wf-1", "wf-later"]


def test_explain_has_deterministic_policy_and_approval_precedence(tmp_path, capsys):
    state = _checkpoint(tmp_path, _records(status="FAILED", policy="DENY"))
    assert main(["--state-file", str(state), "--output", "json", "explain", "wf-1"]) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "DENIED"
    state = _checkpoint(tmp_path, _records(status="FAILED", policy="REQUIRE_APPROVAL", approval="PENDING"))
    assert main(["--state-file", str(state), "--output", "json", "explain", "wf-1"]) == 0
    explanation = json.loads(capsys.readouterr().out)
    assert explanation["outcome"] == "APPROVAL_PENDING"
    assert explanation["decisiveEvidence"]["id"] == "approval-1"


def test_explain_denial_outranks_approval_and_approved_requirement_is_not_pending(tmp_path, capsys):
    records = _records(status="FAILED", policy="DENY", approval="PENDING")
    records.append({"id": "policy-2", "kind": "PolicyDecision", "workflowExecutionId": "wf-1", "taskExecutionId": "task-1", "decision": "REQUIRE_APPROVAL", "reason": "review", "createdAt": "2026-01-01T00:00:01Z"})
    state = _checkpoint(tmp_path, records)
    assert main(["--state-file", str(state), "--output", "json", "explain", "wf-1"]) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "DENIED"
    records = _records(policy="REQUIRE_APPROVAL", approval="APPROVED")
    state = _checkpoint(tmp_path, records)
    assert main(["--state-file", str(state), "--output", "json", "explain", "wf-1"]) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "SUCCEEDED"


def test_explain_ignores_failed_superseded_task_attempt(tmp_path, capsys):
    records = _records()
    failed = dict(records[1])
    failed.update({"id": "task-old", "attempt": 1, "status": "FAILED"})
    records[1].update({"attempt": 2, "status": "SUCCEEDED"})
    records.append(failed)
    state = _checkpoint(tmp_path, records)
    assert main(["--state-file", str(state), "--output", "json", "explain", "wf-1"]) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "SUCCEEDED"


def test_explain_identifies_validation_failure(tmp_path, capsys):
    state = _checkpoint(tmp_path, _records(status="FAILED"))
    assert main(["--state-file", str(state), "--output", "json", "explain", "wf-1"]) == 0
    explanation = json.loads(capsys.readouterr().out)
    assert explanation["outcome"] == "FAILED"
    assert explanation["decisiveEvidence"]["id"] == "evaluation-1"


def test_missing_or_wrong_kind_identifier_has_stable_json_error(tmp_path, capsys):
    state = _checkpoint(tmp_path, _records())
    assert main(["--state-file", str(state), "tasks", "show", "missing"]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "RUNTIME_OBJECT_NOT_FOUND"
    assert main(["--state-file", str(state), "artifacts", "show", "task-1"]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "RUNTIME_KIND_MISMATCH"
    assert main(["--state-file", str(state), "--output", "json", "tasks", "show"]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "INVALID_ARGUMENT"
    assert main(["--state-file", str(state), "--output=json", "tasks", "show"]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "INVALID_ARGUMENT"


def test_artifact_metadata_is_safe_and_unsafe_debug_is_explicit(tmp_path, capsys):
    state = _checkpoint(tmp_path, _records())
    assert main(["--state-file", str(state), "--output", "json", "artifacts", "show", "artifact-1"]) == 0
    artifact = json.loads(capsys.readouterr().out)
    assert artifact["mediaType"] == "text/x-diff"
    assert artifact["contentAddress"].startswith("sha256:")
    assert main(["--state-file", str(state), "--unsafe-debug", "--output", "json", "contexts", "show", "ctx-1"]) == 0
    assert json.loads(capsys.readouterr().out)["elements"][0]["content"] == "private source"


def test_execution_checks_task_references_and_redacts_evaluation_logs(tmp_path, capsys):
    records = _records()
    records[1]["contextPackageId"] = "missing-context"
    state = _checkpoint(tmp_path, records)
    assert main(["--state-file", str(state), "executions", "show", "wf-1"]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "RUNTIME_REFERENCE_MALFORMED"
    records = _records(status="FAILED")
    records[-1]["logs"] = ["private rejected output"]
    records[1]["failure"] = {"message": "private model output", "class": "EVALUATION"}
    state = _checkpoint(tmp_path, records)
    assert main(["--state-file", str(state), "--output", "json", "evaluations", "show", "evaluation-1"]) == 0
    assert json.loads(capsys.readouterr().out)["logs"] == "[REDACTED]"
    assert main(["--state-file", str(state), "--output", "json", "tasks", "show", "task-1"]) == 0
    assert json.loads(capsys.readouterr().out)["failure"]["message"] == "[REDACTED]"


def test_explain_reports_transitively_blocked_dependencies_and_rejects_foreign_evidence(tmp_path, capsys):
    records = _records(status="FAILED")
    records[1]["status"] = "FAILED"
    records[0]["resolvedTaskPlan"] = [
        {"taskRef": records[1]["taskRef"], "dependencies": []},
        {"taskRef": {"kind": "Task", "name": "publish", "version": "1.0.0"}, "dependencies": [records[1]["taskRef"]]},
        {"taskRef": {"kind": "Task", "name": "notify", "version": "1.0.0"}, "dependencies": [{"kind": "Task", "name": "publish", "version": "1.0.0"}]},
    ]
    state = _checkpoint(tmp_path, records)
    assert main(["--state-file", str(state), "--output", "json", "explain", "wf-1"]) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "BLOCKED"
    assert main(["--state-file", str(state), "--output", "json", "executions", "show", "wf-1"]) == 0
    blocked = [item["taskRef"]["name"] for item in json.loads(capsys.readouterr().out)["tasks"] if item["status"] == "BLOCKED"]
    assert blocked == ["publish", "notify"]
    records = _records()
    records[1]["contextPackageId"] = "ctx-1"
    records[2]["workflowExecutionId"] = "other-workflow"
    state = _checkpoint(tmp_path, records)
    assert main(["--state-file", str(state), "executions", "show", "wf-1"]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "RUNTIME_REFERENCE_MALFORMED"


def test_execution_rejects_broken_nested_runtime_reference(tmp_path, capsys):
    records = _records()
    records[1]["agentInvocationIds"] = ["agent-1"]
    records.append({
        "id": "agent-1", "kind": "AgentInvocation", "traceId": "trace-1",
        "workflowExecutionId": "wf-1", "taskExecutionId": "task-1",
        "modelInvocationIds": ["missing-model"], "toolInvocationIds": [],
        "createdAt": "2026-01-01T00:00:00Z",
    })
    state = _checkpoint(tmp_path, records)
    assert main(["--state-file", str(state), "executions", "show", "wf-1"]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "RUNTIME_REFERENCE_MALFORMED"


def test_execution_allows_cross_task_publication_evidence(tmp_path, capsys):
    records = _records()
    records[1]["policyDecisionIds"] = ["policy-1"]
    records[3]["taskExecutionId"] = "upstream-task"
    records[4]["generatedArtifactIds"] = ["artifact-1"]
    records.append({
        "id": "evaluation-1", "kind": "EvaluationResult", "traceId": "trace-1",
        "workflowExecutionId": "wf-1", "taskExecutionId": "upstream-task",
        "status": "SUCCEEDED", "outcome": "PASS", "createdAt": "2026-01-01T00:00:00Z",
    })
    records[4]["evaluationResultIds"] = ["evaluation-1"]
    state = _checkpoint(tmp_path, records)
    assert main(["--state-file", str(state), "executions", "show", "wf-1"]) == 0
    assert "Workflow execution: wf-1" in capsys.readouterr().out


def test_execution_keeps_prompt_reference_metadata_visible(tmp_path, capsys):
    records = _records()
    records.append({"id": "agent-1", "kind": "ResolvedAgent", "traceId": "trace-1", "taskExecutionId": "task-1", "promptRef": {"kind": "Prompt", "name": "safe-prompt", "version": "1.0.0"}, "toolRefs": [{"kind": "Tool", "name": "unused-tool", "version": "1.0.0"}], "provenance": {"workflowExecutionId": "wf-1"}})
    state = _checkpoint(tmp_path, records)
    assert main(["--state-file", str(state), "--output", "json", "executions", "show", "wf-1"]) == 0
    related = json.loads(capsys.readouterr().out)["relatedObjects"]
    assert next(item for item in related if item["id"] == "agent-1")["promptRef"]["version"] == "1.0.0"
    assert next(item for item in related if item["id"] == "agent-1")["toolRefs"][0]["name"] == "unused-tool"
    assert main(["--state-file", str(state), "executions", "show", "wf-1"]) == 0
    human = capsys.readouterr().out
    assert "Workflow: issue-to-pr:1.0.0" in human and "Elapsed: 1000 ms" in human


def test_human_execution_view_includes_evidence_ids_and_synthetic_edges(tmp_path, capsys):
    records = _records(status="FAILED")
    records[1].update({"contextPackageId": "ctx-1", "generatedArtifactIds": ["artifact-1"], "policyDecisionIds": ["policy-1"]})
    records[0]["resolvedTaskPlan"] = [
        {"taskRef": records[1]["taskRef"], "dependencies": []},
        {"taskRef": {"kind": "Task", "name": "publish", "version": "1.0.0"}, "dependencies": [records[1]["taskRef"]]},
    ]
    state = _checkpoint(tmp_path, records)
    assert main(["--state-file", str(state), "executions", "show", "wf-1"]) == 0
    human = capsys.readouterr().out
    assert "contextPackageId=ctx-1" in human and "policyDecisionIds=policy-1" in human
    assert "depends on validate:1.0.0" in human
    assert main(["--state-file", str(state), "--output", "json", "executions", "show", "wf-1"]) == 0
    tasks = json.loads(capsys.readouterr().out)["tasks"]
    publish = next(item for item in tasks if item["taskRef"]["name"] == "publish")
    assert publish["dependencyTaskRefs"] == [records[1]["taskRef"]]


def test_runtime_checkpoint_fixture_is_schema_valid():
    fixture = Path(__file__).parents[1] / "fixtures" / "runtime" / "inspection-cli-checkpoint.json"
    for value in json.loads(fixture.read_text(encoding="utf-8"))["objects"].values():
        validator = _runtime_validator(f"{str(value['kind']).lower()}.schema.json")
        assert not list(validator.iter_errors(value))
