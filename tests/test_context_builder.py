import json
from pathlib import Path

import pytest

from aep.context_builder import (
    ContextBudgetExceededError,
    ContextBuilder,
    ContextInputValidationError,
    ImmutableContextPackageError,
    RequiredContextError,
)
from aep.generated_artifact_store import InMemoryGeneratedArtifactStore
from aep.observability import StructuredLifecycleLogger
from aep.repository_knowledge import (
    DependencyManifest,
    InMemoryRepositoryKnowledgeProvider,
    RepositoryFile,
    RepositoryKnowledgeSnapshot,
    SourceProvenance,
    TestCommandHint as RepositoryTestCommandHint,
)
from aep.runtime_store import InMemoryRuntimeObjectStore


REVISION = "abc1234"
CREATED_AT = "2026-07-12T10:00:00Z"
WORKFLOW_ID = "workflowexecution-cccccccccccc"
TASK_EXECUTION_ID = "taskexecution-bbbbbbbbbbbb"
PRIOR_TASK_EXECUTION_ID = "taskexecution-aaaaaaaaaaaa"


def source(path: str, *, revision: str = REVISION) -> SourceProvenance:
    return SourceProvenance(
        source_path=path,
        repository_revision=revision,
        scanned_at=CREATED_AT,
        scanner_version="mvp-scanner/1.0.0",
    )


def knowledge_provider(
    *, revision: str = REVISION, snapshot_version: str = "snapshot-context-v1"
) -> InMemoryRepositoryKnowledgeProvider:
    files = (
        RepositoryFile("README.md", "Markdown", True, source("README.md", revision=revision)),
        RepositoryFile(
            "pyproject.toml",
            "TOML",
            False,
            source("pyproject.toml", revision=revision),
        ),
        RepositoryFile(
            "src/aep/context_builder.py",
            "Python",
            False,
            source("src/aep/context_builder.py", revision=revision),
        ),
        RepositoryFile(
            "tests/test_context_builder.py",
            "Python",
            False,
            source("tests/test_context_builder.py", revision=revision),
        ),
        RepositoryFile(
            "docs/context-builder.md",
            "Markdown",
            True,
            source("docs/context-builder.md", revision=revision),
        ),
    )
    return InMemoryRepositoryKnowledgeProvider(
        RepositoryKnowledgeSnapshot(
            api_version="aep.dev/repository-knowledge/v1",
            snapshot_version=snapshot_version,
            repository_revision=revision,
            created_at=CREATED_AT,
            scanner_version="mvp-scanner/1.0.0",
            files=files,
            documentation=(files[0], files[4]),
            dependency_manifests=(
                DependencyManifest(
                    "pyproject.toml",
                    "python",
                    "dependencies",
                    source("pyproject.toml", revision=revision),
                ),
            ),
            test_command_hints=(
                RepositoryTestCommandHint(
                    "python -m pytest",
                    "pyproject.toml",
                    source("pyproject.toml", revision=revision),
                ),
            ),
        )
    )


def task(name: str, required_context: list[str]) -> dict:
    resource = {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "Task",
        "metadata": {"name": name, "version": "1.0.0"},
        "spec": {
            "objective": f"Execute {name} for context builder tests.",
            "outputs": {"type": "object"},
            "requiredContext": required_context,
            "inputContextTokenBudget": 32_000,
        },
    }
    if "knowledge" in required_context:
        resource["spec"]["knowledgeBases"] = [
            {
                "kind": "KnowledgeBase",
                "name": "repository-docs",
                "version": "1.0.0",
            }
        ]
    if "policies" in required_context:
        resource["spec"]["policies"] = [
            {
                "kind": "Policy",
                "name": "default-publication",
                "version": "1.0.0",
            }
        ]
    return resource


def task_execution(task_resource: dict) -> dict:
    execution = {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "TaskExecution",
        "id": TASK_EXECUTION_ID,
        "traceId": "trace-context-builder-0001",
        "createdAt": CREATED_AT,
        "updatedAt": CREATED_AT,
        "provenance": {
            "actor": "task-executor",
            "workflowExecutionId": WORKFLOW_ID,
            "repositoryRevision": REVISION,
            "resourceRefs": [],
        },
        "workflowExecutionId": WORKFLOW_ID,
        "taskRef": {
            "kind": "Task",
            "name": task_resource["metadata"]["name"],
            "version": task_resource["metadata"]["version"],
        },
        "attempt": 1,
        "status": "RUNNING",
    }
    if "prior-artifacts" in task_resource["spec"].get("requiredContext", ()):
        execution["dependencyTaskExecutionIds"] = [PRIOR_TASK_EXECUTION_ID]
    return execution


