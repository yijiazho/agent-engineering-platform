"""Resolve normalized events to explicitly versioned Workflow Resources."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from aep.resource_loader import (
    Resource,
    ResourceCollection,
    ResourceRef,
    format_ref,
)


class WorkflowResolutionError(ValueError):
    """Base class for machine-readable workflow resolution failures."""

    code = "workflow_resolution_error"

    def __init__(self, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = deepcopy(dict(details or {}))

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": str(self),
            "details": deepcopy(self.details),
        }


class InvalidNormalizedEventError(WorkflowResolutionError):
    """Raised when a normalized event lacks a usable source or type."""

    code = "invalid_normalized_event"


class InvalidTriggerConfigurationError(WorkflowResolutionError):
    """Raised when a loaded Workflow trigger cannot be resolved safely."""

    code = "invalid_trigger_configuration"


class AmbiguousWorkflowMatchError(WorkflowResolutionError):
    """Raised when multiple Workflows match and fan-out is not allowed."""

    code = "ambiguous_workflow_match"


@dataclass(frozen=True)
class WorkflowResolution:
    """Deterministic Workflow references selected for one normalized event."""

    workflow_refs: tuple[ResourceRef, ...]

    @property
    def matched(self) -> bool:
        return bool(self.workflow_refs)

    @property
    def workflow_ref(self) -> ResourceRef | None:
        """Return the sole match, or ``None`` for a no-match result."""
        if len(self.workflow_refs) > 1:
            raise ValueError("resolution contains multiple Workflow references")
        return self.workflow_refs[0] if self.workflow_refs else None


def resolve_workflow_for_event(
    event: Mapping[str, Any],
    resources: ResourceCollection,
    *,
    allow_fan_out: bool = False,
) -> WorkflowResolution:
    """Match an event to Workflow triggers in a loaded Resource collection.

    Trigger references are resolved to Event Resources before their declared
    ``source`` and ``type`` are compared. Multiple matching Workflows are a
    configuration error unless the caller's policy explicitly allows fan-out.
    """
    source, event_type = _event_identity(event)
    if not isinstance(resources, ResourceCollection):
        raise TypeError("resources must be a ResourceCollection")
    if not isinstance(allow_fan_out, bool):
        raise TypeError("allow_fan_out must be a bool")

    matches: list[ResourceRef] = []
    for workflow in resources.by_kind("Workflow"):
        if _workflow_matches(workflow, resources, source=source, event_type=event_type):
            matches.append(workflow.ref)

    if len(matches) > 1 and not allow_fan_out:
        formatted_matches = [format_ref(ref) for ref in matches]
        raise AmbiguousWorkflowMatchError(
            f"Event {source}/{event_type} matches multiple Workflows: "
            + ", ".join(formatted_matches),
            details={
                "source": source,
                "type": event_type,
                "workflowRefs": [_ref_record(ref) for ref in matches],
            },
        )

    return WorkflowResolution(tuple(matches))


def _event_identity(event: Mapping[str, Any]) -> tuple[str, str]:
    if not isinstance(event, Mapping):
        raise InvalidNormalizedEventError(
            "Normalized event must be an object",
            details={"field": "event", "reason": "invalid_type"},
        )

    for field in ("source", "type"):
        value = event.get(field)
        if not isinstance(value, str) or not value.strip():
            raise InvalidNormalizedEventError(
                f"Normalized event field {field!r} must be a non-empty string",
                details={"field": field, "reason": "required"},
            )
    return event["source"], event["type"]


def _workflow_matches(
    workflow: Resource,
    resources: ResourceCollection,
    *,
    source: str,
    event_type: str,
) -> bool:
    spec = workflow.data.get("spec")
    triggers = spec.get("triggers") if isinstance(spec, Mapping) else None
    if not isinstance(triggers, list) or not triggers:
        raise _invalid_trigger(workflow, "spec.triggers must be a non-empty array")

    matched = False
    seen_refs: set[ResourceRef] = set()
    for index, trigger in enumerate(triggers):
        if not isinstance(trigger, Mapping):
            raise _invalid_trigger(workflow, f"spec.triggers[{index}] must be an object")
        event_ref_value = trigger.get("eventRef")
        try:
            if not isinstance(event_ref_value, dict):
                raise TypeError
            event_ref = ResourceRef.from_mapping(event_ref_value)
        except (KeyError, TypeError, ValueError):
            raise _invalid_trigger(
                workflow,
                f"spec.triggers[{index}].eventRef must be an explicit Resource reference",
            ) from None

        if event_ref.kind != "Event" or event_ref.version == "latest":
            raise _invalid_trigger(
                workflow,
                f"spec.triggers[{index}].eventRef must reference an explicit Event version",
                event_ref=event_ref,
            )
        if event_ref in seen_refs:
            raise _invalid_trigger(
                workflow,
                f"spec.triggers[{index}].eventRef duplicates {format_ref(event_ref)}",
                event_ref=event_ref,
            )
        seen_refs.add(event_ref)

        event_resource = resources.get(event_ref)
        if event_resource is None or event_resource.kind != "Event":
            raise _invalid_trigger(
                workflow,
                f"spec.triggers[{index}].eventRef cannot resolve {format_ref(event_ref)}",
                event_ref=event_ref,
            )

        event_spec = event_resource.data.get("spec")
        declared_source = event_spec.get("source") if isinstance(event_spec, Mapping) else None
        declared_type = event_spec.get("type") if isinstance(event_spec, Mapping) else None
        if not isinstance(declared_source, str) or not declared_source:
            raise _invalid_trigger(
                workflow,
                f"{format_ref(event_ref)} must declare a non-empty spec.source",
                event_ref=event_ref,
            )
        if not isinstance(declared_type, str) or not declared_type:
            raise _invalid_trigger(
                workflow,
                f"{format_ref(event_ref)} must declare a non-empty spec.type",
                event_ref=event_ref,
            )
        matched = matched or (declared_source == source and declared_type == event_type)

    return matched


def _invalid_trigger(
    workflow: Resource,
    message: str,
    *,
    event_ref: ResourceRef | None = None,
) -> InvalidTriggerConfigurationError:
    details: dict[str, object] = {"workflowRef": _ref_record(workflow.ref)}
    if event_ref is not None:
        details["eventRef"] = _ref_record(event_ref)
    return InvalidTriggerConfigurationError(
        f"{format_ref(workflow.ref)}: {message}",
        details=details,
    )


def _ref_record(ref: ResourceRef) -> dict[str, str]:
    return {"kind": ref.kind, "name": ref.name, "version": ref.version}
