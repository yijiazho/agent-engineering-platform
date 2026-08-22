from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource as SchemaResource
from referencing.jsonschema import DRAFT202012

from aep.create_pull_request import CreatePullRequestTaskHandler
from aep.execution_checkout import (
    CheckoutState,
    ExecutionCheckout,
    RepositoryIdentity,
)
from aep.generated_artifact_store import (
    GeneratedArtifactStore,
    GeneratedArtifactStoreError,
    InMemoryGeneratedArtifactStore,
)
from aep.git_tool import GIT_INPUT_SCHEMA, GIT_OUTPUT_SCHEMA, GitTool
from aep.github_tool import (
    GITHUB_INPUT_SCHEMA,
    GITHUB_OUTPUT_SCHEMA,
    GitHubProviderError,
    GitHubToolAdapter,
)
from aep.resource_loader import ResourceCollection, ResourceLoader, ResourceRef
from aep.runtime_store import InMemoryRuntimeObjectStore
from aep.task_execution import FailureClass
from aep.tool_runtime import (
    ToolFailureClass,
    ToolMetrics,
    ToolRequest,
    ToolResult,
    ToolResultStatus,
)
from test_evaluate_acceptance import (
    ACCEPTANCE_ID,
    REVISION,
    TIMESTAMP,
    TRACE_ID,
    WORKFLOW_ID,
    ref,
    resource,
    setup_handler as setup_acceptance,
)


ROOT = Path(__file__).parents[1]
CREATE_ID = "taskexecution-777777777777"
CREATE_RETRY_ID = "taskexecution-888888888888"
HEAD_REVISION = "b" * 40


class FakeGitTool(GitTool):
    def __init__(
        self,
        store: InMemoryRuntimeObjectStore,
        *,
        failure: ToolFailureClass | None = None,
        repository: str = "octo/repo",
    ) -> None:
        self.store = store
        self.failure = failure
        self.repository = repository
        self.calls: list[ToolRequest] = []

    def invoke(
        self,
        *,
        invocation_id: str,
        task_execution_id: str,
        request: ToolRequest,
        authorize,
        policy_decision_id: str | None = None,
    ):
        existing = self.store.get(invocation_id)
        if existing is not None:
            return _result_from_record(existing), existing
        self.calls.append(request)
        if not authorize(request):
            raise AssertionError("handler supplied an unauthorized Git request")
        operation = request.input["operation"]
        failure = self.failure if operation == "push_branch" else None
        output = {
            "operation": operation,
            "repository": self.repository,
            "branch": request.input["branch"],
            "revision": HEAD_REVISION,
            "baseRevision": request.input["expectedRevision"],
            "changedFiles": [],
            "diff": None,
            "remoteMutationState": (
                (
                    "NOT_ATTEMPTED"
                    if failure is ToolFailureClass.STARTUP
                    else "UNKNOWN"
                )
                if operation == "push_branch" and failure
                else "CONFIRMED"
                if operation == "push_branch"
                else "NOT_ATTEMPTED"
            ),
            "commandResults": [],
        }
        result = ToolResult(
            status=(
                ToolResultStatus.FAILED
                if failure is not None
                else ToolResultStatus.SUCCEEDED
            ),
            output=output,
            logs_ref=None,
            metrics=ToolMetrics(duration_ms=1),
            started_at=TIMESTAMP,
            completed_at=TIMESTAMP,
            failure_class=failure,
            failure_message="push failed" if failure else None,
        )
        record = {
            "apiVersion": "aep.dev/v1alpha1",
            "kind": "ToolInvocation",
            "id": invocation_id,
            "traceId": request.trace_id,
            "createdAt": TIMESTAMP,
            "updatedAt": TIMESTAMP,
            "provenance": {
                "actor": "tool-runtime",
                "caller": f"TaskExecution:{task_execution_id}",
                "workflowExecutionId": WORKFLOW_ID,
                "taskExecutionId": task_execution_id,
                "repositoryRevision": REVISION,
                "resourceRefs": [dict(request.tool_ref)],
            },
            "taskExecutionId": task_execution_id,
            "toolRef": dict(request.tool_ref),
            "status": "FAILED" if failure else "SUCCEEDED",
            "input": dict(request.input),
            "output": result.output_record(),
            "capabilities": list(request.capabilities),
            "policyDecisionId": policy_decision_id,
            "resultStatus": result.status.value,
            "metrics": result.metrics.as_record(),
            "startedAt": TIMESTAMP,
            "completedAt": TIMESTAMP,
        }
        if failure:
            record.update(
                {
                    "failureClass": failure.value,
                    "failure": {
                        "class": "RECOVERABLE",
                        "message": "push failed",
                        "retryable": True,
                    },
                }
            )
        persisted = self.store.create(
            record, deterministic_key=f"fake-git:{invocation_id}"
        )
        return result, persisted