def workflow_execution() -> dict:
    return {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "WorkflowExecution",
        "id": WORKFLOW_ID,
        "traceId": "trace-context-builder-0001",
        "createdAt": CREATED_AT,
        "updatedAt": CREATED_AT,
        "provenance": {
            "actor": "workflow-controller",
            "repositoryRevision": REVISION,
            "resourceRefs": [],
        },
        "workflowRef": {"kind": "Workflow", "name": "issue-to-pr", "version": "1.0.0"},
        "eventRef": {"kind": "Event", "name": "github-issue-created", "version": "1.0.0"},
        "eventId": "event-11111111-1111-1111-1111-111111111111",
        "repositoryRevision": REVISION,
        "knowledgeGraphVersion": "snapshot-context-v1",
        "status": "RUNNING",
    }


def event() -> dict:
    return {
        "id": "event-11111111-1111-1111-1111-111111111111",
        "source": "github",
        "type": "github.issue.created",
        "repository": {"id": 1, "full_name": "aep/example"},
        "issue": {"id": 2, "number": 42, "title": "Implement context builder"},
        "sender": {"id": 3, "login": "octocat"},
        "receivedAt": CREATED_AT,
        "deduplicationKey": "github:delivery:delivery-1",
    }


def knowledge_base() -> dict:
    return {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "KnowledgeBase",
        "metadata": {"name": "repository-docs", "version": "1.0.0"},
        "spec": {
            "sources": [{"type": "docs", "path": "docs/"}],
            "indexing": {"strategy": "documentation"},
            "refreshPolicy": "on-change",
            "visibility": "workspace",
        },
    }


def policy() -> dict:
    return {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "Policy",
        "metadata": {"name": "default-publication", "version": "1.0.0"},
        "spec": {"type": "publication", "rules": [{"effect": "allow"}]},
    }


def artifact_store() -> InMemoryGeneratedArtifactStore:
    store = InMemoryGeneratedArtifactStore()
    store.publish(
        {
            "apiVersion": "aep.dev/v1alpha1",
            "kind": "GeneratedArtifact",
            "id": "generatedartifact-aaaaaaaaaaaa",
            "traceId": "trace-context-builder-0001",
            "createdAt": CREATED_AT,
            "updatedAt": CREATED_AT,
            "provenance": {
                "actor": "artifact-store",
                "workflowExecutionId": WORKFLOW_ID,
                "taskExecutionId": PRIOR_TASK_EXECUTION_ID,
                "repositoryRevision": REVISION,
                "resourceRefs": [],
            },
            "taskExecutionId": PRIOR_TASK_EXECUTION_ID,
            "artifactType": "IMPLEMENTATION_PLAN",
            "repositoryRevision": REVISION,
            "mediaType": "application/json",
            "publishedAt": CREATED_AT,
        },
        {"steps": ["implement", "test"]},
    )
    return store


def seed_producer(
    store: InMemoryRuntimeObjectStore, **changes
) -> InMemoryRuntimeObjectStore:
    producer = {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "TaskExecution",
        "id": PRIOR_TASK_EXECUTION_ID,
        "traceId": "trace-context-builder-0001",
        "createdAt": CREATED_AT,
        "updatedAt": CREATED_AT,
        "provenance": {
            "actor": "task-executor",
            "workflowExecutionId": WORKFLOW_ID,
            "repositoryRevision": REVISION,
            "resourceRefs": [],
        },
        "workflowExecutionId": WORKFLOW_ID,
        "taskRef": {"kind": "Task", "name": "analyze-issue", "version": "1.0.0"},
        "attempt": 1,
        "status": "SUCCEEDED",
        "completedAt": CREATED_AT,
    }
    for key, value in changes.items():
        if key.startswith("provenance_"):
            producer["provenance"][key.removeprefix("provenance_")] = value
        else:
            producer[key] = value
    store.create(producer, deterministic_key=f"task-execution:{PRIOR_TASK_EXECUTION_ID}")
    return store


