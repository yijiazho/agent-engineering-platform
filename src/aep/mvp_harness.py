"""Deterministic, credential-free harness for the ADR-003 MVP control loop."""

from __future__ import annotations

import json
from hashlib import sha256
import os
import shutil
import subprocess
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from aep.analyze_issue import AnalyzeIssueTaskHandler
from aep.build_implementation_plan import BuildImplementationPlanTaskHandler
from aep.context_builder import ContextBuilder
from aep.create_pull_request import CreatePullRequestTaskHandler
from aep.docker_validation_tool import (
    DockerCommandResult,
    DockerExecution,
    DockerExecutionResult,
    DockerExecutor,
    DockerRunConfiguration,
    DockerValidationAdapter,
    DockerValidationTool,
)
from aep.evaluate_acceptance import EvaluateAcceptanceTaskHandler
from aep.filesystem_tool import FilesystemTool
from aep.generate_patch import GeneratePatchTaskHandler
from aep.generated_artifact_store import InMemoryGeneratedArtifactStore
from aep.git_tool import (
    GitSandbox,
    GitSandboxCommandResult,
    GitSandboxTimeout,
    GitTool,
    GitToolAdapter,
    InMemoryGitCommandLogStore,
)
from aep.github_events import EventDeduplicator, normalize_github_issue_created
from aep.github_tool import GitHubToolAdapter
from aep.execution_checkout import RepositoryIdentity
from aep.model_invocation import FakeModelAdapter, ModelResponse, ModelUsage
from aep.repository_knowledge import (
    InMemoryRepositoryKnowledgeProvider,
    RepositoryFile,
    RepositoryKnowledgeSnapshot,
    SourceProvenance,
)
from aep.resource_loader import Resource, ResourceCollection, ResourceLoader, ResourceRef
from aep.run_validation import RunValidationTaskHandler
from aep.runtime_store import InMemoryRuntimeObjectStore, RuntimeObject
from aep.task_dag import resolve_task_dag
from aep.workflow_execution import WorkflowExecutionCreator, _runtime_validator
from aep.workflow_resolver import resolve_workflow_for_event
from aep.workflow_scheduler import TaskExecutionResult, WorkflowScheduler


TIMESTAMP = "2026-08-07T12:00:00Z"
KNOWLEDGE_VERSION = "fixture-knowledge-v1"
DELIVERY_ID = "aep-037-fixture-delivery"
BRANCH = "agent/aep-037-fixture"
PR_URL = "https://github.com/octo-org/octo-repo/pull/137"


@dataclass(frozen=True)
class HarnessResult:
    resources: ResourceCollection
    normalized_event: Mapping[str, Any]
    duplicate_was_rejected: bool
    workflow_execution: Mapping[str, Any]
    runtime_history: tuple[Mapping[str, Any], ...]
    task_names: tuple[str, ...]
    generated_artifacts: tuple[Mapping[str, Any], ...]
    evaluation_results: tuple[Mapping[str, Any], ...]
    policy_decisions: tuple[Mapping[str, Any], ...]
    model_request_count: int
    github_request_count: int
    git_operations: tuple[str, ...]
    pull_request_url: str | None