class FakeOperation:
    def __init__(self, outcome: Mapping[str, Any] | Exception) -> None:
        self.outcome = outcome
        self.request_id = (
            outcome.get("requestId")
            if isinstance(outcome, Mapping)
            else getattr(outcome, "request_id", None)
        )

    def wait(self, timeout_ms: int):
        return self.outcome

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass

    def cleanup(self) -> None:
        pass


class FakeGitHubClient:
    def __init__(self, outcome: Mapping[str, Any] | Exception | None = None) -> None:
        self.outcome = outcome or {
            "number": 42,
            "url": "https://github.com/octo/repo/pull/42",
            "requestId": "provider-request-42",
        }
        self.calls: list[dict[str, str]] = []

    def start_read_issue(self, repository: str, issue_number: int):
        raise AssertionError("CreatePullRequest must not read the issue via the Tool")

    def start_create_pull_request(
        self,
        repository: str,
        *,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> FakeOperation:
        self.calls.append(
            {
                "repository": repository,
                "head": head,
                "base": base,
                "title": title,
                "body": body,
            }
        )
        return FakeOperation(self.outcome)


class FailDescriptionOnceStore(GeneratedArtifactStore):
    def __init__(self, delegate: InMemoryGeneratedArtifactStore) -> None:
        self.delegate = delegate
        self.fail_description = False

    def publish(self, metadata, content):
        if self.fail_description and metadata.get("artifactType") == "PULL_REQUEST_DESCRIPTION":
            self.fail_description = False
            raise GeneratedArtifactStoreError("transient artifact failure")
        return self.delegate.publish(metadata, content)

    def get(self, artifact_id: str):
        return self.delegate.get(artifact_id)

    def get_content(self, artifact_id: str):
        return self.delegate.get_content(artifact_id)

    def list_by_task_execution(self, task_execution_id: str):
        return self.delegate.list_by_task_execution(task_execution_id)


def test_success_publishes_after_all_three_policy_gates() -> None:
    store, handler, task, artifacts, git, github = setup_handler()

    result = handler.execute(task, store.get(CREATE_ID))

    assert result.succeeded is True
    assert [call.input["operation"] for call in git.calls] == [
        "commit_changes",
        "push_branch",
    ]
    assert len(github.calls) == 1
    assert github.calls[0]["title"] == "Issue #34: Add publication"
    assert "Closes #34" in github.calls[0]["body"]
    execution = store.get(CREATE_ID)
    assert len(execution["policyDecisionIds"]) == 3
    assert [store.get(item)["decision"] for item in execution["policyDecisionIds"]] == [
        "ALLOW",
        "ALLOW",
        "ALLOW",
    ]
    publication = next(
        store.get(item)
        for item in execution["policyDecisionIds"]
        if store.get(item)["gate"] == "PUBLICATION"
    )
    assert publication["publicationTarget"]["commitToolInvocationId"] == (
        execution["toolInvocationIds"][0]
    )
    assert len(execution["toolInvocationIds"]) == 3
    description = artifacts.get(execution["generatedArtifactIds"][0])
    assert description["artifactType"] == "PULL_REQUEST_DESCRIPTION"
    assert description["pullRequestNumber"] == 42
    assert description["pullRequestUrl"].endswith("/pull/42")
    assert description["providerRequestId"] == "provider-request-42"
    assert description["gitCommitToolInvocationId"] == (
        execution["toolInvocationIds"][0]
    )
    assert description["policyDecisionIds"] == execution["policyDecisionIds"]


def test_repository_resources_authorize_all_three_gates_and_create_one_fake_pr() -> None:
    store, handler, task, artifacts, git, github = setup_handler(
        repository_resources=True
    )

    result = handler.execute(task, store.get(CREATE_ID))

    assert result.succeeded is True
    execution = store.get(CREATE_ID)
    decisions = [store.get(item) for item in execution["policyDecisionIds"]]
    assert [(item["gate"], item["action"], item["decision"]) for item in decisions] == [
        ("PUBLICATION", "github.create_pr", "ALLOW"),
        ("PRE_EXECUTION_CAPABILITY", "git.push", "ALLOW"),
        ("PRE_EXECUTION_CAPABILITY", "github.create_pr", "ALLOW"),
    ]
    publication = decisions[0]
    assert publication["policyRefs"] == [
        {"kind": "Policy", "name": "publication-evidence", "version": "1.1.0"}
    ]
    assert publication["evaluatedRule"]["ruleIndex"] == 0
    assert len(github.calls) == 1
    assert len(artifacts.list_by_task_execution(CREATE_ID)) == 1


def test_emergency_disable_prevents_commit_push_and_pull_request() -> None:
    store, handler, task, _artifacts, git, github = setup_handler(
        publication_guard=lambda: False
    )

    result = handler.execute(task, store.get(CREATE_ID))

    assert result.succeeded is False
    assert result.failure_class is FailureClass.POLICY
    assert "emergency-disabled" in (result.message or "")
    assert git.calls == []
    assert github.calls == []


def test_deployment_emergency_marker_is_the_default_publication_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "EMERGENCY_DISABLE"
    marker.write_text("disabled", encoding="utf-8")
    monkeypatch.setenv("AEP_EMERGENCY_DISABLE_FILE", str(marker))
    store, handler, task, _artifacts, git, github = setup_handler()

    result = handler.execute(task, store.get(CREATE_ID))

    assert result.failure_class is FailureClass.POLICY
    assert git.calls == []
    assert github.calls == []


def test_checkout_bound_working_branch_drives_commit_push_and_pull_request(
    tmp_path: Path,
) -> None:
    store, handler, task, _artifacts, git, github = setup_handler()
    workspace = tmp_path / "checkout"
    workspace.mkdir()
    now = datetime(2026, 8, 7, tzinfo=UTC)
    checkout = ExecutionCheckout(
        execution_id=WORKFLOW_ID,
        repository=RepositoryIdentity("github", "octo", "repo"),
        base_revision=REVISION,
        knowledge_revision=REVISION,
        branch="aep/execution/bound-handler-branch",
        workspace_path=workspace.resolve(),
        source_cache_path=(tmp_path / "cache").resolve(),
        state=CheckoutState.READY,
        created_at=now,
        updated_at=now,
    )
    unbound = dict(store.get(CREATE_ID))
    unbound.pop("workingBranch")
    bound = checkout.binding().orchestration().task_execution_input(unbound)

    result = handler.execute(task, bound)

    assert result.succeeded is True
    assert bound["workingBranch"] == checkout.branch
    assert [call.input["branch"] for call in git.calls] == [
        checkout.branch,
        checkout.branch,
    ]
    assert github.calls[0]["head"] == checkout.branch


@pytest.mark.parametrize("effect", ["deny", "require-approval"])
def test_publication_gate_blocks_all_remote_mutations(effect: str) -> None:
    store, handler, task, artifacts, git, github = setup_handler(
        publication_effect=effect
    )

    result = handler.execute(task, store.get(CREATE_ID))

    assert result.failure_class is FailureClass.POLICY
    assert git.calls == []
    assert github.calls == []
    assert artifacts.list_by_task_execution(CREATE_ID) == ()


@pytest.mark.parametrize("effect", ["deny", "require-approval"])
def test_git_capability_gate_blocks_push(effect: str) -> None:
    store, handler, task, _artifacts, git, github = setup_handler(
        capability_effects={"git.push": effect, "github.create_pr": "allow"}
    )

    result = handler.execute(task, store.get(CREATE_ID))

    assert result.failure_class is FailureClass.POLICY
    assert git.calls == []
    assert github.calls == []


@pytest.mark.parametrize("effect", ["deny", "require-approval"])
def test_github_capability_gate_allows_push_but_blocks_pr(effect: str) -> None:
    store, handler, task, _artifacts, git, github = setup_handler(
        capability_effects={"git.push": "allow", "github.create_pr": effect}
    )

    result = handler.execute(task, store.get(CREATE_ID))

    assert result.failure_class is FailureClass.POLICY
    assert [call.input["operation"] for call in git.calls] == [
        "commit_changes",
        "push_branch",
    ]
    assert github.calls == []


def test_unknown_push_failure_is_permanent_and_does_not_call_github() -> None:
    store, handler, task, _artifacts, git, github = setup_handler(
        git_failure=ToolFailureClass.IO
    )

    result = handler.execute(task, store.get(CREATE_ID))

    assert result.failure_class is FailureClass.PERMANENT
    assert [call.input["operation"] for call in git.calls] == [
        "commit_changes",
        "push_branch",
    ]
    assert github.calls == []


def test_pre_mutation_helper_startup_failure_is_recoverable() -> None:
    store, handler, task, _artifacts, git, github = setup_handler(
        git_failure=ToolFailureClass.STARTUP
    )

    result = handler.execute(task, store.get(CREATE_ID))

    assert result.failure_class is FailureClass.RECOVERABLE
    push = store.get(store.get(CREATE_ID)["toolInvocationIds"][-1])
    assert push["output"]["remoteMutationState"] == "NOT_ATTEMPTED"
    assert github.calls == []


def test_provider_failure_is_persisted_and_not_repeated() -> None:
    store, handler, task, artifacts, git, github = setup_handler(
        github_outcome=GitHubProviderError(
            "provider unavailable", retryable=True, request_id="request-failed"
        )
    )

    first = handler.execute(task, store.get(CREATE_ID))
    second = handler.execute(task, store.get(CREATE_ID))

    assert first.failure_class is FailureClass.PERMANENT
    assert second.failure_class is FailureClass.PERMANENT
    assert len(git.calls) == 2
    assert len(github.calls) == 1
    assert artifacts.list_by_task_execution(CREATE_ID) == ()
    github_invocation = store.get(store.get(CREATE_ID)["toolInvocationIds"][-1])
    assert github_invocation["status"] == "FAILED"
    assert github_invocation["failure"]["retryable"] is False


def test_partial_success_reuses_provider_result_and_finishes_artifact() -> None:
    store, handler, task, artifacts, git, github = setup_handler(
        fail_description_once=True
    )

    first = handler.execute(task, store.get(CREATE_ID))
    second = handler.execute(task, store.get(CREATE_ID))

    assert first.failure_class is FailureClass.RECOVERABLE
    assert second.succeeded is True
    assert len(git.calls) == 2
    assert len(github.calls) == 1
    assert len(artifacts.list_by_task_execution(CREATE_ID)) == 1


def test_new_scheduler_attempt_reconciles_publication_without_duplicate_pr() -> None:
    store, handler, task, artifacts, git, github = setup_handler(
        fail_description_once=True
    )

    first = handler.execute(task, store.get(CREATE_ID))
    store.update_status(
        CREATE_ID,
        "FAILED",
        expected_status="RUNNING",
        updated_at=TIMESTAMP,
        changes={
            "failure": {
                "class": "RECOVERABLE",
                "message": first.message,
                "retryable": True,
            }
        },
    )
    store.create(create_execution(CREATE_RETRY_ID, attempt=2), deterministic_key="retry")

    second = handler.execute(task, store.get(CREATE_RETRY_ID))

    assert first.failure_class is FailureClass.RECOVERABLE
    assert second.succeeded is True
    assert len(github.calls) == 1
    assert [call.input["operation"] for call in git.calls] == [
        "commit_changes",
        "push_branch",
        "commit_changes",
        "push_branch",
    ]
    assert len(artifacts.list_by_task_execution(CREATE_RETRY_ID)) == 1
    assert artifacts.list_by_task_execution(CREATE_ID) == ()
    retry_execution = store.get(CREATE_RETRY_ID)
    for invocation_id in retry_execution["toolInvocationIds"]:
        assert store.get(invocation_id)["taskExecutionId"] == CREATE_RETRY_ID


def test_retry_after_unpersisted_task_success_keeps_attempt_ownership_honest() -> None:
    store, handler, task, artifacts, _git, github = setup_handler()

    first = handler.execute(task, store.get(CREATE_ID))
    store.update_status(
        CREATE_ID,
        "FAILED",
        expected_status="RUNNING",
        updated_at=TIMESTAMP,
        changes={
            "failure": {
                "class": "RECOVERABLE",
                "message": "scheduler did not persist handler success",
                "retryable": True,
            }
        },
    )
    store.create(create_execution(CREATE_RETRY_ID, attempt=2), deterministic_key="retry")

    second = handler.execute(task, store.get(CREATE_RETRY_ID))

    assert first.succeeded and second.succeeded
    assert len(github.calls) == 1
    first_artifacts = artifacts.list_by_task_execution(CREATE_ID)
    retry_artifacts = artifacts.list_by_task_execution(CREATE_RETRY_ID)
    assert len(first_artifacts) == len(retry_artifacts) == 1
    assert first_artifacts[0]["id"] != retry_artifacts[0]["id"]
    retry_execution = store.get(CREATE_RETRY_ID)
    for invocation_id in retry_execution["toolInvocationIds"]:
        invocation = store.get(invocation_id)
        assert invocation["taskExecutionId"] == CREATE_RETRY_ID
    reconciled = store.get(retry_execution["toolInvocationIds"][-1])
    assert reconciled["reconciledFromToolInvocationId"] == (
        store.get(CREATE_ID)["toolInvocationIds"][-1]
    )
    assert retry_artifacts[0]["taskExecutionId"] == CREATE_RETRY_ID


def test_successful_retry_is_idempotent() -> None:
    store, handler, task, artifacts, git, github = setup_handler()

    first = handler.execute(task, store.get(CREATE_ID))
    second = handler.execute(task, store.get(CREATE_ID))

    assert first.succeeded and second.succeeded
    assert len(git.calls) == 2
    assert len(github.calls) == 1
    assert len(artifacts.list_by_task_execution(CREATE_ID)) == 1


def test_create_pull_request_task_fixture_matches_resource_contract() -> None:
    schema_root = ROOT / "schemas" / "resources" / "v1"
    schemas = [
        json.loads((schema_root / name).read_text(encoding="utf-8"))
        for name in ("resource-definitions.schema.json", "task.schema.json")
    ]
    registry = Registry()
    for schema in schemas:
        registry = registry.with_resource(
            schema["$id"],
            SchemaResource.from_contents(
                schema, default_specification=DRAFT202012
            ),
        )
    fixture = json.loads(
        (
            ROOT
            / "fixtures"
            / "resources"
            / "valid"
            / "create-pull-request-task.json"
        ).read_text(encoding="utf-8")
    )

    assert list(Draft202012Validator(schemas[-1], registry=registry).iter_errors(fixture)) == []


def setup_handler(
    *,
    publication_effect: str = "allow",
    capability_effects: Mapping[str, str] | None = None,
    git_failure: ToolFailureClass | None = None,
    github_outcome: Mapping[str, Any] | Exception | None = None,
    fail_description_once: bool = False,
    publication_guard=None,
    repository_resources: bool = False,
):
    store, acceptance_handler, acceptance_task, base_artifacts = setup_acceptance()
    acceptance_result = acceptance_handler.execute(
        acceptance_task, store.get(ACCEPTANCE_ID)
    )
    assert acceptance_result.succeeded
    store.update_status(
        ACCEPTANCE_ID,
        "SUCCEEDED",
        expected_status="RUNNING",
        updated_at=TIMESTAMP,
    )

    if fail_description_once:
        artifacts = FailDescriptionOnceStore(base_artifacts)
        artifacts.fail_description = True
    else:
        artifacts = base_artifacts

    capability_effects = capability_effects or {
        "git.push": "allow",
        "github.create_pr": "allow",
    }
    workspace = resource(
        "Workspace",
        "workspace",
        {
            "repository": {
                "owner": "octo",
                "name": "repo",
                "defaultBranch": "main",
            }
        },
    )
    git_resource = resource(
        "Tool",
        "git",
        {
            "category": "execution",
            "capabilities": ["git.push"],
            "inputSchema": deepcopy(dict(GIT_INPUT_SCHEMA)),
            "outputSchema": deepcopy(dict(GIT_OUTPUT_SCHEMA)),
        },
    )
    github_resource = resource(
        "Tool",
        "github",
        {
            "category": "external-service",
            "capabilities": ["github.create_pr"],
            "inputSchema": deepcopy(dict(GITHUB_INPUT_SCHEMA)),
            "outputSchema": deepcopy(dict(GITHUB_OUTPUT_SCHEMA)),
        },
    )
    publication_policy = resource(
        "Policy",
        "publication",
        {
            "type": "publication",
            "rules": [{"effect": publication_effect, "reason": "test publication"}],
        },
    )
    capability_policy = resource(
        "Policy",
        "publication-capabilities",
        {
            "type": "pre-execution-capability",
            "rules": [
                {
                    "effect": effect,
                    "capabilities": [capability],
                    "reason": f"test {capability}",
                }
                for capability, effect in capability_effects.items()
            ],
        },
    )
    task = resource(
        "Task",
        "create-pull-request",
        {
            "objective": "Publish accepted work.",
            "outputs": {"type": "object"},
            "policies": [
                ref("Policy", "publication"),
                ref("Policy", "publication-capabilities"),
            ],
            "publication": {
                "gitToolRef": ref("Tool", "git"),
                "githubToolRef": ref("Tool", "github"),
                "timeoutMs": 5000,
            },
        },
    )
    resources = ResourceCollection(
        workspace,
        (
            workspace,
            task,
            git_resource,
            github_resource,
            publication_policy,
            capability_policy,
        ),
    )
    if repository_resources:
        resources = ResourceLoader(ROOT).load()
        task = resources.get(ResourceRef("Task", "create-pull-request", "1.2.0"))
        assert task is not None
    repository_spec = resources.workspace.data["spec"]["repository"]
    repository = f"{repository_spec['owner']}/{repository_spec['name']}"
    store.create(
        create_execution(CREATE_ID, attempt=1, task_ref=dict(task.data["metadata"]) | {"kind": "Task"}),
        deterministic_key="create-pr-task",
    )
    event = {
        "repository": {"full_name": repository},
        "issue": {"number": 34, "title": "Add publication"},
    }
    store.update_status(
        WORKFLOW_ID,
        "RUNNING",
        expected_status="RUNNING",
        updated_at=TIMESTAMP,
        changes={"eventId": "event-34"},
    )
    git = FakeGitTool(store, failure=git_failure, repository=repository)
    github = FakeGitHubClient(github_outcome)
    handler = CreatePullRequestTaskHandler(
        resources=resources,
        runtime_store=store,
        artifact_store=artifacts,
        git_tool=git,
        github_adapter=GitHubToolAdapter(github),
        event_resolver=lambda event_id: event if event_id == "event-34" else None,
        clock=lambda: TIMESTAMP,
        publication_guard=publication_guard,
    )
    return store, handler, task, artifacts, git, github


def create_execution(
    execution_id: str, *, attempt: int, task_ref: Mapping[str, str] | None = None
) -> dict[str, Any]:
    selected_task_ref = dict(task_ref or ref("Task", "create-pull-request"))
    selected_task_ref.pop("description", None)
    return {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "TaskExecution",
        "id": execution_id,
        "traceId": TRACE_ID,
        "createdAt": TIMESTAMP,
        "updatedAt": TIMESTAMP,
        "provenance": {
            "actor": "workflow-scheduler",
            "workflowExecutionId": WORKFLOW_ID,
            "repositoryRevision": REVISION,
            "resourceRefs": [selected_task_ref],
        },
        "workflowExecutionId": WORKFLOW_ID,
        "taskRef": selected_task_ref,
        "attempt": attempt,
        "status": "RUNNING",
        "workingBranch": "agent/aep-034",
        "dependencyTaskExecutionIds": [ACCEPTANCE_ID],
    }


def _result_from_record(record: Mapping[str, Any]) -> ToolResult:
    failure_class = record.get("failureClass")
    return ToolResult(
        status=ToolResultStatus(record["resultStatus"]),
        output=record.get("output"),
        logs_ref=None,
        metrics=ToolMetrics(duration_ms=1),
        started_at=TIMESTAMP,
        completed_at=TIMESTAMP,
        failure_class=ToolFailureClass(failure_class) if failure_class else None,
        failure_message=(
            record.get("failure", {}).get("message") if failure_class else None
        ),
    )
