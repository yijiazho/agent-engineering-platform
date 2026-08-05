"""Idempotent WorkflowExecution creation and trace-root initialization."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from functools import cache
import json
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource as SchemaResource
from referencing.jsonschema import DRAFT202012

from aep.resource_loader import Resource, ResourceRef
from aep.runtime_validation import is_rfc3339_timestamp
from aep.runtime_store import RuntimeObject, RuntimeObjectStore


class InvalidWorkflowExecutionInputError(ValueError):
    """Raised when execution inputs do not describe one resolved trigger."""


class WorkflowExecutionCreator:
    """Create one trace root for a deduplicated Event and Workflow pair."""

    def __init__(self, store: RuntimeObjectStore) -> None:
        self._store = store

    def create(
        self,
        *,
        event: Mapping[str, Any],
        workflow: Resource,
        event_resource: Resource,
        repository_revision: str,
        knowledge_graph_version: str,
        timestamp: str,
    ) -> RuntimeObject:
        """Persist an initial WorkflowExecution and its creation event.

        Repeated or concurrent calls for the same normalized Event and explicit
        Workflow version return the first execution. The deterministic
        ExecutionEvent append also repairs a prior call interrupted after the
        execution was stored.
        """
        event_id = _required_string(event, "id")
        _required_string(event, "deduplicationKey")
        _validate_resource_pair(event, workflow, event_resource)
        revision = _required_value(repository_revision, "repository_revision")
        graph_version = _required_value(
            knowledge_graph_version, "knowledge_graph_version"
        )
        created_at = _required_value(timestamp, "timestamp")

        workflow_ref = _ref_record(workflow.ref)
        event_ref = _ref_record(event_resource.ref)
        identity = f"{event_id}:{_ref_identity(workflow.ref)}"
        execution_uuid = uuid5(NAMESPACE_URL, f"workflow-execution:{identity}")
        execution_id = f"workflowexecution-{execution_uuid}"
        trace_id = f"trace-{uuid5(NAMESPACE_URL, f'workflow-trace:{identity}')}"
        provenance = {
            "actor": "workflow-controller",
            "repositoryRevision": revision,
            "knowledgeGraphVersion": graph_version,
            "resourceRefs": [workflow_ref, event_ref],
        }
        record = {
            "apiVersion": "aep.dev/v1alpha1",
            "kind": "WorkflowExecution",
            "id": execution_id,
            "traceId": trace_id,
            "createdAt": created_at,
            "updatedAt": created_at,
            "provenance": provenance,
            "workflowRef": workflow_ref,
            "eventRef": event_ref,
            "eventId": event_id,
            "repositoryRevision": revision,
            "knowledgeGraphVersion": graph_version,
            "status": "RUNNING",
            "startedAt": created_at,
            "taskExecutionIds": [],
        }
        creation_event = _started_event(record)
        _validate_runtime_record(record, "workflowexecution.schema.json")
        _validate_runtime_record(creation_event, "executionevent.schema.json")

        execution = self._store.create(
            record,
            deterministic_key=f"workflow-execution:{identity}",
        )
        if execution != record:
            creation_event = _started_event(execution)
            _validate_runtime_record(creation_event, "executionevent.schema.json")
        self._store.append_event(creation_event)
        return execution


def _started_event(execution: RuntimeObject) -> dict[str, Any]:
    execution_id = str(execution["id"])
    timestamp = str(execution["createdAt"])
    event_uuid = uuid5(
        NAMESPACE_URL, f"execution-event:{execution_id}:WorkflowExecutionStarted"
    )
    return {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "ExecutionEvent",
        "id": f"executionevent-{event_uuid}",
        "traceId": execution["traceId"],
        "createdAt": timestamp,
        "updatedAt": timestamp,
        "provenance": {
            "actor": "workflow-controller",
            "workflowExecutionId": execution_id,
            "repositoryRevision": execution["repositoryRevision"],
            "knowledgeGraphVersion": execution["knowledgeGraphVersion"],
            "resourceRefs": deepcopy(list(execution["provenance"]["resourceRefs"])),
        },
        "eventType": "WorkflowExecutionStarted",
        "subject": {"kind": "WorkflowExecution", "id": execution_id},
        "sequence": 1,
        "emittedAt": timestamp,
        "payload": {
            "status": execution["status"],
            "eventId": execution["eventId"],
        },
    }


def _validate_resource_pair(
    event: Mapping[str, Any],
    workflow: Resource,
    event_resource: Resource,
) -> None:
    if not isinstance(workflow, Resource) or workflow.kind != "Workflow":
        raise InvalidWorkflowExecutionInputError(
            "workflow must be a loaded Workflow Resource"
        )
    if not isinstance(event_resource, Resource) or event_resource.kind != "Event":
        raise InvalidWorkflowExecutionInputError(
            "event_resource must be a loaded Event Resource"
        )
    if workflow.version == "latest" or event_resource.version == "latest":
        raise InvalidWorkflowExecutionInputError(
            "WorkflowExecution requires explicit immutable Resource versions"
        )

    workflow_spec = workflow.data.get("spec")
    triggers = (
        workflow_spec.get("triggers")
        if isinstance(workflow_spec, Mapping)
        else None
    )
    trigger_refs = {
        ResourceRef.from_mapping(dict(trigger["eventRef"]))
        for trigger in triggers or ()
        if isinstance(trigger, Mapping)
        and isinstance(trigger.get("eventRef"), Mapping)
    }
    if event_resource.ref not in trigger_refs:
        raise InvalidWorkflowExecutionInputError(
            "event_resource is not a declared trigger of workflow"
        )

    event_spec = event_resource.data.get("spec")
    if not isinstance(event_spec, Mapping) or any(
        event.get(field) != event_spec.get(field) for field in ("source", "type")
    ):
        raise InvalidWorkflowExecutionInputError(
            "normalized Event does not match event_resource source and type"
        )


def _required_string(value: Mapping[str, Any], field: str) -> str:
    candidate = value.get(field)
    if not isinstance(candidate, str) or not candidate.strip():
        raise InvalidWorkflowExecutionInputError(
            f"event.{field} must be a non-empty string"
        )
    return candidate.strip()


def _required_value(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidWorkflowExecutionInputError(
            f"{field} must be a non-empty string"
        )
    return value.strip()


def _validate_runtime_record(record: RuntimeObject, schema_name: str) -> None:
    errors = sorted(
        _runtime_validator(schema_name).iter_errors(dict(record)),
        key=lambda error: (list(error.absolute_path), error.message),
    )
    if errors:
        error = errors[0]
        path = "$" + "".join(f".{part}" for part in error.absolute_path)
        raise InvalidWorkflowExecutionInputError(
            f"invalid {record.get('kind', 'runtime object')} at {path}: {error.message}"
        )


@cache
def _runtime_validator(schema_name: str) -> Draft202012Validator:
    schema_root = Path(__file__).parents[2] / "schemas"
    schema_paths = (
        schema_root / "resources" / "v1" / "resource-definitions.schema.json",
        schema_root / "runtime" / "v1" / "runtime-definitions.schema.json",
        schema_root / "runtime" / "v1" / schema_name,
    )
    schemas = [
        json.loads(schema_path.read_text(encoding="utf-8"))
        for schema_path in schema_paths
    ]
    registry = Registry().with_resources(
        (
            schema["$id"],
            SchemaResource.from_contents(
                schema, default_specification=DRAFT202012
            ),
        )
        for schema in schemas
    )
    format_checker = FormatChecker()
    # ``date-time`` is optional in jsonschema and is absent when its optional
    # RFC3339 dependency is not installed. Register it locally so validation
    # never silently treats the authoritative timestamp format as an annotation.
    format_checker.checkers = dict(format_checker.checkers)
    format_checker.checkers["date-time"] = (is_rfc3339_timestamp, ())
    return Draft202012Validator(
        schemas[-1],
        registry=registry,
        format_checker=format_checker,
    )


def _ref_identity(ref: ResourceRef) -> str:
    return f"{ref.kind}/{ref.name}:{ref.version}"


def _ref_record(ref: ResourceRef) -> dict[str, str]:
    return {"kind": ref.kind, "name": ref.name, "version": ref.version}