class _LocalGitSandbox(GitSandbox):
    disabled_hooks_path = os.devnull
    null_device_path = os.devnull

    def run(
        self,
        *,
        repository: Path,
        arguments: Any,
        environment: Mapping[str, str],
        timeout_ms: int,
        stdin: bytes | None = None,
    ) -> GitSandboxCommandResult:
        process_environment = dict(environment)
        if os.name == "nt":
            process_environment["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
        try:
            completed = subprocess.run(
                ("git", *arguments),
                cwd=repository,
                env=process_environment,
                input=stdin,
                stdin=subprocess.DEVNULL if stdin is None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout_ms / 1000,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise GitSandboxTimeout(
                stdout=error.stdout or b"", stderr=error.stderr or b""
            ) from error
        return GitSandboxCommandResult(
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


class _FakeDockerExecution(DockerExecution):
    def __init__(self, result: DockerExecutionResult) -> None:
        self.result = result

    def wait(self, timeout_ms: int) -> DockerExecutionResult:
        return self.result

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass

    def cleanup(self) -> None:
        pass


class _FakeDockerExecutor(DockerExecutor):
    def __init__(self) -> None:
        self.configurations: list[DockerRunConfiguration] = []

    def start(self, configuration: DockerRunConfiguration) -> DockerExecution:
        self.configurations.append(configuration)
        commands = tuple(
            DockerCommandResult(
                argv=tuple(item),
                stdout="ok\n",
                stderr="",
                exit_code=0,
                duration_ms=1,
                logs_ref="sha256:" + str(index + 1) * 64,
            )
            for index, item in enumerate(configuration.commands)
        )
        return _FakeDockerExecution(
            DockerExecutionResult(
                commands=commands,
                logs_ref="sha256:" + "d" * 64,
                started_at=TIMESTAMP,
                completed_at=TIMESTAMP,
                readiness=tuple(
                    {
                        "argv": list(argv),
                        "versionPattern": pattern,
                        "output": "Python 3.12.9" if argv[0] == "python" else "git version 2.43.0",
                        "logsRef": "sha256:" + str(index + 3) * 64,
                    }
                    for index, (argv, pattern) in enumerate(configuration.required_executables)
                ),
            )
        )

    def cleanup_startup(self) -> None:
        pass


class _FakeOperation:
    request_id = "fake-github-137"

    def wait(self, timeout_ms: int) -> Mapping[str, Any]:
        return {"number": 137, "url": PR_URL, "requestId": self.request_id}

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass

    def cleanup(self) -> None:
        pass


class _FakeGitHubClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def start_read_issue(self, repository: str, issue_number: int) -> None:
        raise AssertionError("CreatePullRequest must consume the normalized event")

    def start_create_pull_request(
        self,
        repository: str,
        *,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> _FakeOperation:
        self.calls.append(
            {"repository": repository, "head": head, "base": base, "title": title, "body": body}
        )
        return _FakeOperation()


class _ProductionTaskExecutor:
    def __init__(
        self,
        handlers: Mapping[str, Any],
        store: InMemoryRuntimeObjectStore,
        workspace: Path,
    ) -> None:
        self.handlers = handlers
        self.store = store
        self.workspace = workspace
        self.executed: list[str] = []

    def execute(self, task: Resource, task_execution: RuntimeObject) -> TaskExecutionResult:
        self.executed.append(task.name)
        changes: dict[str, Any] = {}
        if task.name == "run-validation":
            changes["workspacePath"] = str(self.workspace)
        if task.name == "create-pull-request":
            changes["workingBranch"] = BRANCH
        if changes:
            task_execution = self.store.update_status(
                str(task_execution["id"]),
                "RUNNING",
                expected_status="RUNNING",
                updated_at=TIMESTAMP,
                changes=changes,
            )
        return self.handlers[task.name].execute(task, task_execution)


def run_mvp_harness(fixture_root: Path | str, *, block_publication: bool = False) -> HarnessResult:
    fixture = Path(fixture_root)
    loaded_resources = ResourceLoader(fixture).load()
    resources = _publication_variant(loaded_resources, block_publication)
    payload = json.loads((fixture / "issue-created.json").read_text(encoding="utf-8"))
    event = normalize_github_issue_created(
        payload,
        delivery_id=DELIVERY_ID,
        received_at=datetime(2026, 8, 7, 12, tzinfo=UTC),
    )

    with TemporaryDirectory(prefix="aep-037-") as temporary:
        workspace, evaluation_workspace, revision = _repositories(
            fixture, Path(temporary)
        )
        store = InMemoryRuntimeObjectStore()
        deduplicator = EventDeduplicator(store)
        first = deduplicator.accept(event)
        duplicate = deduplicator.accept(event)
        resolution = resolve_workflow_for_event(first.event, resources)
        if resolution.workflow_ref is None:
            raise AssertionError("fixture event did not resolve a Workflow")
        workflow = resources.get(resolution.workflow_ref)
        event_resource = resources.get(
            ResourceRef("Event", "github-issue-created", "1.0.0")
        )
        if workflow is None or event_resource is None:
            raise AssertionError("fixture Workflow or Event Resource is missing")
        execution = WorkflowExecutionCreator(store).create(
            event=first.event,
            workflow=workflow,
            event_resource=event_resource,
            repository_revision=revision,
            knowledge_graph_version=KNOWLEDGE_VERSION,
            timestamp=TIMESTAMP,
        )
        artifacts = InMemoryGeneratedArtifactStore(runtime_store=store)
        context_builder = ContextBuilder(
            repository_knowledge=_repository_provider(revision),
            artifact_store=artifacts,
            runtime_store=store,
        )
        model = FakeModelAdapter(
            [
                ModelResponse(
                    output={
                        "requestedChange": "Update the fixture value.",
                        "acceptanceCriteria": ["The value is updated and tests pass."],
                        "risks": ["The patch could exceed its plan."],
                        "likelyRepositoryAreas": ["src/app.py"],
                    },
                    usage=ModelUsage(10, 10),
                    latency_ms=1,
                ),
                ModelResponse(
                    output={
                        "intendedFiles": ["src/app.py"],
                        "tests": ["python -m pytest"],
                        "assumptions": ["The checkout is revision-bound."],
                        "risks": ["Validation may fail."],
                        "implementationSteps": ["Update src/app.py.", "Run tests."],
                        "acceptanceCriteriaClassifications": [{
                            "criterion": "The value is updated and tests pass.",
                            "classification": "REQUIRED_INSERTION",
                            "requiredInsertion": {"path": "src/app.py", "value": "value = 2"},
                        }],
                        "requiredInsertions": [
                            {"path": "src/app.py", "value": "value = 2"}
                        ],
                        "unsupportedAcceptanceCriteria": [],
                    },
                    usage=ModelUsage(10, 10),
                    latency_ms=1,
                ),
                ModelResponse(
                    output={"changes": [{
                        "path": "src/app.py",
                        "content": "value = 2\n",
                        "preimageSha256": sha256(
                            (workspace / "src" / "app.py").read_bytes()
                        ).hexdigest(),
                    }]},
                    usage=ModelUsage(10, 10),
                    latency_ms=1,
                ),
            ]
        )
        common = {
            "resources": resources,
            "runtime_store": store,
            "context_builder": context_builder,
            "artifact_store": artifacts,
            "model_adapter": model,
            "event_resolver": lambda event_id: first.event
            if event_id == first.event["id"]
            else None,
            "clock": lambda: TIMESTAMP,
        }
        analyze = AnalyzeIssueTaskHandler(**common)
        plan_handler = BuildImplementationPlanTaskHandler(**common)
        workspace_git_tool = GitTool(_git_adapter(workspace, revision), store)
        generate = GeneratePatchTaskHandler(
            **common,
            filesystem_tool=FilesystemTool(workspace, store),
            workspace_git_tool=workspace_git_tool,
            evaluation_git_tool=GitTool(
                _git_adapter(evaluation_workspace, revision), store
            ),
            authorize_filesystem=lambda _request: True,
            authorize_git=lambda _request: True,
            working_branch=BRANCH,
        )
        docker_executor = _FakeDockerExecutor()
        validation = RunValidationTaskHandler(
            resources=resources,
            runtime_store=store,
            artifact_store=artifacts,
            docker_tool=DockerValidationTool(
                DockerValidationAdapter(docker_executor, workspace), store
            ),
            authorize_docker=lambda _request: True,
            clock=lambda: TIMESTAMP,
        )
        acceptance = EvaluateAcceptanceTaskHandler(
            resources=resources,
            runtime_store=store,
            artifact_store=artifacts,
            clock=lambda: TIMESTAMP,
        )
        github = _FakeGitHubClient()
        publication = CreatePullRequestTaskHandler(
            resources=resources,
            runtime_store=store,
            artifact_store=artifacts,
            git_tool=workspace_git_tool,
            github_adapter=GitHubToolAdapter(github),
            event_resolver=lambda event_id: first.event
            if event_id == first.event["id"]
            else None,
            clock=lambda: TIMESTAMP,
        )
        executor = _ProductionTaskExecutor(
            {
                "analyze-issue": analyze,
                "build-implementation-plan": plan_handler,
                "generate-patch": generate,
                "run-validation": validation,
                "evaluate-acceptance": acceptance,
                "create-pull-request": publication,
            },
            store,
            workspace,
        )
        scheduler = WorkflowScheduler(store, executor, clock=lambda: TIMESTAMP)
        task_plan = resolve_task_dag(workflow, resources)
        for _ in task_plan.nodes:
            scheduler.reconcile(
                task_plan, store.get(str(execution["id"])) or execution
            )

        task_records = tuple(
            item
            for item in store.list_by_workflow_execution(str(execution["id"]))
            if item.get("kind") == "TaskExecution"
        )
        failed = any(item.get("status") == "FAILED" for item in task_records)
        final_execution = store.update_status(
            str(execution["id"]),
            "FAILED" if failed else "SUCCEEDED",
            expected_status="RUNNING",
            updated_at=TIMESTAMP,
        )
        history = store.list_by_workflow_execution(str(execution["id"]))
        _validate_runtime_history((final_execution, *history))
        artifact_records = tuple(
            item for item in history if item.get("kind") == "GeneratedArtifact"
        )
        pr_artifact = next(
            (
                item
                for item in artifact_records
                if item.get("artifactType") == "PULL_REQUEST_DESCRIPTION"
            ),
            None,
        )
        return HarnessResult(
            resources=resources,
            normalized_event=first.event,
            duplicate_was_rejected=not duplicate.accepted,
            workflow_execution=final_execution,
            runtime_history=history,
            task_names=tuple(executor.executed),
            generated_artifacts=artifact_records,
            evaluation_results=tuple(
                item for item in history if item.get("kind") == "EvaluationResult"
            ),
            policy_decisions=tuple(
                item for item in history if item.get("kind") == "PolicyDecision"
            ),
            model_request_count=len(model.requests),
            github_request_count=len(github.calls),
            git_operations=tuple(
                str(item.get("input", {}).get("operation"))
                for item in history
                if item.get("kind") == "ToolInvocation"
                and item.get("toolRef", {}).get("name") == "git"
            ),
            pull_request_url=(
                str(pr_artifact["pullRequestUrl"]) if pr_artifact is not None else None
            ),
        )


def _repositories(fixture: Path, root: Path) -> tuple[Path, Path, str]:
    source = root / "source"
    shutil.copytree(fixture, source)
    _git(source, "init")
    _git(source, "config", "user.name", "AEP Harness")
    _git(source, "config", "user.email", "aep@example.test")
    _git(source, "config", "core.autocrlf", "false")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "fixture")
    revision = _git(source, "rev-parse", "HEAD")
    workspace = root / "workspace"
    evaluation = root / "evaluation"
    _git(root, "-c", "core.autocrlf=false", "clone", str(source), str(workspace))
    _git(root, "-c", "core.autocrlf=false", "clone", str(source), str(evaluation))
    _git(workspace, "switch", "-c", BRANCH)
    _git(evaluation, "switch", "-c", BRANCH)
    return workspace, evaluation, revision


def _git(root: Path, *arguments: str) -> str:
    environment = dict(os.environ)
    environment.update(
        {
            "GIT_AUTHOR_DATE": TIMESTAMP,
            "GIT_COMMITTER_DATE": TIMESTAMP,
        }
    )
    completed = subprocess.run(
        ("git", *arguments),
        cwd=root,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return completed.stdout.decode("utf-8").strip()


def _git_adapter(root: Path, revision: str) -> GitToolAdapter:
    return GitToolAdapter(
        repository=root,
        repository_id=RepositoryIdentity("github", "octo-org", "octo-repo").canonical,
        expected_revision=revision,
        working_branch=BRANCH,
        log_store=InMemoryGitCommandLogStore(),
        sandbox=_LocalGitSandbox(),
    )


def _repository_provider(revision: str) -> InMemoryRepositoryKnowledgeProvider:
    provenance = SourceProvenance(
        source_path="src/app.py",
        repository_revision=revision,
        scanned_at=TIMESTAMP,
        scanner_version="mvp-scanner/1.0.0",
    )
    return InMemoryRepositoryKnowledgeProvider(
        RepositoryKnowledgeSnapshot(
            api_version="aep.dev/repository-knowledge/v1",
            snapshot_version=KNOWLEDGE_VERSION,
            repository_revision=revision,
            created_at=TIMESTAMP,
            scanner_version="mvp-scanner/1.0.0",
            files=(
                RepositoryFile(
                    path="src/app.py",
                    language="Python",
                    is_documentation=False,
                    provenance=provenance,
                ),
            ),
            documentation=(),
            dependency_manifests=(),
            test_command_hints=(),
        )
    )


def _publication_variant(
    resources: ResourceCollection, blocked: bool
) -> ResourceCollection:
    if not blocked:
        return resources
    values: list[Resource] = []
    for resource in resources.resources:
        if resource.kind == "Policy" and resource.name == "publication":
            data = deepcopy(resource.data)
            data["spec"]["rules"][0]["effect"] = "deny"
            data["spec"]["rules"][0]["reason"] = "Fixture blocks publication."
            resource = Resource(resource.ref, resource.path, data, resource.references)
        values.append(resource)
    return ResourceCollection(resources.workspace, tuple(values))


def _validate_runtime_history(values: tuple[Mapping[str, Any], ...]) -> None:
    seen: set[str] = set()
    for value in values:
        object_id = str(value["id"])
        if object_id in seen:
            continue
        seen.add(object_id)
        schema_name = f"{str(value['kind']).lower()}.schema.json"
        errors = sorted(
            _runtime_validator(schema_name).iter_errors(dict(value)),
            key=lambda error: (list(error.absolute_path), error.message),
        )
        if errors:
            error = errors[0]
            path = "$" + "".join(f".{part}" for part in error.absolute_path)
            raise AssertionError(
                f"invalid persisted {value['kind']} {object_id} at {path}: {error.message}"
            )