def builder(
    *,
    provider: InMemoryRepositoryKnowledgeProvider | None = None,
    runtime_store: InMemoryRuntimeObjectStore | None = None,
    lifecycle_logger: StructuredLifecycleLogger | None = None,
) -> ContextBuilder:
    resolved_runtime_store = runtime_store or InMemoryRuntimeObjectStore()
    if resolved_runtime_store.get(PRIOR_TASK_EXECUTION_ID) is None:
        seed_producer(resolved_runtime_store)
    return ContextBuilder(
        repository_knowledge=provider or knowledge_provider(),
        artifact_store=artifact_store(),
        runtime_store=resolved_runtime_store,
        lifecycle_logger=lifecycle_logger,
    )


def build(task_resource: dict, **changes):
    values = {
        "task": task_resource,
        "task_execution": task_execution(task_resource),
        "workflow_execution": workflow_execution(),
        "event": event(),
        "created_at": CREATED_AT,
    }
    values.update(changes)
    return builder().build(**values)


def test_builds_context_for_every_mvp_task_type_from_deterministic_fixture() -> None:
    fixture_path = (
        Path(__file__).parents[1]
        / "fixtures"
        / "context-builder"
        / "mvp-task-types.json"
    )
    task_types = json.loads(fixture_path.read_text(encoding="utf-8"))

    for definition in task_types:
        required = definition["requiredContext"]
        task_resource = task(definition["name"], required)
        context = builder().build(
            task=task_resource,
            task_execution=task_execution(task_resource),
            workflow_execution=workflow_execution(),
            event=event(),
            knowledge_bases=(knowledge_base(),) if "knowledge" in required else (),
            policies=(policy(),) if "policies" in required else (),
            prior_task_execution_ids=(PRIOR_TASK_EXECUTION_ID,)
            if "prior-artifacts" in required
            else (),
            optional_context=definition["optionalContext"],
            token_budget=32_000,
            created_at=CREATED_AT,
        )

        element_types = {element["type"] for element in context["elements"]}
        expected_types = set(definition["expectedTypes"])
        # Cross-category repository identities are represented once; their
        # complete selection reasons remain on the surviving element.
        if "knowledge" in expected_types and "knowledge" not in element_types:
            assert any(
                "knowledge" in element.get("content", {}).get("selectionReasons", ())
                for element in context["elements"]
            )
            expected_types.remove("knowledge")
        assert expected_types.issubset(element_types)
        assert context["selection"]["requiredContext"] == tuple(sorted(required))
        assert context["tokenEstimate"]["count"] == context["tokenCount"]
        assert context["tokenCount"] <= context["tokenBudget"]
        assert context["provenance"]["knowledgeGraphVersion"] == "snapshot-context-v1"
        assert all(element["provenance"]["actor"] for element in context["elements"])


def test_identical_inputs_produce_identical_recursive_immutable_package() -> None:
    task_resource = task("analyze-issue", ["issue", "repository-inventory"])

    first = build(task_resource)
    second = build(task_resource)

    assert first == second
    assert first["id"] == second["id"]
    with pytest.raises(TypeError):
        first["tokenCount"] = 0
    with pytest.raises(TypeError):
        first["elements"][0]["content"]["spec"] = {}


def test_persists_idempotently_when_runtime_store_is_supplied() -> None:
    runtime_store = InMemoryRuntimeObjectStore()
    context_builder = builder(runtime_store=runtime_store)
    task_resource = task("analyze-issue", ["issue", "repository-inventory"])
    inputs = {
        "task": task_resource,
        "task_execution": task_execution(task_resource),
        "workflow_execution": workflow_execution(),
        "event": event(),
        "created_at": CREATED_AT,
    }

    first = context_builder.build(**inputs)
    second = context_builder.build(**inputs)

    assert first == second
    assert runtime_store.get(first["id"])["kind"] == "ContextPackage"


def test_emits_context_created_with_boundary_correlation() -> None:
    captured: list[dict[str, object]] = []
    logger = StructuredLifecycleLogger(lambda value: captured.append(dict(value)))
    task_resource = task("analyze-issue", ["issue"])

    context = builder(lifecycle_logger=logger).build(
        task=task_resource,
        task_execution=task_execution(task_resource),
        workflow_execution=workflow_execution(),
        event=event(),
        created_at=CREATED_AT,
    )

    assert len(captured) == 1
    assert captured[0]["eventName"] == "ContextPackageCreated"
    assert captured[0]["traceId"] == context["traceId"]
    assert captured[0]["executionId"] == WORKFLOW_ID
    assert captured[0]["taskId"] == TASK_EXECUTION_ID
    assert captured[0]["repositoryRevision"] == REVISION


