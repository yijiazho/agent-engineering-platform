from __future__ import annotations

import json
from pathlib import Path

from aep.build_implementation_plan import BuildImplementationPlanTaskHandler
from aep.context_builder import ContextBuilder
from aep.dogfood_runtime import _pinned_workspace_reader
from aep.generated_artifact_store import InMemoryGeneratedArtifactStore
from aep.repository_knowledge import (
    InMemoryRepositoryKnowledgeProvider,
    RepositoryFile,
    RepositoryKnowledgeSnapshot,
    SourceProvenance,
)


ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "fixtures" / "planning-evidence" / "issue-78-regression"
REVISION = "679a0c6f4eb04483aa917faae018a3037d3e82f9"
CREATED_AT = "2026-08-31T12:00:00Z"


def test_issue_78_regression_uses_exact_status_evidence_not_relevance() -> None:
    paths = sorted(
        path.relative_to(FIXTURE).as_posix()
        for path in (FIXTURE / "docs").rglob("*.md")
    )
    files = tuple(
        RepositoryFile(
            path=path, language="Markdown", is_documentation=True,
            provenance=SourceProvenance(
                source_path=path, repository_revision=REVISION,
                scanned_at=CREATED_AT, scanner_version="fixture-scanner/1.0.0",
            ),
        )
        for path in paths
    )
    provider = InMemoryRepositoryKnowledgeProvider(RepositoryKnowledgeSnapshot(
        api_version="aep.dev/repository-knowledge/v1",
        snapshot_version="issue-78-fixture-v1", repository_revision=REVISION,
        created_at=CREATED_AT, scanner_version="fixture-scanner/1.0.0",
        files=files, documentation=files, dependency_manifests=(),
        test_command_hints=(),
    ))
    declarations = [
        {
            "pathPrefix": "docs/tasks",
            "predicate": {"kind": "STATUS_EQUALS", "value": "In Progress"},
            "postcondition": {"kind": "STATUS_EQUALS", "value": "Completed"},
            "selectionReason": "Issue requests the In Progress to Completed transition",
            "maxBytes": 4096,
            "maxPaths": 20,
        },
        {
            "path": "docs/execution-plan.md",
            "predicate": {"kind": "TEXT_PRESENT", "value": "In Progress"},
            "postcondition": {"kind": "TEXT_ABSENT", "value": "In Progress"},
            "selectionReason": "Synchronize remaining pending execution-plan rows",
            "maxBytes": 4096,
        },
    ]
    task = {
        "apiVersion": "aep.dev/v1alpha1", "kind": "Task",
        "metadata": {"name": "build-implementation-plan", "version": "1.8.0"},
        "spec": {
            "objective": "Update In Progress manual-testing tasks and docs execution plan",
            "agentRef": {"kind": "Agent", "name": "planner", "version": "1.8.0"},
            "outputs": {"type": "object"},
            "requiredContext": ["candidate-files", "planning-evidence"],
            "inputContextTokenBudget": 32000,
            "planningPredicates": declarations,
        },
    }
    execution = _task_execution()
    workflow = _workflow_execution()
    issue = json.loads((FIXTURE / "issue.json").read_text(encoding="utf-8"))
    package = ContextBuilder(
        repository_knowledge=provider,
        artifact_store=InMemoryGeneratedArtifactStore(),
        repository_file_reader=_pinned_workspace_reader(FIXTURE, REVISION),
    ).build(
        task=task, task_execution=execution, workflow_execution=workflow,
        event=issue, created_at=CREATED_AT,
    )

    ranked = {
        item["content"]["source"]["path"]
        for item in package["elements"] if item["type"] == "repository"
    }
    evidence = [
        item["content"] for item in package["elements"]
        if item["type"] == "planning-evidence"
    ]
    assert set(paths).issubset(ranked)  # relevance admits all five false positives
    assert len(evidence) == len(paths)
    assert all("content" not in item for item in evidence)
    assert sum(item["preimageSha256"] != "" for item in evidence) == len(paths)

    model_plan = {
        # Simulate the relevance-only failure: the model proposes one historical
        # completed file and omits the genuinely pending files.
        "intendedFiles": [expected_historical := paths[1]],
        "tests": ["python -m pytest"], "assumptions": [],
        "risks": [], "implementationSteps": ["Update status fields."],
        "acceptanceCriteriaClassifications": [], "unsupportedAcceptanceCriteria": [],
    }
    handler = object.__new__(BuildImplementationPlanTaskHandler)
    authoritative = handler._authoritative_output(model_plan, execution, workflow, package)
    expected = json.loads(
        (FIXTURE / "expected-context-manifest.json").read_text(encoding="utf-8")
    )
    assert authoritative["requiredChangePaths"] == expected["requiredChangePaths"]
    assert expected_historical in authoritative["verifiedNoChangePaths"]
    assert authoritative["authorizedPaths"] == paths
    assert authoritative["verifiedNoChangePaths"] == expected["verifiedNoChangePaths"]
    assert authoritative["unsupportedPaths"] == expected["unsupportedPaths"]
    assert len(authoritative["pathEvidence"]) == expected["boundedPathCount"]
    assert set(authoritative["requiredChangePaths"]).isdisjoint(
        authoritative["verifiedNoChangePaths"]
    )


def _read(path: str, revision: str, limit: int) -> str:
    assert revision == REVISION
    content = (FIXTURE / path).read_text(encoding="utf-8")
    if len(content.encode("utf-8")) > limit:
        raise ValueError("oversized fixture target")
    return content


def _workflow_execution() -> dict:
    return {
        "apiVersion": "aep.dev/v1alpha1", "kind": "WorkflowExecution",
        "id": "workflowexecution-787878787878", "traceId": "trace-issue-78-regression",
        "createdAt": CREATED_AT, "updatedAt": CREATED_AT,
        "provenance": {"actor": "fixture", "repositoryRevision": REVISION,
            "resourceRefs": []},
        "workflowRef": {"kind": "Workflow", "name": "issue-to-pr", "version": "1.16.0"},
        "eventId": "event-issue-78-derived",
        "eventRef": {"kind": "Event", "name": "github-issue-created", "version": "1.0.0"},
        "repositoryRevision": REVISION, "knowledgeGraphVersion": "issue-78-fixture-v1",
        "status": "RUNNING", "startedAt": CREATED_AT,
        "taskExecutionIds": ["taskexecution-787878787878"],
    }


def _task_execution() -> dict:
    return {
        "apiVersion": "aep.dev/v1alpha1", "kind": "TaskExecution",
        "id": "taskexecution-787878787878", "traceId": "trace-issue-78-regression",
        "createdAt": CREATED_AT, "updatedAt": CREATED_AT,
        "provenance": {"actor": "fixture",
            "workflowExecutionId": "workflowexecution-787878787878",
            "repositoryRevision": REVISION,
            "resourceRefs": [{"kind": "Task", "name": "build-implementation-plan", "version": "1.8.0"}]},
        "workflowExecutionId": "workflowexecution-787878787878",
        "taskRef": {"kind": "Task", "name": "build-implementation-plan", "version": "1.8.0"},
        "attempt": 1, "status": "RUNNING", "dependencyTaskExecutionIds": [],
        "startedAt": CREATED_AT,
    }
