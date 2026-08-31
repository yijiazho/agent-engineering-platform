import json
from pathlib import Path

import pytest

from aep.resource_loader import (
    DuplicateResourceError,
    DuplicateWorkspaceError,
    MissingResourceReferenceError,
    ResourceFileNotFoundError,
    ResourceLoader,
    ResourceValidationError,
)


def test_loads_valid_resources_in_stable_order(tmp_path: Path) -> None:
    write_valid_workspace(tmp_path)
    write_resource(tmp_path, "events/github-issue-created.yaml", resource("Event", "github-issue-created", "1.0.0", {
        "source": "github",
        "type": "github.issue.created",
        "schema": {"type": "object"},
    }))
    write_resource(tmp_path, "policies/default-publication.yaml", resource("Policy", "default-publication", "1.0.0", {
        "type": "publication",
        "rules": [{"effect": "allow", "conditions": {"requiredEvaluationStatus": "passed"}}],
    }))
    write_resource(tmp_path, "evaluations/issue-analysis-schema.yaml", resource("Evaluation", "issue-analysis-schema", "1.0.0", {
        "type": "schema",
        "inputSchema": {"type": "object"},
    }))
    write_resource(tmp_path, "tools/github-read-issue.yaml", resource("Tool", "github-read-issue", "1.0.0", {
        "category": "external-service",
        "capabilities": ["github.issue.read"],
        "inputSchema": {"type": "object"},
        "outputSchema": {"type": "object"},
    }))
    write_resource(tmp_path, "knowledge/repository-docs.yaml", resource("KnowledgeBase", "repository-docs", "1.0.0", {
        "sources": [{"type": "docs", "path": "docs/"}],
        "indexing": {"strategy": "documentation"},
    }))
    write_resource(tmp_path, "models/default-reasoning.yaml", resource("Model", "default-reasoning", "1.0.0", {
        "provider": "openai",
        "model": "gpt-5",
    }))
    write_resource(tmp_path, "prompts/issue-analysis.yaml", resource("Prompt", "issue-analysis", "1.0.0", {
        "system": "Analyze the issue.",
    }))
    write_resource(tmp_path, "agents/issue-analyzer.yaml", resource("Agent", "issue-analyzer", "1.0.0", {
        "promptRef": ref("Prompt", "issue-analysis"),
        "modelRef": ref("Model", "default-reasoning"),
        "toolRefs": [ref("Tool", "github-read-issue")],
    }))
    write_resource(tmp_path, "tasks/analyze-issue.yaml", resource("Task", "analyze-issue", "1.0.0", {
        "objective": "Analyze issue.",
        "agentRef": ref("Agent", "issue-analyzer"),
        "inputContextTokenBudget": 32_000,
        "outputs": {"type": "object"},
        "evaluations": [ref("Evaluation", "issue-analysis-schema")],
    }))
    write_resource(tmp_path, "workflows/issue-to-pr.yaml", resource("Workflow", "issue-to-pr", "1.0.0", {
        "triggers": [{"eventRef": ref("Event", "github-issue-created")}],
        "tasks": [{"taskRef": ref("Task", "analyze-issue")}],
    }))

    collection = ResourceLoader(tmp_path).load()

    assert collection.workspace.name == "default-workspace"
    assert [(item.kind, item.name) for item in collection.resources] == [
        ("Workspace", "default-workspace"),
        ("Workflow", "issue-to-pr"),
        ("Task", "analyze-issue"),
        ("Agent", "issue-analyzer"),
        ("Prompt", "issue-analysis"),
        ("Model", "default-reasoning"),
        ("Tool", "github-read-issue"),
        ("KnowledgeBase", "repository-docs"),
        ("Policy", "default-publication"),
        ("Evaluation", "issue-analysis-schema"),
        ("Event", "github-issue-created"),
    ]


def test_rejects_invalid_schema(tmp_path: Path) -> None:
    write_valid_workspace(tmp_path)
    write_resource(tmp_path, "models/default-reasoning.yaml", resource("Model", "default-reasoning", "latest", {
        "provider": "openai",
        "model": "gpt-5",
    }))

    with pytest.raises(ResourceValidationError):
        ResourceLoader(tmp_path).load()