def test_rejects_replacing_the_package_for_one_task_execution() -> None:
    runtime_store = InMemoryRuntimeObjectStore()
    context_builder = builder(runtime_store=runtime_store)
    task_resource = task("analyze-issue", ["issue"])
    inputs = {
        "task": task_resource,
        "task_execution": task_execution(task_resource),
        "workflow_execution": workflow_execution(),
        "event": event(),
        "created_at": CREATED_AT,
    }
    context_builder.build(**inputs)

    with pytest.raises(ImmutableContextPackageError, match="already has a different"):
        context_builder.build(**inputs, optional_context=("repository-inventory",))


def test_rejects_missing_required_context() -> None:
    task_resource = task("build-implementation-plan", ["prior-artifacts"])

    with pytest.raises(RequiredContextError, match="prior-artifacts"):
        build(task_resource)


def test_rejects_unknown_required_context_instead_of_silently_omitting_it() -> None:
    task_resource = task("analyze-issue", ["semantic-magic"])

    with pytest.raises(ContextInputValidationError, match="unsupported"):
        build(task_resource)


@pytest.mark.parametrize("case", ["missing", "wrong-name", "wrong-version", "extra"])
def test_requires_exact_task_declared_knowledge_base_references(case: str) -> None:
    task_resource = task("analyze-issue", ["knowledge"])
    matching = knowledge_base()
    other = knowledge_base()
    if case == "missing":
        supplied = ()
    elif case == "wrong-name":
        other["metadata"]["name"] = "other-docs"
        supplied = (other,)
    elif case == "wrong-version":
        other["metadata"]["version"] = "2.0.0"
        supplied = (other,)
    else:
        other["metadata"]["name"] = "other-docs"
        supplied = (matching, other)

    with pytest.raises(ContextInputValidationError, match="do not match"):
        build(task_resource, knowledge_bases=supplied)


def test_requires_exact_task_declared_policy_references() -> None:
    task_resource = task("create-pull-request", ["policies"])
    wrong_policy = policy()
    wrong_policy["metadata"]["version"] = "2.0.0"

    with pytest.raises(ContextInputValidationError, match="Task.spec.policies"):
        build(task_resource, policies=(wrong_policy,))


def test_schema_validates_supplied_resources() -> None:
    task_resource = task("create-pull-request", ["policies"])
    invalid_policy = policy()
    del invalid_policy["spec"]["rules"]

    with pytest.raises(ContextInputValidationError, match="invalid Policy Resource"):
        build(task_resource, policies=(invalid_policy,))


def test_requires_triggering_event_when_workflow_records_an_event_reference() -> None:
    task_resource = task("analyze-issue", ["task"])

    with pytest.raises(ContextInputValidationError, match="event is required"):
        build(task_resource, event=None)


def test_rejects_event_that_does_not_match_workflow_event_id() -> None:
    task_resource = task("analyze-issue", ["issue"])
    wrong_event = event()
    wrong_event["id"] = "event-22222222-2222-2222-2222-222222222222"

    with pytest.raises(ContextInputValidationError, match="does not match"):
        build(task_resource, event=wrong_event)


def test_rejects_malformed_issue_when_issue_context_is_required() -> None:
    task_resource = task("analyze-issue", ["issue"])
    malformed_event = event()
    del malformed_event["issue"]["title"]

    with pytest.raises(ContextInputValidationError, match="Event.issue.title"):
        build(task_resource, event=malformed_event)


def test_rejects_extraneous_event_without_workflow_event_binding() -> None:
    task_resource = task("internal-task", ["task"])
    unbound_workflow = workflow_execution()
    del unbound_workflow["eventId"]
    del unbound_workflow["eventRef"]

    with pytest.raises(ContextInputValidationError, match="not bound"):
        build(
            task_resource,
            workflow_execution=unbound_workflow,
            event=event(),
        )


def test_rejects_mixed_repository_revisions() -> None:
    context_builder = builder(provider=knowledge_provider(revision="def5678"))
    task_resource = task("analyze-issue", ["repository-inventory"])

    with pytest.raises(ContextInputValidationError, match="expected 'abc1234'"):
        context_builder.build(
            task=task_resource,
            task_execution=task_execution(task_resource),
            workflow_execution=workflow_execution(),
            event=event(),
            created_at=CREATED_AT,
        )


