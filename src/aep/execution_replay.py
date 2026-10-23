"""Create provenance-bound WorkflowExecution replays without touching source evidence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import datetime, timezone
import re
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from aep.runtime_store import RuntimeObject, RuntimeObjectStore


class ReplayError(ValueError):
    """Replay could not be constructed from complete historical evidence."""


_RESOURCE_KINDS = frozenset({
    "Workspace", "Workflow", "Task", "Agent", "Prompt", "Model", "Tool",
    "KnowledgeBase", "Policy", "Evaluation", "Event",
})
_OVERRIDE_KINDS = frozenset({"Agent", "Prompt", "Model", "Evaluation", "Policy"})
_SEMVER = re.compile(
    r"^v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
_RESOURCE_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")


class ExecutionReplayService:
    def __init__(self, store: RuntimeObjectStore) -> None:
        self._store = store

    def replay(
        self,
        source_id: str,
        *,
        overrides: Sequence[str] = (),
        context_mode: str = "reuse",
        publication: bool = False,
        timestamp: str | None = None,
    ) -> RuntimeObject:
        source = self._store.get(source_id)
        if source is None or source.get("kind") != "WorkflowExecution":
            raise ReplayError("source WorkflowExecution was not found")
        if source.get("status") not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            raise ReplayError("only terminal WorkflowExecutions can be replayed")
        if context_mode not in {"reuse", "rebuild"}:
            raise ReplayError("context mode must be reuse or rebuild")

        revision = source.get("repositoryRevision")
        if not isinstance(revision, str) or not revision:
            raise ReplayError("exact replay is missing its historical repository revision")
        source_provenance = source.get("provenance")
        if not isinstance(source_provenance, Mapping):
            raise ReplayError("source WorkflowExecution provenance is malformed")

        related = self._store.list_by_workflow_execution(source_id)
        if context_mode == "reuse":
            _validate_context_evidence(source, related)
        resource_refs = _collect_resource_refs(source, related)
        if not resource_refs:
            raise ReplayError("exact replay is missing historical Resource provenance")
        parsed_overrides = [_parse_override(value) for value in overrides]
        resource_refs = _apply_overrides(resource_refs, parsed_overrides)

        now = timestamp or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        replay_id = f"workflowexecution-replay-{uuid4()}"
        trace_id = f"trace-replay-{uuid4()}"
        provenance = deepcopy(dict(source_provenance))
        provenance.update({
            "actor": "execution-replay",
            "repositoryRevision": revision,
            "resourceRefs": resource_refs,
        })
        record = {
            key: deepcopy(value)
            for key, value in source.items()
            if key not in {
                "id", "traceId", "createdAt", "updatedAt", "startedAt",
                "completedAt", "status", "taskExecutionIds", "failure",
                "resolvedTaskPlan", "sourceExecutionId", "replayProvenance",
            }
        }
        record.update({
            "id": replay_id,
            "traceId": trace_id,
            "createdAt": now,
            "updatedAt": now,
            "startedAt": now,
            "status": "RUNNING",
            "taskExecutionIds": [],
            "provenance": provenance,
            "sourceExecutionId": source_id,
            "resolvedTaskPlan": deepcopy(source.get("resolvedTaskPlan", [])),
            "replayProvenance": {
                "mode": "exact",
                "sourceExecutionId": source_id,
                "repositoryRevision": revision,
                "contextPackages": context_mode,
                "inheritedResourceRefs": _collect_resource_refs(source, related),
                "resourceRefs": resource_refs,
                "overrides": parsed_overrides,
                "publicationMode": "enabled" if publication else "disabled",
                "newRuntimeEvidence": [],
            },
        })

        created = self._store.create(record, deterministic_key=f"execution-replay:{replay_id}")
        self._store.append_event(_replay_started_event(created, source_id))
        return self._store.get(replay_id) or created


def _validate_context_evidence(
    source: Mapping[str, Any], related: Sequence[RuntimeObject]
) -> None:
    task_ids = source.get("taskExecutionIds", [])
    if not isinstance(task_ids, list) or not all(isinstance(value, str) and value for value in task_ids):
        raise ReplayError("source WorkflowExecution task references are malformed")
    if len(task_ids) != len(set(task_ids)):
        raise ReplayError("source WorkflowExecution contains duplicate TaskExecution references")
    by_id = {str(item.get("id")): item for item in related if isinstance(item.get("id"), str)}
    for task_id in task_ids:
        task = by_id.get(task_id)
        if task is None or task.get("kind") != "TaskExecution" or task.get("workflowExecutionId") != source.get("id"):
            raise ReplayError("source WorkflowExecution references a missing or foreign TaskExecution")
        context_id = task.get("contextPackageId")
        context_required = bool(context_id or task.get("resolvedAgentId") or task.get("agentInvocationIds"))
        if not context_required:
            continue
        if not isinstance(context_id, str) or not context_id:
            raise ReplayError("historical ContextPackage is required but not recorded on a TaskExecution")
        context = by_id.get(context_id)
        if (
            context is None
            or context.get("kind") != "ContextPackage"
            or context.get("workflowExecutionId") != source.get("id")
            or context.get("taskExecutionId") != task_id
        ):
            raise ReplayError("historical ContextPackage is missing or has mismatched ownership")


def _collect_resource_refs(
    source: Mapping[str, Any], related: Sequence[RuntimeObject]
) -> list[dict[str, str]]:
    candidates: list[Any] = [source.get("workflowRef"), source.get("eventRef")]
    root_provenance = source.get("provenance")
    if isinstance(root_provenance, Mapping):
        root_refs = root_provenance.get("resourceRefs", ())
        if not isinstance(root_refs, (list, tuple)):
            raise ReplayError("historical root Resource references are malformed")
        candidates.extend(root_refs)
    for item in related:
        provenance = item.get("provenance")
        if isinstance(provenance, Mapping):
            item_refs = provenance.get("resourceRefs", ())
            if not isinstance(item_refs, (list, tuple)):
                raise ReplayError("historical runtime Resource references are malformed")
            candidates.extend(item_refs)
        for field in ("taskRef", "agentRef", "promptRef", "modelRef", "toolRef", "evaluationRef", "policyRef"):
            if item.get(field) is not None:
                candidates.append(item[field])
        for field in ("toolRefs", "policyRefs", "evaluationRefs"):
            refs = item.get(field)
            if isinstance(refs, list):
                candidates.extend(refs)
    unique: dict[tuple[str, str, str], dict[str, str]] = {}
    for candidate in candidates:
        if candidate is None:
            continue
        if not _versioned_ref(candidate):
            raise ReplayError("historical execution contains a malformed or floating Resource reference")
        key = (candidate["kind"], candidate["name"], candidate["version"])
        unique[key] = {"kind": key[0], "name": key[1], "version": key[2]}
    return [unique[key] for key in sorted(unique)]


def _apply_overrides(
    refs: list[dict[str, str]], overrides: Sequence[dict[str, str]]
) -> list[dict[str, str]]:
    result = deepcopy(refs)
    seen: set[tuple[str, str]] = set()
    for override in overrides:
        identity = (override["kind"], override["name"])
        if identity in seen:
            raise ReplayError("only one override per Resource kind and name is allowed")
        seen.add(identity)
        matches = [index for index, ref in enumerate(result) if (ref["kind"], ref["name"]) == identity]
        if not matches:
            raise ReplayError(f"override does not match a historical {override['kind']} resource")
        result = [
            ref for ref in result
            if (ref["kind"], ref["name"]) != identity
        ]
        result.append(dict(override))
    return sorted(result, key=lambda ref: (ref["kind"], ref["name"], ref["version"]))


def _versioned_ref(value: object) -> bool:
    return (
        isinstance(value, Mapping)
        and value.get("kind") in _RESOURCE_KINDS
        and isinstance(value.get("name"), str)
        and bool(_RESOURCE_NAME.fullmatch(value["name"]))
        and isinstance(value.get("version"), str)
        and bool(_SEMVER.fullmatch(value["version"]))
    )


def _parse_override(value: str) -> dict[str, str]:
    parts = value.split(":")
    if len(parts) != 3:
        raise ReplayError("override must use Kind:name:version with an explicit immutable version")
    kind, name, version = parts
    result = {"kind": kind, "name": name, "version": version}
    if kind not in _OVERRIDE_KINDS or not _versioned_ref(result):
        raise ReplayError("override must use a supported Resource kind, valid name, and semantic version")
    return result


def _replay_started_event(execution: Mapping[str, Any], source_id: str) -> dict[str, Any]:
    execution_id = str(execution["id"])
    event_id = uuid5(NAMESPACE_URL, f"execution-event:{execution_id}:WorkflowExecutionStarted")
    return {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "ExecutionEvent",
        "id": f"executionevent-{event_id}",
        "traceId": execution["traceId"],
        "createdAt": execution["createdAt"],
        "updatedAt": execution["createdAt"],
        "provenance": {
            "actor": "execution-replay",
            "workflowExecutionId": execution_id,
            "repositoryRevision": execution["repositoryRevision"],
            "knowledgeGraphVersion": execution.get("knowledgeGraphVersion", "unknown"),
            "resourceRefs": deepcopy(execution["provenance"]["resourceRefs"]),
        },
        "eventType": "WorkflowExecutionStarted",
        "subject": {"kind": "WorkflowExecution", "id": execution_id},
        "sequence": 1,
        "emittedAt": execution["createdAt"],
        "payload": {
            "status": execution["status"],
            "sourceExecutionId": source_id,
            "replayMode": "exact",
        },
    }
