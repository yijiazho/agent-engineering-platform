"""Create provenance-bound WorkflowExecution replays without touching source evidence."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

from aep.runtime_store import RuntimeObject, RuntimeObjectStore


class ReplayError(ValueError):
    """Replay could not be constructed from complete historical evidence."""


class ExecutionReplayService:
    def __init__(self, store: RuntimeObjectStore) -> None:
        self._store = store

    def replay(self, source_id: str, *, overrides: Sequence[str] = (), context_mode: str = "reuse",
               publication: bool = False, timestamp: str | None = None) -> RuntimeObject:
        source = self._store.get(source_id)
        if source is None or source.get("kind") != "WorkflowExecution":
            raise ReplayError("source WorkflowExecution was not found")
        if context_mode not in {"reuse", "rebuild"}:
            raise ReplayError("context mode must be reuse or rebuild")
        revision = source.get("repositoryRevision")
        resources = source.get("provenance", {}).get("resourceRefs") if isinstance(source.get("provenance"), Mapping) else None
        if not isinstance(revision, str) or not revision or not isinstance(resources, list) or not resources:
            raise ReplayError("exact replay is missing historical repository or Resource provenance")
        if not all(_versioned_ref(item) for item in resources):
            raise ReplayError("exact replay requires immutable historical Resource versions")
        parsed_overrides = [_parse_override(value) for value in overrides]
        permitted = {"Agent", "Prompt", "Model", "Evaluation", "Policy"}
        if any(item["kind"] not in permitted for item in parsed_overrides):
            raise ReplayError("unsupported replay override kind")
        if context_mode == "reuse":
            related = self._store.list_by_workflow_execution(source_id)
            source_contexts = [item for item in related if item.get("kind") == "ContextPackage"]
            expected_ids = {item.get("contextPackageId") for item in related if item.get("kind") == "TaskExecution" and item.get("contextPackageId")}
            if expected_ids - {item.get("id") for item in source_contexts}:
                raise ReplayError("exact replay is missing a historical ContextPackage")
        now = timestamp or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        new_id = f"workflowexecution-replay-{uuid4()}"
        trace_id = f"trace-replay-{uuid4()}"
        provenance = deepcopy(source.get("provenance", {}))
        provenance.update({"actor": "execution-replay", "repositoryRevision": revision,
                           "resourceRefs": deepcopy(resources), "sourceExecutionId": source_id})
        record = {key: deepcopy(value) for key, value in source.items() if key not in {
            "id", "traceId", "createdAt", "updatedAt", "startedAt", "completedAt", "status",
            "taskExecutionIds", "failure", "resolvedTaskPlan"}}
        record.update({"id": new_id, "traceId": trace_id, "createdAt": now, "updatedAt": now,
                       "startedAt": now, "status": "RUNNING", "taskExecutionIds": [],
                       "provenance": provenance, "sourceExecutionId": source_id,
                       "resolvedTaskPlan": deepcopy(source.get("resolvedTaskPlan", [])),
                       "replayProvenance": {"mode": "exact", "sourceExecutionId": source_id,
                           "repositoryRevision": revision, "contextPackages": context_mode,
                           "inheritedResourceRefs": deepcopy(resources),
                           "overrides": parsed_overrides, "publicationMode": "enabled" if publication else "disabled",
                           "newRuntimeEvidence": []}})
        if parsed_overrides:
            # Replace matching refs consistently in root provenance and expose every substitution.
            refs = deepcopy(resources)
            for override in parsed_overrides:
                match = next((index for index, ref in enumerate(refs) if ref.get("kind") == override["kind"] and ref.get("name") == override["name"]), None)
                if match is None:
                    raise ReplayError(f"override does not match a historical {override['kind']} resource")
                refs[match] = {"kind": override["kind"], "name": override["name"], "version": override["version"]}
            record["provenance"]["resourceRefs"] = refs
            record["replayProvenance"]["resourceRefs"] = refs
        self._store.create(record, deterministic_key=f"execution-replay:{new_id}")
        return self._store.get(new_id) or record


def _versioned_ref(value: object) -> bool:
    return isinstance(value, Mapping) and all(isinstance(value.get(key), str) and value[key] and value[key] != "latest" for key in ("kind", "name", "version"))


def _parse_override(value: str) -> dict[str, str]:
    parts = value.split(":")
    if len(parts) != 3 or not all(parts) or parts[2] == "latest":
        raise ReplayError("override must use Kind/name:version with an explicit immutable version")
    kind, name, version = parts
    return {"kind": kind, "name": name, "version": version}
