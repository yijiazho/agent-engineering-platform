from copy import deepcopy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource as SchemaResource
from referencing.jsonschema import DRAFT202012

from aep.agent_resolver import (
    AgentToolDeniedError,
    InvalidAgentReferenceError,
    MissingAgentResourceError,
    resolve_agent,
)
from aep.resource_loader import Resource, ResourceCollection, ResourceRef


ROOT = Path(__file__).parents[1]
TASK_REF = ResourceRef("Task", "analyze-issue", "1.0.0")
AGENT_REF = ResourceRef("Agent", "issue-analyzer", "1.0.0")


def test_resolves_exact_resources_to_immutable_schema_valid_runtime_object() -> None:
    resources = collection()

    resolved = resolve_agent(
        TASK_REF,
        AGENT_REF,
        resources,
        task_execution_id="taskexecution-123456789abc",
        trace_id="trace-agent-resolver-123",
        resolved_at="2026-08-04T12:00:00Z",
    )

    assert resolved["agentRef"] == {
        "kind": "Agent",
        "name": "issue-analyzer",
        "version": "1.0.0",
    }
    assert resolved["promptRef"]["name"] == "issue-analysis"
    assert resolved["modelRef"]["name"] == "default-reasoning"
    assert resolved["toolRefs"][0]["name"] == "github-read-issue"
    assert [ref["name"] for ref in resolved["policyRefs"]] == [
        "task-policy",
        "agent-policy",
    ]
    assert resolved["modelParameters"] == {"temperature": 0.2}
    assert resolved["outputSchema"] == {
        "type": "object",
        "required": ("summary",),
    }
    assert resolved["provenance"]["resourceRefs"][0] == {
        "kind": "Task",
        "name": "analyze-issue",
        "version": "1.0.0",
    }
    assert list(runtime_validator().iter_errors(resolved.as_dict())) == []

    with pytest.raises(TypeError):
        resolved["modelParameters"]["temperature"] = 1.0
    detached = resolved.as_dict()
    detached["modelParameters"]["temperature"] = 1.0
    assert resolved["modelParameters"]["temperature"] == 0.2


@pytest.mark.parametrize(
    "missing_ref, expected_field",
    [
        (ResourceRef("Prompt", "issue-analysis", "1.0.0"), "Agent.spec.promptRef"),
        (ResourceRef("Model", "default-reasoning", "1.0.0"), "Agent.spec.modelRef"),
    ],
)
def test_rejects_missing_prompt_or_model(
    missing_ref: ResourceRef, expected_field: str
) -> None:
    resources = collection(exclude={missing_ref})

    with pytest.raises(MissingAgentResourceError) as raised:
        resolve(resources)

    assert raised.value.as_dict()["details"]["field"] == expected_field
    assert raised.value.as_dict()["details"]["resourceRef"] == ref_record(missing_ref)


@pytest.mark.parametrize(
    "task_ref, agent_ref, field",
    [
        (ResourceRef("Task", "analyze-issue", "latest"), AGENT_REF, "task_ref"),
        (TASK_REF, ResourceRef("Agent", "issue-analyzer", "latest"), "agent_ref"),
        (ResourceRef("Agent", "analyze-issue", "1.0.0"), AGENT_REF, "task_ref"),
        (TASK_REF, ResourceRef("Model", "issue-analyzer", "1.0.0"), "agent_ref"),
    ],
)
def test_rejects_floating_or_wrong_kind_input_references(
    task_ref: ResourceRef, agent_ref: ResourceRef, field: str
) -> None:
    with pytest.raises(InvalidAgentReferenceError) as raised:
        resolve_agent(
            task_ref,
            agent_ref,
            collection(),
            task_execution_id="taskexecution-123456789abc",
            trace_id="trace-agent-resolver-123",
            resolved_at="2026-08-04T12:00:00Z",
        )

    assert raised.value.as_dict()["details"]["field"] == field


def test_rejects_floating_nested_reference() -> None:
    resources = collection(
        mutate=lambda values: values[AGENT_REF]["spec"]["promptRef"].update(
            version="latest"
        )
    )

    with pytest.raises(InvalidAgentReferenceError) as raised:
        resolve(resources)

    assert raised.value.as_dict()["details"] == {
        "field": "Agent.spec.promptRef",
        "reason": "floating_version",
        "resourceRef": {
            "kind": "Prompt",
            "name": "issue-analysis",
            "version": "latest",
        },
    }