def test_rejects_repository_snapshot_other_than_workflow_binding() -> None:
    context_builder = builder(
        provider=knowledge_provider(snapshot_version="snapshot-context-v2")
    )
    task_resource = task("analyze-issue", ["repository-inventory"])

    with pytest.raises(ContextInputValidationError, match="snapshot-context-v1"):
        context_builder.build(
            task=task_resource,
            task_execution=task_execution(task_resource),
            workflow_execution=workflow_execution(),
            event=event(),
            created_at=CREATED_AT,
        )


def test_required_context_cannot_be_pruned_to_fit_budget() -> None:
    task_resource = task("analyze-issue", ["repository-inventory"])
    task_resource["spec"]["inputContextTokenBudget"] = 1

    with pytest.raises(ContextBudgetExceededError, match="mandatory context"):
        build(task_resource)


def test_optional_context_is_pruned_and_selection_is_explained() -> None:
    task_resource = task("analyze-issue", ["issue"])
    task_resource["spec"]["optionalContext"] = ["repository-inventory"]
    task_resource["spec"]["inputContextTokenBudget"] = 300

    context = build(task_resource)

    assert context["truncation"] == "PRUNED"
    assert context["selection"]["discarded"]
    assert all(
        discarded["reason"] == "TOKEN_BUDGET"
        for discarded in context["selection"]["discarded"]
    )