def test_rejects_nested_openai_strict_output_schema_before_readiness(tmp_path: Path) -> None:
    write_valid_workspace(tmp_path, default_policies=[])
    write_resource(tmp_path, "models/openai.yaml", resource("Model", "openai", "1.0.0", {
        "provider": "openai", "model": "gpt-5",
    }))
    write_resource(tmp_path, "prompts/planner.yaml", resource("Prompt", "planner", "1.0.0", {
        "system": "Plan.",
    }))
    write_resource(tmp_path, "agents/planner.yaml", resource("Agent", "planner", "1.0.0", {
        "promptRef": ref("Prompt", "planner"),
        "modelRef": ref("Model", "openai"),
        "outputSchema": {
            "type": "object", "additionalProperties": False,
            "required": ["classifications"],
            "properties": {"classifications": {
                "type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["criterion"],
                    "properties": {
                        "criterion": {"type": "string"},
                        "requiredInsertion": {"type": "null"},
                    },
                },
            }},
        },
    }))

    with pytest.raises(
        ResourceValidationError,
        match=r"\$\.properties\.classifications\.items\.required.*requiredInsertion",
    ):
        ResourceLoader(tmp_path).load()


def test_rejects_missing_workspace_file(tmp_path: Path) -> None:
    (tmp_path / ".ai").mkdir()

    with pytest.raises(ResourceFileNotFoundError):
        ResourceLoader(tmp_path).load()


def test_rejects_missing_references(tmp_path: Path) -> None:
    write_valid_workspace(tmp_path, default_policies=[])
    write_resource(tmp_path, "tasks/analyze-issue.yaml", resource("Task", "analyze-issue", "1.0.0", {
        "objective": "Analyze issue.",
        "agentRef": ref("Agent", "missing-agent"),
        "inputContextTokenBudget": 32_000,
        "outputs": {"type": "object"},
    }))

    with pytest.raises(MissingResourceReferenceError):
        ResourceLoader(tmp_path).load()


def test_rejects_cognitive_task_without_input_context_budget(tmp_path: Path) -> None:
    write_valid_workspace(tmp_path, default_policies=[])
    write_resource(tmp_path, "tasks/analyze-issue.yaml", resource("Task", "analyze-issue", "1.0.0", {
        "objective": "Analyze issue.",
        "agentRef": ref("Agent", "issue-analyzer"),
        "outputs": {"type": "object"},
    }))

    with pytest.raises(ResourceValidationError, match="inputContextTokenBudget"):
        ResourceLoader(tmp_path).load()


def test_rejects_duplicate_versions(tmp_path: Path) -> None:
    write_valid_workspace(tmp_path, default_policies=[])
    duplicate = resource("Prompt", "issue-analysis", "1.0.0", {"system": "Analyze."})
    write_resource(tmp_path, "prompts/a.yaml", duplicate)
    write_resource(tmp_path, "prompts/nested/b.yaml", duplicate)

    with pytest.raises(DuplicateResourceError):
        ResourceLoader(tmp_path).load()


def test_rejects_workspace_resources_outside_workspace_file(tmp_path: Path) -> None:
    write_valid_workspace(tmp_path, default_policies=[])
    write_resource(tmp_path, "tasks/workspace.yaml", resource("Workspace", "secondary-workspace", "1.0.0", {
        "repository": {
            "provider": "github",
            "owner": "example",
            "name": "other-service",
            "defaultBranch": "main",
        },
        "resourceDiscovery": {"root": ".ai"},
    }))

    with pytest.raises(DuplicateWorkspaceError):
        ResourceLoader(tmp_path).load()


def write_valid_workspace(tmp_path: Path, default_policies: list[dict[str, str]] | None = None) -> None:
    spec = {
        "repository": {
            "provider": "github",
            "owner": "example",
            "name": "service",
            "defaultBranch": "main",
        },
        "resourceDiscovery": {"root": ".ai"},
    }
    if default_policies is None:
        default_policies = [ref("Policy", "default-publication")]
    if default_policies:
        spec["defaultPolicies"] = default_policies
    write_resource(tmp_path, "workspace.yaml", resource("Workspace", "default-workspace", "1.0.0", spec))


def write_resource(tmp_path: Path, relative_path: str, value: dict[str, object]) -> None:
    path = tmp_path / ".ai" / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def resource(kind: str, name: str, version: str, spec: dict[str, object]) -> dict[str, object]:
    return {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": kind,
        "metadata": {"name": name, "version": version},
        "spec": spec,
    }


def ref(kind: str, name: str, version: str = "1.0.0") -> dict[str, str]:
    return {"kind": kind, "name": name, "version": version}