def test_rejects_model_provider_listed_as_tool() -> None:
    resources = collection(
        mutate=lambda values: values[AGENT_REF]["spec"]["toolRefs"].__setitem__(
            0, ref_record(ResourceRef("Model", "default-reasoning", "1.0.0"))
        )
    )

    with pytest.raises(InvalidAgentReferenceError) as raised:
        resolve(resources)

    assert raised.value.as_dict()["details"]["expectedKind"] == "Tool"
    assert raised.value.as_dict()["details"]["actualKind"] == "Model"


def test_rejects_tool_capability_denied_by_task_or_agent_policy() -> None:
    resources = collection(
        mutate=lambda values: values[
            ResourceRef("Policy", "agent-policy", "1.0.0")
        ]["spec"]["rules"].append(
            {
                "effect": "deny",
                "capabilities": ["github.issue.read"],
                "reason": "Agent may not retrieve issues directly.",
            }
        )
    )

    with pytest.raises(AgentToolDeniedError) as raised:
        resolve(resources)

    assert raised.value.as_dict() == {
        "code": "agent_tool_denied",
        "message": (
            "Policy/agent-policy:1.0.0 denies Tool/github-read-issue:1.0.0 "
            "capabilities: github.issue.read"
        ),
        "details": {
            "toolRef": ref_record(
                ResourceRef("Tool", "github-read-issue", "1.0.0")
            ),
            "policyRef": ref_record(
                ResourceRef("Policy", "agent-policy", "1.0.0")
            ),
            "ruleIndex": 1,
            "capabilities": ["github.issue.read"],
        },
    }


def test_conditional_denial_is_preserved_for_execution_time_policy_input() -> None:
    resources = collection(
        mutate=lambda values: values[
            ResourceRef("Policy", "agent-policy", "1.0.0")
        ]["spec"]["rules"].append(
            {
                "effect": "deny",
                "capabilities": ["github.issue.read"],
                "conditions": {"required": ["executionContext"]},
            }
        )
    )

    resolved = resolve(resources)

    assert resolved["toolRefs"][0]["name"] == "github-read-issue"
    assert resolved["policyRefs"][1]["name"] == "agent-policy"


def test_task_must_assign_the_supplied_agent() -> None:
    other_ref = ResourceRef("Agent", "other-agent", "1.0.0")
    resources = collection(
        mutate=lambda values: values[AGENT_REF]["metadata"].update(name="other-agent")
    )
    resources = ResourceCollection(
        workspace=resources.workspace,
        resources=tuple(
            Resource(other_ref, item.path, item.data, item.references)
            if item.ref == AGENT_REF
            else item
            for item in resources.resources
        ),
    )

    with pytest.raises(InvalidAgentReferenceError, match="assigns"):
        resolve_agent(
            TASK_REF,
            other_ref,
            resources,
            task_execution_id="taskexecution-123456789abc",
            trace_id="trace-agent-resolver-123",
            resolved_at="2026-08-04T12:00:00Z",
        )


@pytest.mark.parametrize(
    "overrides, expected_field",
    [
        ({"task_execution_id": "x"}, "$.provenance.taskExecutionId"),
        ({"trace_id": "x"}, "$.traceId"),
        ({"resolved_at": "not-a-time"}, "$.createdAt"),
        ({"resolved_at": "2026-02-30T12:00:00Z"}, "$.createdAt"),
    ],
)
def test_rejects_runtime_metadata_outside_resolvedagent_contract(
    overrides: dict[str, str], expected_field: str
) -> None:
    arguments = {
        "task_execution_id": "taskexecution-123456789abc",
        "trace_id": "trace-agent-resolver-123",
        "resolved_at": "2026-08-04T12:00:00Z",
        **overrides,
    }

    with pytest.raises(InvalidAgentReferenceError) as raised:
        resolve_agent(TASK_REF, AGENT_REF, collection(), **arguments)

    assert raised.value.as_dict()["details"]["reason"] == "runtime_contract"
    assert raised.value.as_dict()["details"]["field"] == expected_field


