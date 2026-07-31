from pathlib import Path

import pytest

from aep.resource_loader import Resource, ResourceCollection, ResourceRef
from aep.task_dag import (
    CyclicTaskDependencyError,
    DuplicateTaskIdentityError,
    MissingDependencyNodeError,
    MissingTaskResourceError,
    resolve_task_dag,
)


def test_resolves_linear_dag_with_dependency_metadata() -> None:
    resources, workflow = graph(
        [
            node("analyze"),
            node("plan", depends_on=("analyze",)),
            node("implement", depends_on=("plan",)),
        ]
    )

    plan = resolve_task_dag(workflow, resources)

    assert names(plan.topological_order) == ("analyze", "plan", "implement")
    assert tuple(names(group) for group in plan.ready_groups) == (
        ("analyze",),
        ("plan",),
        ("implement",),
    )
    assert names(plan.get_node(task_ref("plan")).dependencies) == ("analyze",)
    assert names(plan.get_node(task_ref("plan")).dependents) == ("implement",)


def test_resolves_branched_dag_into_stable_parallel_ready_groups() -> None:
    resources, workflow = graph(
        [
            node("root"),
            node("lint", depends_on=("root",)),
            node("test", depends_on=("root",)),
            node("publish", depends_on=("test", "lint")),
        ]
    )

    first = resolve_task_dag(workflow, resources)
    second = resolve_task_dag(workflow, resources)

    assert first == second
    assert names(first.topological_order) == ("root", "lint", "test", "publish")
    assert tuple(names(group) for group in first.ready_groups) == (
        ("root",),
        ("lint", "test"),
        ("publish",),
    )
    assert names(first.get_node(task_ref("root")).dependents) == ("lint", "test")


def test_rejects_missing_task_resource_with_structured_error() -> None:
    resources, workflow = graph([node("missing")], loaded_tasks=())

    with pytest.raises(MissingTaskResourceError) as raised:
        resolve_task_dag(workflow, resources)

    assert raised.value.as_dict() == {
        "code": "missing_task_resource",
        "message": (
            "Workflow/test-workflow:1.0.0 cannot resolve Task node "
            "Task/missing:1.0.0"
        ),
        "details": {
            "workflowRef": {
                "kind": "Workflow",
                "name": "test-workflow",
                "version": "1.0.0",
            },
            "taskRef": {"kind": "Task", "name": "missing", "version": "1.0.0"},
            "index": 0,
        },
    }


def test_rejects_dependency_not_declared_as_workflow_node() -> None:
    resources, workflow = graph(
        [node("publish", depends_on=("build",))],
        loaded_tasks=("publish", "build"),
    )

    with pytest.raises(MissingDependencyNodeError) as raised:
        resolve_task_dag(workflow, resources)

    assert raised.value.details["dependencyRef"] == {
        "kind": "Task",
        "name": "build",
        "version": "1.0.0",
    }


def test_rejects_duplicate_task_identity() -> None:
    resources, workflow = graph([node("analyze"), node("analyze")])

    with pytest.raises(DuplicateTaskIdentityError) as raised:
        resolve_task_dag(workflow, resources)

    assert raised.value.details["firstIndex"] == 0
    assert raised.value.details["duplicateIndex"] == 1


def test_rejects_cyclic_dag_with_structured_error() -> None:
    resources, workflow = graph(
        [
            node("analyze", depends_on=("publish",)),
            node("build", depends_on=("analyze",)),
            node("publish", depends_on=("build",)),
        ]
    )

    with pytest.raises(CyclicTaskDependencyError) as raised:
        resolve_task_dag(workflow, resources)

    assert raised.value.as_dict()["code"] == "cyclic_task_dependency"
    assert [
        task["name"] for task in raised.value.details["taskRefs"]
    ] == ["analyze", "build", "publish"]


def graph(
    nodes: list[dict[str, object]],
    *,
    loaded_tasks: tuple[str, ...] | None = None,
) -> tuple[ResourceCollection, Resource]:
    workflow = make_resource("Workflow", "test-workflow", {"tasks": nodes})
    if loaded_tasks is None:
        loaded_tasks = tuple(
            dict(entry["taskRef"])["name"]  # type: ignore[arg-type]
            for entry in nodes
        )
    tasks = tuple(
        make_resource(
            "Task",
            name,
            {"objective": f"Run {name}.", "outputs": {"type": "object"}},
        )
        for name in dict.fromkeys(loaded_tasks)
    )
    workspace = make_resource(
        "Workspace",
        "test-workspace",
        {
            "repository": {
                "provider": "github",
                "owner": "example",
                "name": "repo",
                "defaultBranch": "main",
            },
            "resourceDiscovery": {"root": ".ai"},
        },
    )
    return (
        ResourceCollection(workspace=workspace, resources=(workspace, workflow, *tasks)),
        workflow,
    )


def node(name: str, *, depends_on: tuple[str, ...] = ()) -> dict[str, object]:
    value: dict[str, object] = {"taskRef": ref_record(name)}
    if depends_on:
        value["dependsOn"] = [ref_record(dependency) for dependency in depends_on]
    return value


def make_resource(kind: str, name: str, spec: dict[str, object]) -> Resource:
    ref = ResourceRef(kind, name, "1.0.0")
    return Resource(
        ref=ref,
        path=Path(f"{name}.yaml"),
        data={
            "apiVersion": "aep.dev/v1alpha1",
            "kind": kind,
            "metadata": {"name": name, "version": "1.0.0"},
            "spec": spec,
        },
        references=(),
    )


def task_ref(name: str) -> ResourceRef:
    return ResourceRef("Task", name, "1.0.0")


def ref_record(name: str) -> dict[str, str]:
    return {"kind": "Task", "name": name, "version": "1.0.0"}


def names(refs: tuple[ResourceRef, ...]) -> tuple[str, ...]:
    return tuple(ref.name for ref in refs)
