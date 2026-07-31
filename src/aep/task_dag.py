"""Validate a Workflow Task graph and build a deterministic execution plan."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from aep.resource_loader import Resource, ResourceCollection, ResourceRef, format_ref


class TaskDagResolutionError(ValueError):
    """Base class for machine-readable Task DAG resolution failures."""

    code = "task_dag_resolution_error"

    def __init__(self, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = deepcopy(dict(details or {}))

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": str(self),
            "details": deepcopy(self.details),
        }


class InvalidWorkflowGraphError(TaskDagResolutionError):
    """Raised when the Workflow does not contain a usable Task graph."""

    code = "invalid_workflow_graph"


class MissingTaskResourceError(TaskDagResolutionError):
    """Raised when a Workflow node does not resolve to a loaded Task."""

    code = "missing_task_resource"


class MissingDependencyNodeError(TaskDagResolutionError):
    """Raised when a dependency is not declared as a node in the Workflow."""

    code = "missing_dependency_node"


class DuplicateTaskIdentityError(TaskDagResolutionError):
    """Raised when a Workflow declares the same versioned Task more than once."""

    code = "duplicate_task_identity"


class CyclicTaskDependencyError(TaskDagResolutionError):
    """Raised when Task dependencies form a cycle."""

    code = "cyclic_task_dependency"


@dataclass(frozen=True)
class TaskPlanNode:
    """One resolved Task and its graph relationships."""

    task_ref: ResourceRef
    task: Resource
    dependencies: tuple[ResourceRef, ...]
    dependents: tuple[ResourceRef, ...]


@dataclass(frozen=True)
class TaskDagPlan:
    """Immutable, deterministic plan produced from a Workflow Task DAG."""

    workflow_ref: ResourceRef
    nodes: tuple[TaskPlanNode, ...]
    ready_groups: tuple[tuple[ResourceRef, ...], ...]

    @property
    def topological_order(self) -> tuple[ResourceRef, ...]:
        return tuple(node.task_ref for node in self.nodes)

    def get_node(self, task_ref: ResourceRef) -> TaskPlanNode | None:
        return next((node for node in self.nodes if node.task_ref == task_ref), None)


def resolve_task_dag(workflow: Resource, resources: ResourceCollection) -> TaskDagPlan:
    """Resolve and validate the versioned Task graph declared by ``workflow``.

    Nodes within the same ready group have no dependencies on one another and
    may be scheduled in parallel. Declaration order is used as the stable
    tie-breaker both within ready groups and in the flattened topological order.
    """
    if not isinstance(workflow, Resource) or workflow.kind != "Workflow":
        raise TypeError("workflow must be a Workflow Resource")
    if not isinstance(resources, ResourceCollection):
        raise TypeError("resources must be a ResourceCollection")

    entries = _task_entries(workflow)
    declaration_order: dict[ResourceRef, int] = {}
    dependencies: dict[ResourceRef, tuple[ResourceRef, ...]] = {}
    resolved_tasks: dict[ResourceRef, Resource] = {}

    for index, entry in enumerate(entries):
        task_ref = _parse_task_ref(
            workflow,
            entry.get("taskRef"),
            location=f"spec.tasks[{index}].taskRef",
        )
        if task_ref in declaration_order:
            raise DuplicateTaskIdentityError(
                f"{format_ref(workflow.ref)} declares duplicate Task node {format_ref(task_ref)}",
                details={
                    "workflowRef": _ref_record(workflow.ref),
                    "taskRef": _ref_record(task_ref),
                    "firstIndex": declaration_order[task_ref],
                    "duplicateIndex": index,
                },
            )
        task = resources.get(task_ref)
        if task is None or task.kind != "Task":
            raise MissingTaskResourceError(
                f"{format_ref(workflow.ref)} cannot resolve Task node {format_ref(task_ref)}",
                details={
                    "workflowRef": _ref_record(workflow.ref),
                    "taskRef": _ref_record(task_ref),
                    "index": index,
                },
            )

        dependency_values = entry.get("dependsOn", [])
        if not isinstance(dependency_values, list):
            raise _invalid_graph(
                workflow, f"spec.tasks[{index}].dependsOn must be an array"
            )
        parsed_dependencies = tuple(
            _parse_task_ref(
                workflow,
                value,
                location=f"spec.tasks[{index}].dependsOn[{dependency_index}]",
            )
            for dependency_index, value in enumerate(dependency_values)
        )
        if len(set(parsed_dependencies)) != len(parsed_dependencies):
            raise _invalid_graph(
                workflow,
                f"spec.tasks[{index}].dependsOn contains a duplicate Task reference",
            )

        declaration_order[task_ref] = index
        dependencies[task_ref] = parsed_dependencies
        resolved_tasks[task_ref] = task

    for task_ref, dependency_refs in dependencies.items():
        for dependency_ref in dependency_refs:
            if dependency_ref not in declaration_order:
                raise MissingDependencyNodeError(
                    f"{format_ref(task_ref)} depends on Task node "
                    f"{format_ref(dependency_ref)} that is not declared in "
                    f"{format_ref(workflow.ref)}",
                    details={
                        "workflowRef": _ref_record(workflow.ref),
                        "taskRef": _ref_record(task_ref),
                        "dependencyRef": _ref_record(dependency_ref),
                    },
                )

    dependents: dict[ResourceRef, list[ResourceRef]] = {
        task_ref: [] for task_ref in declaration_order
    }
    indegree = {task_ref: len(dependencies[task_ref]) for task_ref in declaration_order}
    for task_ref, dependency_refs in dependencies.items():
        for dependency_ref in dependency_refs:
            dependents[dependency_ref].append(task_ref)

    ready = sorted(
        (task_ref for task_ref, count in indegree.items() if count == 0),
        key=declaration_order.__getitem__,
    )
    ready_groups: list[tuple[ResourceRef, ...]] = []
    topological_order: list[ResourceRef] = []
    while ready:
        group = tuple(ready)
        ready_groups.append(group)
        topological_order.extend(group)
        next_ready: list[ResourceRef] = []
        for completed_ref in group:
            for dependent_ref in dependents[completed_ref]:
                indegree[dependent_ref] -= 1
                if indegree[dependent_ref] == 0:
                    next_ready.append(dependent_ref)
        ready = sorted(next_ready, key=declaration_order.__getitem__)

    if len(topological_order) != len(declaration_order):
        cyclic_refs = tuple(
            sorted(
                (task_ref for task_ref, count in indegree.items() if count > 0),
                key=declaration_order.__getitem__,
            )
        )
        raise CyclicTaskDependencyError(
            f"{format_ref(workflow.ref)} contains a cyclic Task dependency",
            details={
                "workflowRef": _ref_record(workflow.ref),
                "taskRefs": [_ref_record(ref) for ref in cyclic_refs],
            },
        )

    nodes = tuple(
        TaskPlanNode(
            task_ref=task_ref,
            task=resolved_tasks[task_ref],
            dependencies=dependencies[task_ref],
            dependents=tuple(
                sorted(dependents[task_ref], key=declaration_order.__getitem__)
            ),
        )
        for task_ref in topological_order
    )
    return TaskDagPlan(
        workflow_ref=workflow.ref,
        nodes=nodes,
        ready_groups=tuple(ready_groups),
    )


def _task_entries(workflow: Resource) -> list[Mapping[str, Any]]:
    spec = workflow.data.get("spec")
    task_values = spec.get("tasks") if isinstance(spec, Mapping) else None
    if not isinstance(task_values, list) or not task_values:
        raise _invalid_graph(workflow, "spec.tasks must be a non-empty array")
    if not all(isinstance(entry, Mapping) for entry in task_values):
        raise _invalid_graph(workflow, "every spec.tasks entry must be an object")
    return task_values


def _parse_task_ref(
    workflow: Resource, value: Any, *, location: str
) -> ResourceRef:
    try:
        if not isinstance(value, dict):
            raise TypeError
        task_ref = ResourceRef.from_mapping(value)
    except (KeyError, TypeError, ValueError):
        raise _invalid_graph(
            workflow, f"{location} must be an explicit Task Resource reference"
        ) from None
    if task_ref.kind != "Task" or task_ref.version == "latest":
        raise _invalid_graph(
            workflow, f"{location} must reference an explicit Task version"
        )
    return task_ref


def _invalid_graph(workflow: Resource, message: str) -> InvalidWorkflowGraphError:
    return InvalidWorkflowGraphError(
        f"{format_ref(workflow.ref)}: {message}",
        details={"workflowRef": _ref_record(workflow.ref)},
    )


def _ref_record(ref: ResourceRef) -> dict[str, str]:
    return {"kind": ref.kind, "name": ref.name, "version": ref.version}