def resolve(resources: ResourceCollection):
    return resolve_agent(
        TASK_REF,
        AGENT_REF,
        resources,
        task_execution_id="taskexecution-123456789abc",
        trace_id="trace-agent-resolver-123",
        resolved_at="2026-08-04T12:00:00Z",
    )


def collection(*, exclude: set[ResourceRef] | None = None, mutate=None) -> ResourceCollection:
    refs = {
        "workspace": ResourceRef("Workspace", "main", "1.0.0"),
        "task": TASK_REF,
        "agent": AGENT_REF,
        "prompt": ResourceRef("Prompt", "issue-analysis", "1.0.0"),
        "model": ResourceRef("Model", "default-reasoning", "1.0.0"),
        "tool": ResourceRef("Tool", "github-read-issue", "1.0.0"),
        "task_policy": ResourceRef("Policy", "task-policy", "1.0.0"),
        "agent_policy": ResourceRef("Policy", "agent-policy", "1.0.0"),
    }
    values = {
        refs["workspace"]: resource_data(
            refs["workspace"],
            {
                "repository": {
                    "provider": "github",
                    "owner": "acme",
                    "name": "widgets",
                    "defaultBranch": "main",
                },
                "resourceDiscovery": {"root": ".ai"},
            },
        ),
        refs["task"]: resource_data(
            refs["task"],
            {
                "objective": "Analyze an issue.",
                "agentRef": ref_record(refs["agent"]),
                "outputs": {"type": "object", "required": ["summary"]},
                "policies": [ref_record(refs["task_policy"])],
            },
        ),
        refs["agent"]: resource_data(
            refs["agent"],
            {
                "promptRef": ref_record(refs["prompt"]),
                "modelRef": ref_record(refs["model"]),
                "toolRefs": [ref_record(refs["tool"])],
                "policyRefs": [ref_record(refs["agent_policy"])],
            },
        ),
        refs["prompt"]: resource_data(refs["prompt"], {"system": "Analyze."}),
        refs["model"]: resource_data(
            refs["model"],
            {
                "provider": "openai",
                "model": "gpt-5",
                "parameters": {"temperature": 0.2},
            },
        ),
        refs["tool"]: resource_data(
            refs["tool"],
            {
                "category": "external-service",
                "capabilities": ["github.issue.read"],
                "inputSchema": {"type": "object"},
                "outputSchema": {"type": "object"},
            },
        ),
        refs["task_policy"]: policy_data(refs["task_policy"]),
        refs["agent_policy"]: policy_data(refs["agent_policy"]),
    }
    if mutate is not None:
        mutate(values)
    excluded = exclude or set()
    resources = tuple(
        Resource(ref, Path(f"/{ref.kind.lower()}/{ref.name}.yaml"), data, ())
        for ref, data in values.items()
        if ref not in excluded
    )
    return ResourceCollection(workspace=resources[0], resources=resources)


def resource_data(ref: ResourceRef, spec: dict) -> dict:
    return {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": ref.kind,
        "metadata": {"name": ref.name, "version": ref.version},
        "spec": deepcopy(spec),
    }


def policy_data(ref: ResourceRef) -> dict:
    return resource_data(
        ref,
        {
            "type": "pre-execution-capability",
            "rules": [{"effect": "allow", "capabilities": ["github.issue.read"]}],
        },
    )


def ref_record(ref: ResourceRef) -> dict[str, str]:
    return {"kind": ref.kind, "name": ref.name, "version": ref.version}


def runtime_validator() -> Draft202012Validator:
    paths = (
        ROOT / "schemas/resources/v1/resource-definitions.schema.json",
        ROOT / "schemas/runtime/v1/runtime-definitions.schema.json",
        ROOT / "schemas/runtime/v1/resolvedagent.schema.json",
    )
    schemas = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    registry = Registry()
    for schema in schemas:
        registry = registry.with_resource(
            schema["$id"],
            SchemaResource.from_contents(schema, default_specification=DRAFT202012),
        )
    return Draft202012Validator(schemas[-1], registry=registry)
