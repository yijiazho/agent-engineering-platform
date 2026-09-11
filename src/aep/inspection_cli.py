"""Safe, deterministic inspection of persisted AEP runtime evidence."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any


KIND_COMMANDS = {
    "executions": "WorkflowExecution", "tasks": "TaskExecution",
    "contexts": "ContextPackage", "invocations": None,
    "artifacts": "GeneratedArtifact", "evaluations": "EvaluationResult",
    "policy-decisions": "PolicyDecision", "approvals": "Approval",
}
INVOCATION_KINDS = {"AgentInvocation", "ModelInvocation", "ToolInvocation"}
SENSITIVE_FIELDS = frozenset({"content", "output", "input", "payload", "body", "prompt", "credentials", "token", "secret"})
TASK_REFERENCE_FIELDS = {
    "contextPackageId": "ContextPackage", "resolvedAgentId": "ResolvedAgent",
    "agentInvocationIds": "AgentInvocation", "toolInvocationIds": "ToolInvocation",
    "generatedArtifactIds": "GeneratedArtifact", "evaluationResultIds": "EvaluationResult",
    "policyDecisionIds": "PolicyDecision", "dependencyTaskExecutionIds": "TaskExecution",
}


class InspectionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _ArgumentParser(argparse.ArgumentParser):
    _json_errors_default = False

    def __init__(self, *args: Any, json_errors: bool | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._json_errors = self._json_errors_default if json_errors is None else json_errors

    def error(self, message: str) -> None:
        if self._json_errors:
            raise InspectionError("INVALID_ARGUMENT", message)
        super().error(message)


class RuntimeEvidenceReader:
    """Read the durable checkpoint directly, without starting a runtime worker."""

    def __init__(self, state_file: Path | str) -> None:
        self._state_file = Path(state_file)

    def objects(self) -> dict[str, dict[str, Any]]:
        if not self._state_file.is_file():
            raise InspectionError("RUNTIME_STORE_NOT_FOUND", "runtime checkpoint was not found")
        try:
            value = json.loads(self._state_file.read_text(encoding="utf-8"))
            objects = value["objects"]
        except (OSError, ValueError, KeyError, TypeError):
            raise InspectionError("RUNTIME_STORE_INVALID", "runtime checkpoint is malformed") from None
        if not isinstance(objects, Mapping) or not all(
            isinstance(key, str) and isinstance(item, Mapping) for key, item in objects.items()
        ):
            raise InspectionError("RUNTIME_STORE_INVALID", "runtime checkpoint is malformed")
        return {key: deepcopy(dict(item)) for key, item in objects.items()}


class ExecutionInspector:
    def __init__(self, objects: Mapping[str, Mapping[str, Any]]) -> None:
        self._objects = {key: deepcopy(dict(value)) for key, value in objects.items()}

    def show(self, object_id: str, expected_kind: str | None, *, unsafe: bool = False) -> dict[str, Any]:
        value = self._get(object_id)
        if expected_kind is not None and value.get("kind") != expected_kind:
            raise InspectionError("RUNTIME_KIND_MISMATCH", "runtime object has an unexpected kind")
        if expected_kind is None and value.get("kind") not in INVOCATION_KINDS:
            raise InspectionError("RUNTIME_KIND_MISMATCH", "runtime object is not an invocation")
        if value.get("kind") == "WorkflowExecution":
            return self.execution(object_id, unsafe=unsafe)
        return self._safe(value, unsafe=unsafe)

    def list_executions(self, *, status: str | None = None) -> dict[str, Any]:
        """Return deterministic, safe execution discovery records."""

        if status is not None and not status.strip():
            raise InspectionError("INVALID_STATUS_FILTER", "status filter must be a non-empty string")
        executions = [
            value for value in self._objects.values()
            if value.get("kind") == "WorkflowExecution"
            and (status is None or value.get("status") == status)
        ]
        return {"workflowExecutions": [_summary(value) for value in _ordered(executions)]}

    def execution(self, execution_id: str, *, unsafe: bool = False) -> dict[str, Any]:
        workflow = self._get(execution_id)
        if workflow.get("kind") != "WorkflowExecution":
            raise InspectionError("RUNTIME_KIND_MISMATCH", "runtime object is not a WorkflowExecution")
        task_ids = workflow.get("taskExecutionIds", [])
        if not isinstance(task_ids, list):
            raise InspectionError("RUNTIME_REFERENCE_MALFORMED", "WorkflowExecution task references are malformed")
        tasks = [self._reference(item, "TaskExecution") for item in task_ids]
        for task in tasks:
            task_value = self._get(str(task["id"]))
            self._validate_owner(task_value, workflow, task_id=None)
            self._validate_task_references(task_value, workflow)
        tasks.extend(_blocked_plan_nodes(workflow, [self._get(str(task["id"])) for task in tasks]))
        related = [item for item in self._objects.values() if _belongs_to_workflow(item, execution_id)]
        return self._safe({
            "workflowExecution": workflow,
            "elapsed": _elapsed(workflow),
            "tasks": tasks,
            "relatedObjects": [_summary(item) for item in _ordered(related)],
        }, unsafe=unsafe)

    def explain(self, execution_id: str) -> dict[str, Any]:
        execution = self._get(execution_id)
        if execution.get("kind") != "WorkflowExecution":
            raise InspectionError("RUNTIME_KIND_MISMATCH", "runtime object is not a WorkflowExecution")
        self.execution(execution_id)
        related = _ordered(item for item in self._objects.values() if _belongs_to_workflow(item, execution_id))
        approvals = [item for item in related if item.get("kind") == "Approval"]
        policies = [item for item in related if item.get("kind") == "PolicyDecision"]
        denials = [item for item in policies if item.get("decision") == "DENY"]
        required = [item for item in policies if item.get("decision") == "REQUIRE_APPROVAL"]
        approval_by_policy = {item.get("policyDecisionId"): item for item in approvals}
        unresolved = [item for item in required if approval_by_policy.get(item.get("id"), {}).get("status") in (None, "PENDING")]
        rejected = [approval_by_policy[item.get("id")] for item in required if approval_by_policy.get(item.get("id"), {}).get("status") in ("REJECTED", "EXPIRED")]
        effective_tasks = _effective_tasks(related)
        effective_ids = {item["id"] for item in effective_tasks}
        failed_tasks = [item for item in effective_tasks if item.get("status") == "FAILED"]
        failed_evaluations = [item for item in related if item.get("kind") == "EvaluationResult" and item.get("outcome") == "FAIL" and item.get("taskExecutionId") in effective_ids]
        blocked = [item for item in effective_tasks if item.get("status") in {"PENDING", "QUEUED"} and any(
            self._objects.get(dependency_id, {}).get("status") == "FAILED"
            for dependency_id in item.get("dependencyTaskExecutionIds", ())
        )]
        blocked.extend(_blocked_plan_nodes(execution, effective_tasks))
        if denials:
            decisive, outcome, reason = denials[0], "DENIED", str(denials[0].get("reason", "policy denied action"))
        elif rejected:
            decisive, outcome, reason = rejected[0], "DENIED", f"approval {str(rejected[0].get('status')).lower()}"
        elif unresolved:
            approval = approval_by_policy.get(unresolved[0].get("id"))
            decisive, outcome, reason = approval or unresolved[0], "APPROVAL_PENDING", "policy requires approval"
        elif execution.get("status") == "SUCCEEDED":
            decisive, outcome, reason = execution, "SUCCEEDED", "all recorded tasks completed successfully"
        elif blocked:
            decisive, outcome, reason = blocked[0], "BLOCKED", "a prerequisite task failed"
        elif failed_evaluations:
            decisive, outcome, reason = failed_evaluations[0], "FAILED", "evaluation failed"
        elif failed_tasks:
            decisive, outcome, reason = failed_tasks[0], "FAILED", "task execution failed"
        else:
            decisive, outcome, reason = execution, str(execution.get("status", "UNKNOWN")), "workflow has no more specific terminal evidence"
        return {"workflowExecutionId": execution_id, "outcome": outcome, "reason": reason,
                "decisiveEvidence": self._safe(_summary(decisive), unsafe=False), "evidence": self._safe(decisive, unsafe=False)}

    def _get(self, object_id: str) -> dict[str, Any]:
        if not isinstance(object_id, str) or not object_id.strip():
            raise InspectionError("INVALID_IDENTIFIER", "identifier must be a non-empty string")
        value = self._objects.get(object_id)
        if value is None:
            raise InspectionError("RUNTIME_OBJECT_NOT_FOUND", "runtime object was not found")
        if value.get("id") != object_id or not isinstance(value.get("kind"), str):
            raise InspectionError("RUNTIME_OBJECT_MALFORMED", "runtime object is malformed")
        return value

    def _reference(self, object_id: Any, kind: str) -> dict[str, Any]:
        if not isinstance(object_id, str):
            raise InspectionError("RUNTIME_REFERENCE_MALFORMED", "runtime reference is malformed")
        try:
            value = self._get(object_id)
        except InspectionError as error:
            if error.code == "RUNTIME_OBJECT_NOT_FOUND":
                raise InspectionError("RUNTIME_REFERENCE_MALFORMED", "task runtime reference was not found") from None
            raise
        if value.get("kind") != kind:
            raise InspectionError("RUNTIME_REFERENCE_MALFORMED", "runtime reference has an unexpected kind")
        return _summary(value)

    def _validate_task_references(self, task: Mapping[str, Any], workflow: Mapping[str, Any]) -> None:
        for field, kind in TASK_REFERENCE_FIELDS.items():
            value = task.get(field)
            if value is None:
                continue
            references = value if field.endswith("Ids") else [value]
            if not isinstance(references, list):
                raise InspectionError("RUNTIME_REFERENCE_MALFORMED", "task runtime reference is malformed")
            for object_id in references:
                self._reference(object_id, kind)
                self._validate_owner(self._get(object_id), workflow, task_id=None if kind == "TaskExecution" else str(task["id"]))

    def _validate_owner(self, value: Mapping[str, Any], workflow: Mapping[str, Any], *, task_id: str | None) -> None:
        provenance = value.get("provenance")
        workflow_id = value.get("workflowExecutionId") or (provenance.get("workflowExecutionId") if isinstance(provenance, Mapping) else None)
        trace_id = value.get("traceId")
        if workflow_id != workflow.get("id") or trace_id != workflow.get("traceId"):
            raise InspectionError("RUNTIME_REFERENCE_MALFORMED", "runtime reference belongs to another execution")
        if task_id is None:
            return
        owner_task_id = value.get("taskExecutionId") or (provenance.get("taskExecutionId") if isinstance(provenance, Mapping) else None)
        if owner_task_id != task_id:
            raise InspectionError("RUNTIME_REFERENCE_MALFORMED", "runtime reference belongs to another task")

    def _safe(self, value: Any, *, unsafe: bool) -> Any:
        if unsafe:
            return deepcopy(value)
        if isinstance(value, Mapping):
            return {str(key): ("[REDACTED]" if _is_sensitive_key(str(key)) else self._safe(item, unsafe=False)) for key, item in value.items()}
        if isinstance(value, list):
            return [self._safe(item, unsafe=False) for item in value]
        return deepcopy(value)


def _summary(value: Mapping[str, Any]) -> dict[str, Any]:
    fields = ("id", "kind", "status", "outcome", "decision", "eventId", "eventRef", "taskRef", "workflowRef", "agentRef", "promptRef", "modelRef", "toolRef", "toolRefs", "policyRefs", "evaluationRef", "repositoryRevision", "createdAt", "completedAt", "failure", "reason", "mediaType", "contentAddress", "artifactType", "resolvedAgentId", "contextPackageId", "dependencyTaskExecutionIds", "agentInvocationIds", "toolInvocationIds", "generatedArtifactIds", "evaluationResultIds", "policyDecisionIds", "provenance")
    return {field: deepcopy(value[field]) for field in fields if field in value}


def _belongs_to_workflow(value: Mapping[str, Any], workflow_execution_id: str) -> bool:
    if value.get("workflowExecutionId") == workflow_execution_id:
        return True
    provenance = value.get("provenance")
    return isinstance(provenance, Mapping) and provenance.get("workflowExecutionId") == workflow_execution_id


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("_", "").replace("-", "")
    if normalized in {"contentaddress", "inputaddress", "outputaddress", "logsaddress", "evidenceaddress", "tokencount", "tokenbudget", "tokenestimate", "tokenusage", "tokenlimit", "promptref"}:
        return False
    return normalized in SENSITIVE_FIELDS or normalized in {"logs", "message"} or any(
        term in normalized for term in ("content", "body", "prompt", "credential", "secret", "token", "authorization", "password")
    )


def _ordered(values: Sequence[Mapping[str, Any]] | Any) -> list[dict[str, Any]]:
    return [dict(item) for item in sorted(values, key=lambda item: (str(item.get("createdAt", "")), str(item.get("id", ""))))]


def _effective_tasks(values: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[str, Mapping[str, Any]] = {}
    for value in values:
        if value.get("kind") != "TaskExecution":
            continue
        ref = value.get("taskRef")
        key = json.dumps(ref, sort_keys=True, separators=(",", ":")) if isinstance(ref, Mapping) else str(value.get("id"))
        prior = latest.get(key)
        if prior is None or (int(value.get("attempt", 0)), str(value.get("id", ""))) > (int(prior.get("attempt", 0)), str(prior.get("id", ""))):
            latest[key] = value
    return _ordered(latest.values())


def _blocked_plan_nodes(execution: Mapping[str, Any], tasks: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    plan = execution.get("resolvedTaskPlan")
    if not isinstance(plan, list):
        return []
    by_ref = {json.dumps(item.get("taskRef"), sort_keys=True): item for item in tasks}
    blocked: list[dict[str, Any]] = []
    for node in plan:
        if not isinstance(node, Mapping) or not isinstance(node.get("taskRef"), Mapping):
            continue
        key = json.dumps(node["taskRef"], sort_keys=True)
        if key in by_ref:
            continue
        dependencies = node.get("dependencies", ())
        if any(by_ref.get(json.dumps(dependency, sort_keys=True), {}).get("status") == "FAILED" for dependency in dependencies if isinstance(dependency, Mapping)):
            blocked.append({"id": f"blocked:{key}", "kind": "TaskExecution", "taskRef": dict(node["taskRef"]), "status": "BLOCKED"})
    return blocked


def _elapsed(value: Mapping[str, Any]) -> dict[str, Any]:
    started, completed = value.get("startedAt", value.get("createdAt")), value.get("completedAt")
    result: dict[str, Any] = {"startedAt": started, "completedAt": completed}
    try:
        if isinstance(started, str) and isinstance(completed, str):
            result["milliseconds"] = int((datetime.fromisoformat(completed.replace("Z", "+00:00")) - datetime.fromisoformat(started.replace("Z", "+00:00"))).total_seconds() * 1000)
    except ValueError:
        pass
    return result


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    json_errors = any(arguments[index:index + 2] == ["--output", "json"] or arguments[index] == "--output=json" for index in range(len(arguments)))
    _ArgumentParser._json_errors_default = json_errors
    parser = _ArgumentParser(prog="aep", json_errors=json_errors)
    parser.add_argument("--state-file", type=Path, default=_default_state_file())
    parser.add_argument("--output", choices=("human", "json"), default="human")
    parser.add_argument("--unsafe-debug", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    explain = sub.add_parser("explain"); explain.add_argument("id")
    for command in KIND_COMMANDS:
        group = sub.add_parser(command).add_subparsers(dest="action", required=True)
        show = group.add_parser("show"); show.add_argument("id")
        if command == "executions":
            listing = group.add_parser("list")
            listing.add_argument("--status")
    try:
        args = parser.parse_args(arguments)
        inspector = ExecutionInspector(RuntimeEvidenceReader(args.state_file).objects())
        if args.command == "explain": result = inspector.explain(args.id)
        elif args.command == "executions" and args.action == "list": result = inspector.list_executions(status=args.status)
        else: result = inspector.show(args.id, KIND_COMMANDS[args.command], unsafe=args.unsafe_debug)
    except InspectionError as error:
        print(json.dumps({"error": {"code": error.code, "message": str(error)}}, sort_keys=True), file=sys.stderr)
        return 2
    if args.output == "json": print(json.dumps(result, indent=2, sort_keys=True, default=str))
    else: print(_human(result))
    return 0


def _default_state_file() -> Path:
    return Path(os.environ.get("AEP_STATE_ROOT", ".")) / "runtime" / "objects.json"


def _human(value: Any) -> str:
    if isinstance(value, Mapping) and "workflowExecutions" in value:
        records = value["workflowExecutions"]
        return "Workflow executions:\n" + ("\n".join(
            f"- {item['id']}  {item.get('status', 'UNKNOWN')}  {item.get('createdAt', '')}" for item in records
        ) if records else "- none")
    if isinstance(value, Mapping) and "workflowExecution" in value:
        workflow = value["workflowExecution"]
        workflow_ref = workflow.get("workflowRef", {})
        elapsed = value.get("elapsed", {})
        lines = [f"Workflow execution: {workflow['id']}", f"Status: {workflow.get('status', 'UNKNOWN')}", f"Workflow: {workflow_ref.get('name', '')}:{workflow_ref.get('version', '')}", f"Repository revision: {workflow.get('repositoryRevision', '')}", f"Elapsed: {elapsed.get('milliseconds', 'in progress')} ms", "Tasks:"]
        lines.extend(f"- {item['id']}  {item.get('status', 'UNKNOWN')}  depends on {', '.join(item.get('dependencyTaskExecutionIds', ())) or 'none'}" for item in value.get("tasks", ()))
        return "\n".join(lines)
    if isinstance(value, Mapping) and "decisiveEvidence" in value:
        evidence = value["decisiveEvidence"]
        return f"Outcome: {value['outcome']}\nReason: {value['reason']}\nDecisive evidence: {evidence.get('kind')} {evidence.get('id')}"
    if isinstance(value, Mapping):
        return "\n".join(f"{key}: {item}" for key, item in value.items())
    return str(value)


if __name__ == "__main__":
    raise SystemExit(main())