def test_controlled_issue_context_is_relevant_bounded_and_deduplicated() -> None:
    fixture = json.loads(
        (
            Path(__file__).parents[1]
            / "fixtures/context-builder/analyze-issue-token-efficiency.json"
        ).read_text(encoding="utf-8")
    )
    padding = "x" * fixture["metadataPaddingBytes"]
    generated_paths = [
        f"src/zmodule_{index:03d}.py"
        for index in range(fixture["repositoryFileCount"] - 2)
    ]
    paths = [*fixture["allowedPaths"], *generated_paths]
    files = tuple(
        RepositoryFile(
            path,
            f"Python-{padding}",
            index % 10 == 0,
            source(path),
        )
        for index, path in enumerate(paths)
    )
    assert sum((len(file.language.encode("utf-8")) + 3) // 4 for file in files) > 120_000
    provider = InMemoryRepositoryKnowledgeProvider(
        RepositoryKnowledgeSnapshot(
            api_version="aep.dev/repository-knowledge/v1",
            snapshot_version="snapshot-context-v1",
            repository_revision=REVISION,
            created_at=CREATED_AT,
            scanner_version="mvp-scanner/1.0.0",
            files=files,
            documentation=tuple(file for file in files if file.is_documentation),
            dependency_manifests=(),
            test_command_hints=(),
        )
    )
    task_resource = task("analyze-issue", ["event", "issue", "candidate-files"])
    task_resource["metadata"]["version"] = "1.1.0"
    task_resource["spec"].update(
        {
            "optionalContext": [
                "repository-inventory",
                "documentation",
                "knowledge",
            ],
            "inputContextTokenBudget": fixture["inputContextTokenBudget"],
            "knowledgeBases": [
                {
                    "kind": "KnowledgeBase",
                    "name": "repository-docs",
                    "version": "1.1.0",
                }
            ],
        }
    )
    kb = knowledge_base()
    kb["metadata"]["version"] = "1.1.0"
    kb["spec"]["sources"] = [
        {"type": "repository", "path": "src/", "limit": 8},
        {"type": "repository", "path": "tests/", "limit": 8},
    ]
    issue_event = event()
    issue_event["issue"].update(fixture["issue"])

    context = builder(provider=provider).build(
        task=task_resource,
        task_execution=task_execution(task_resource),
        workflow_execution=workflow_execution(),
        event=issue_event,
        knowledge_bases=(kb,),
        created_at=CREATED_AT,
    )

    paths_in_context = [
        element["content"]["source"]["path"]
        for element in context["elements"]
        if element["type"] in {"repository", "knowledge"}
    ]
    identities = [
        (
            element["provenance"]["repositoryRevision"],
            element["content"]["source"]["path"],
            element["content"]["source"].get("startLine"),
            element["content"]["source"].get("endLine"),
        )
        for element in context["elements"]
        if element["type"] in {"repository", "knowledge"}
    ]
    assert context["tokenCount"] <= 32_000
    assert set(fixture["allowedPaths"]) <= set(paths_in_context)
    assert len(paths_in_context) < fixture["repositoryFileCount"]
    assert len(identities) == len(set(identities))
    runtime_store_element = next(
        element
        for element in context["elements"]
        if element["type"] in {"repository", "knowledge"}
        and element["content"]["source"].get("path")
        == "src/aep/runtime_store.py"
    )
    assert {"candidate-files", "repository-inventory", "knowledge"} <= set(
        runtime_store_element["content"]["selectionReasons"]
    )
    assert runtime_store_element["content"]["retrieval"][
        "selectionTraversalPaths"
    ]
    assert sum(
        category["tokenCount"]
        for category in context["tokenEstimate"]["breakdown"].values()
    ) == context["tokenCount"]
    assert context["id"] == builder(provider=provider).build(
        task=task_resource,
        task_execution=task_execution(task_resource),
        workflow_execution=workflow_execution(),
        event=issue_event,
        knowledge_bases=(kb,),
        created_at=CREATED_AT,
    )["id"]


def test_artifact_content_and_content_address_provenance_are_preserved() -> None:
    task_resource = task("generate-patch", ["prior-artifacts"])

    context = builder().build(
        task=task_resource,
        task_execution=task_execution(task_resource),
        workflow_execution=workflow_execution(),
        event=event(),
        prior_task_execution_ids=(PRIOR_TASK_EXECUTION_ID,),
        created_at=CREATED_AT,
    )

    artifact = next(element for element in context["elements"] if element["type"] == "artifact")
    assert artifact["content"]["content"] == {"steps": ("implement", "test")}
    assert artifact["provenance"]["inputArtifactRefs"][0]["contentAddress"].startswith("sha256:")
    assert artifact["provenance"]["taskExecutionId"] == PRIOR_TASK_EXECUTION_ID


def test_rejects_artifact_producer_that_is_not_a_task_dependency() -> None:
    task_resource = task("generate-patch", ["prior-artifacts"])
    current_execution = task_execution(task_resource)
    current_execution["dependencyTaskExecutionIds"] = []

    with pytest.raises(ContextInputValidationError, match="is not a dependency"):
        builder().build(
            task=task_resource,
            task_execution=current_execution,
            workflow_execution=workflow_execution(),
            event=event(),
            prior_task_execution_ids=(PRIOR_TASK_EXECUTION_ID,),
            created_at=CREATED_AT,
        )


@pytest.mark.parametrize(
    ("producer_changes", "message"),
    [
        (
            {
                "workflowExecutionId": "workflowexecution-dddddddddddd",
                "provenance_workflowExecutionId": "workflowexecution-dddddddddddd",
            },
            "another WorkflowExecution",
        ),
        ({"traceId": "trace-other-context-0001"}, "another trace"),
        ({"status": "RUNNING", "completedAt": None}, "has not succeeded"),
    ],
)
def test_rejects_cross_execution_artifact_producers(
    producer_changes: dict, message: str
) -> None:
    runtime_store = seed_producer(InMemoryRuntimeObjectStore(), **producer_changes)
    task_resource = task("generate-patch", ["prior-artifacts"])

    with pytest.raises(ContextInputValidationError, match=message):
        builder(runtime_store=runtime_store).build(
            task=task_resource,
            task_execution=task_execution(task_resource),
            workflow_execution=workflow_execution(),
            event=event(),
            prior_task_execution_ids=(PRIOR_TASK_EXECUTION_ID,),
            created_at=CREATED_AT,
        )


def test_repository_knowledge_base_uses_provider_query_boundary() -> None:
    task_resource = task("analyze-issue", ["knowledge"])
    repository_knowledge_base = knowledge_base()
    repository_knowledge_base["spec"]["sources"] = [
        {"type": "repository", "path": "src/"}
    ]

    context = builder().build(
        task=task_resource,
        task_execution=task_execution(task_resource),
        workflow_execution=workflow_execution(),
        event=event(),
        knowledge_bases=(repository_knowledge_base,),
        created_at=CREATED_AT,
    )

    knowledge = [
        element for element in context["elements"] if element["type"] == "knowledge"
    ]
    assert [element["content"]["source"]["path"] for element in knowledge] == [
        "src/aep/context_builder.py"
    ]
