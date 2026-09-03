"""Durable repository-bound reconciliation worker for the dogfood deployment."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
import logging
from pathlib import Path
import subprocess
from threading import Event, Thread
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from aep.analyze_issue import AnalyzeIssueTaskHandler
from aep.build_implementation_plan import BuildImplementationPlanTaskHandler
from aep.capability_policy import ApplicablePolicy, PolicyScope, PreExecutionCapabilityPolicy
from aep.context_builder import ContextBuilder
from aep.create_pull_request import CreatePullRequestTaskHandler
from aep.docker_validation_tool import (
    ContentAddressedDockerLogStore,
    DockerCliExecutor,
    DockerExecutor,
    DockerRunConfiguration,
    DockerValidationTool,
    SubprocessDockerProcessBoundary,
)
from aep.evaluate_acceptance import EvaluateAcceptanceTaskHandler
from aep.execution_checkout import (
    CheckoutProvisionError,
    CheckoutRequest,
    ExecutionCheckoutManager,
    GitRepositorySource,
    RepositoryIdentity,
    local_checkout_registry,
)
from aep.filesystem_tool import FilesystemTool
from aep.generate_patch import GeneratePatchTaskHandler
from aep.generated_artifact_store import (
    FilesystemContentAddressedStore,
    InMemoryGeneratedArtifactStore,
)
from aep.git_tool import (
    FilesystemGitCommandLogStore,
    GitTool,
    SubprocessGitSandbox,
)
from aep.github_app_provider import github_app_provider_from_environment
from aep.github_tool import GitHubToolAdapter
from aep.local_service import verify_resource_checkout
from aep.openai_model_provider import openai_model_adapter_from_environment
from aep.repository_knowledge import MvpRepositoryScanner
from aep.resource_loader import Resource, ResourceLoader, ResourceRef
from aep.run_validation import RunValidationTaskHandler
from aep.runtime_store import DurableJsonRuntimeObjectStore, RuntimeObject
from aep.task_dag import resolve_task_dag
from aep.webhook_dispatch import SQLiteReconciliationDispatcher
from aep.workflow_execution import WorkflowExecutionCreator
from aep.workflow_resolver import resolve_workflow_for_event
from aep.workflow_scheduler import TaskExecutionResult, WorkflowScheduler


class DogfoodReconciliationError(RuntimeError):
    """Safe terminal reconciliation failure suitable for persisted evidence."""


class _HostPathDockerExecutor(DockerExecutor):
    """Translate the container-visible state root for the host Docker daemon."""

    def __init__(
        self,
        delegate: DockerExecutor,
        *,
        container_state_root: Path,
        host_state_root: Path,
    ) -> None:
        self._delegate = delegate
        self._container_root = container_state_root.resolve()
        self._host_root = host_state_root

    def start(self, configuration: DockerRunConfiguration):
        visible = Path(configuration.workspace_mount.host_path).resolve()
        try:
            relative = visible.relative_to(self._container_root)
        except ValueError:
            raise DogfoodReconciliationError(
                "Docker workspace is outside the durable state root"
            ) from None
        mount = replace(
            configuration.workspace_mount,
            host_path=str(self._host_root / relative),
        )
        return self._delegate.start(
            replace(configuration, workspace_mount=mount)
        )

    def cleanup_startup(self) -> None:
        self._delegate.cleanup_startup()


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class _TaskExecutor:
    def __init__(
        self,
        handlers: Mapping[str, Any],
        store: DurableJsonRuntimeObjectStore,
        workspace: Path,
        branch: str,
    ) -> None:
        self._handlers = handlers
        self._store = store
        self._workspace = workspace
        self._branch = branch

    def execute(
        self, task: Resource, task_execution: RuntimeObject
    ) -> TaskExecutionResult:
        changes: dict[str, Any] = {}
        if task.name == "run-validation":
            changes["workspacePath"] = str(self._workspace)
        if task.name == "create-pull-request":
            changes["workingBranch"] = self._branch
        if changes:
            task_execution = self._store.update_status(
                str(task_execution["id"]),
                "RUNNING",
                expected_status="RUNNING",
                changes=changes,
            )
        return self._handlers[task.name].execute(task, task_execution)


class DogfoodWorkflowRunner:
    """Compose the existing six production handlers for one durable Event."""

    def __init__(self, environment: Mapping[str, str]) -> None:
        self._environment = dict(environment)
        self._state_root = Path(self._required("AEP_STATE_ROOT")).resolve()
        repository_root = Path(self._required("AEP_REPOSITORY_ROOT"))
        verify_resource_checkout(
            repository_root,
            self._required("AEP_RESOURCE_REVISION"),
            require_detached=True,
            autocrlf=(
                self._environment.get("AEP_RESOURCE_GIT_AUTOCRLF", "")
                .strip()
                .lower()
                or None
            ),
        )
        self._resources = ResourceLoader(
            repository_root,
            schema_root=Path(self._required("AEP_RESOURCE_SCHEMA_ROOT")),
        ).load()
        self._store = DurableJsonRuntimeObjectStore(
            self._state_root / "runtime" / "objects.json"
        )
        self._artifacts = InMemoryGeneratedArtifactStore(
            runtime_store=self._store,
            content_store=FilesystemContentAddressedStore(
                self._state_root / "artifacts" / "objects"
            ),
        )
        self._github = github_app_provider_from_environment(self._environment)
        self._model = openai_model_adapter_from_environment(
            "openai", environ=self._environment
        )

    def run(self, request: Mapping[str, Any]) -> str:
        event = request.get("event")
        if not isinstance(event, Mapping):
            raise DogfoodReconciliationError("reconciliation Event is missing")
        resolution = resolve_workflow_for_event(event, self._resources)
        if resolution.workflow_ref is None:
            raise DogfoodReconciliationError("Event did not resolve a Workflow")
        workflow = self._resources.get(resolution.workflow_ref)
        if workflow is None:
            raise DogfoodReconciliationError("resolved Workflow is unavailable")
        event_resource = self._event_resource(workflow)
        execution_id = _workflow_execution_id(str(event["id"]), workflow)
        revision = _reconciliation_revision(
            self._store,
            execution_id,
            self._github.client.resolve_default_branch_revision,
        )
        if revision is None:
            return execution_id
        checkout_manager = ExecutionCheckoutManager(
            source=GitRepositorySource(
                "https://github.com/yijiazho/agent-engineering-platform.git"
            ),
            source_cache_root=self._state_root / "repository-cache",
            worktree_root=self._state_root / "execution-worktrees",
            registry=local_checkout_registry(self._state_root),
            credential_provider=self._github.credentials,
        )
        checkout, orchestration = checkout_manager.provision_orchestration(
            CheckoutRequest(
                execution_id=execution_id,
                repository=RepositoryIdentity(
                    "github",
                    self._required("AEP_REPOSITORY_OWNER"),
                    self._required("AEP_REPOSITORY_NAME"),
                ),
                default_branch=self._required("AEP_REPOSITORY_DEFAULT_BRANCH"),
                base_revision=revision,
                knowledge_revision=revision,
            )
        )
        snapshot, knowledge = orchestration.repository_context(MvpRepositoryScanner())
        execution = WorkflowExecutionCreator(self._store).create(
            event=event,
            workflow=workflow,
            event_resource=event_resource,
            repository_revision=revision,
            knowledge_graph_version=snapshot.snapshot_version,
            timestamp=_timestamp(),
        )
        if str(execution["id"]) != execution_id:
            raise DogfoodReconciliationError("WorkflowExecution identity drifted")
        if execution.get("status") in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            return execution_id
        context_builder = ContextBuilder(
            repository_knowledge=knowledge,
            artifact_store=self._artifacts,
            runtime_store=self._store,
            repository_file_reader=_pinned_workspace_reader(
                checkout.workspace_path, revision
            ),
        )
        git_logs = FilesystemGitCommandLogStore(self._state_root / "git-logs")
        git_sandbox = SubprocessGitSandbox(self._state_root / "disabled-git-hooks")
        workspace_git = GitTool(
            orchestration.git_adapter(
                log_store=git_logs,
                sandbox=git_sandbox,
                credential_provider=self._github.credentials,
            ),
            self._store,
        )
        evaluation_root = self._evaluation_checkout(checkout)
        # Patch applicability needs a clean peer checkout, not the modified
        # execution worktree. Rebind only that read-only evaluator.
        evaluation_git = GitTool(
            _git_adapter_for_peer(
                evaluation_root,
                checkout,
                log_store=git_logs,
                sandbox=git_sandbox,
            ),
            self._store,
        )
        common = {
            "resources": self._resources,
            "runtime_store": self._store,
            "context_builder": context_builder,
            "artifact_store": self._artifacts,
            "model_adapter": self._model,
            "event_resolver": lambda event_id: event
            if event_id == event.get("id")
            else None,
            "clock": _timestamp,
        }
        generate_task = self._resource("Task", "generate-patch")
        validation_task = self._resource("Task", "run-validation")
        handlers = {
            "analyze-issue": AnalyzeIssueTaskHandler(**common),
            "build-implementation-plan": BuildImplementationPlanTaskHandler(**common),
            "generate-patch": GeneratePatchTaskHandler(
                **common,
                filesystem_tool=FilesystemTool(checkout.workspace_path, self._store),
                workspace_git_tool=workspace_git,
                evaluation_git_tool=evaluation_git,
                authorize_filesystem=self._authorization(generate_task, revision),
                authorize_git=self._authorization(generate_task, revision),
                working_branch=checkout.branch,
            ),
            "run-validation": RunValidationTaskHandler(
                resources=self._resources,
                runtime_store=self._store,
                artifact_store=self._artifacts,
                docker_tool=DockerValidationTool(
                    orchestration.docker_adapter(
                        _HostPathDockerExecutor(
                            DockerCliExecutor(
                                SubprocessDockerProcessBoundary(),
                                ContentAddressedDockerLogStore(
                                    self._state_root / "docker-logs"
                                ),
                            ),
                            container_state_root=self._state_root,
                            host_state_root=Path(
                                self._required("AEP_DOCKER_HOST_STATE_DIRECTORY")
                            ),
                        )
                    ),
                    self._store,
                ),
                authorize_docker=self._authorization(validation_task, revision),
                clock=_timestamp,
            ),
            "evaluate-acceptance": EvaluateAcceptanceTaskHandler(
                resources=self._resources,
                runtime_store=self._store,
                artifact_store=self._artifacts,
                clock=_timestamp,
            ),
            "create-pull-request": CreatePullRequestTaskHandler(
                resources=self._resources,
                runtime_store=self._store,
                artifact_store=self._artifacts,
                git_tool=workspace_git,
                github_adapter=GitHubToolAdapter(self._github.client),
                event_resolver=lambda event_id: event
                if event_id == event.get("id")
                else None,
                clock=_timestamp,
            ),
        }
        plan = resolve_task_dag(workflow, self._resources)
        scheduler = WorkflowScheduler(
            self._store,
            _TaskExecutor(
                handlers,
                self._store,
                checkout.workspace_path,
                checkout.branch,
            ),
            max_attempts=2,
            clock=_timestamp,
        )
        for _ in range(len(plan.nodes) * 2 + 1):
            current = self._store.get(execution_id) or execution
            scheduler.reconcile(plan, current)
        tasks = tuple(
            item
            for item in self._store.list_by_workflow_execution(execution_id)
            if item.get("kind") == "TaskExecution"
        )
        successful_names = {
            item.get("taskRef", {}).get("name")
            for item in tasks
            if item.get("status") == "SUCCEEDED"
        }
        expected_names = {node.task_ref.name for node in plan.nodes}
        failed = successful_names != expected_names
        current = self._store.get(execution_id) or execution
        if current.get("status") == "RUNNING":
            self._store.update_status(
                execution_id,
                "FAILED" if failed else "SUCCEEDED",
                expected_status="RUNNING",
            )
        return execution_id

    def _authorization(
        self, task: Resource, revision: str
    ) -> Callable[[Any], bool]:
        refs = task.data.get("spec", {}).get("policies", ())
        policies = []
        for value in refs:
            resource = self._resources.get(ResourceRef.from_mapping(value))
            if resource is None:
                raise DogfoodReconciliationError("Task Policy is unavailable")
            policies.append(ApplicablePolicy(PolicyScope.TASK, resource.data))
        engine = PreExecutionCapabilityPolicy(self._store)

        def authorize(request: Any) -> bool:
            safe_scope = {
                key: value
                for key, value in request.input.items()
                if key in {"operation", "path", "branch", "expectedRevision"}
            }
            safe_scope["repositoryRevision"] = revision
            boundary = engine.tool_authorization_boundary(
                task_execution_id=request.correlation.task_execution_id,
                resource_scope=safe_scope,
                execution_context={"environment": "dogfood"},
                applicable_policies=policies,
                timestamp=_timestamp(),
            )
            return boundary(request)

        return authorize

    def _evaluation_checkout(self, checkout: Any) -> Path:
        root = self._state_root / "evaluation-worktrees" / checkout.execution_id
        if root.exists():
            head = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                capture_output=True, text=True, check=False, timeout=10,
            )
            branch = subprocess.run(
                ["git", "-C", str(root), "branch", "--show-current"],
                capture_output=True, text=True, check=False, timeout=10,
            )
            status = subprocess.run(
                ["git", "-C", str(root), "status", "--porcelain"],
                capture_output=True, text=True, check=False, timeout=10,
            )
            if (
                head.returncode
                or branch.returncode
                or status.returncode
                or head.stdout.strip() != checkout.base_revision
                or branch.stdout.strip() != checkout.branch
                or status.stdout
            ):
                raise DogfoodReconciliationError(
                    "evaluation checkout failed identity or cleanliness checks"
                )
            return root.resolve()
        root.parent.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            [
                "git", "clone", "--no-hardlinks", "--no-checkout", "--",
                str(checkout.source_cache_path), str(root),
            ],
            capture_output=True, check=False, timeout=60,
        )
        if completed.returncode:
            raise DogfoodReconciliationError("evaluation checkout could not be created")
        subprocess.run(
            [
                "git", "-C", str(root), "checkout", "-B", checkout.branch,
                checkout.base_revision,
            ],
            capture_output=True, check=True, timeout=30,
        )
        return root.resolve()

    def _event_resource(self, workflow: Resource) -> Resource:
        triggers = workflow.data.get("spec", {}).get("triggers", ())
        if len(triggers) != 1:
            raise DogfoodReconciliationError("Workflow must have one trigger")
        ref = ResourceRef.from_mapping(triggers[0]["eventRef"])
        resource = self._resources.get(ref)
        if resource is None:
            raise DogfoodReconciliationError("Workflow Event Resource is unavailable")
        return resource

    def _resource(self, kind: str, name: str) -> Resource:
        matches = [
            item
            for item in self._resources.resources
            if item.kind == kind and item.name == name
        ]
        if len(matches) != 1:
            raise DogfoodReconciliationError(f"{kind}/{name} is unavailable")
        return matches[0]

    def _required(self, name: str) -> str:
        value = self._environment.get(name, "").strip()
        if not value:
            raise DogfoodReconciliationError(f"{name} must be configured")
        return value


class DogfoodReconciliationConsumer:
    """Poll durable webhook outbox rows and retire only terminal work."""

    def __init__(
        self,
        dispatcher: SQLiteReconciliationDispatcher,
        runner: DogfoodWorkflowRunner,
        *,
        poll_seconds: float = 2.0,
    ) -> None:
        self._dispatcher = dispatcher
        self._runner = runner
        self._poll_seconds = poll_seconds
        self._stop = Event()
        self._thread: Thread | None = None
        self._last_poll_error: str | None = None
        self._last_poll_at: datetime | None = None
        self._logger = logging.getLogger(__name__)
        self._reported_failures: dict[str, tuple[str, str, str]] = {}

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str]
    ) -> "DogfoodReconciliationConsumer":
        state = Path(environment["AEP_STATE_ROOT"]).resolve()
        return cls(
            SQLiteReconciliationDispatcher(
                state / "shared" / "github-webhook.sqlite3"
            ),
            DogfoodWorkflowRunner(environment),
        )

    def run_once(self) -> int:
        processed = 0
        requests = self._dispatcher.pending_requests()
        pending_event_ids = {str(request.get("eventId", "")) for request in requests}
        self._reported_failures = {
            event_id: signature
            for event_id, signature in self._reported_failures.items()
            if event_id in pending_event_ids
        }
        for request in requests:
            event_id = str(request.get("eventId", ""))
            try:
                self._runner.run(request)
            except Exception as error:
                if isinstance(error, CheckoutProvisionError):
                    classification = str(
                        getattr(error.classification, "value", error.classification)
                    )
                    self._log_safe_failure(
                        event_id,
                        classification=classification,
                        code=error.code,
                        error=error,
                    )
                    if classification == "CONFIGURATION":
                        self._dispatcher.mark_failed(
                            event_id,
                            failure_class=classification,
                            message="dogfood checkout provisioning failed configuration checks",
                        )
                    continue
                classification = (
                    "CONFIGURATION"
                    if isinstance(error, (DogfoodReconciliationError, ValueError))
                    else "RECOVERABLE"
                )
                self._log_safe_failure(
                    event_id,
                    classification=classification,
                    code="reconciliation_failed",
                    error=error,
                )
                if classification == "CONFIGURATION":
                    self._dispatcher.mark_failed(
                        event_id,
                        failure_class=classification,
                        message="dogfood reconciliation failed configuration checks",
                    )
                continue
            self._dispatcher.mark_completed(event_id)
            self._reported_failures.pop(event_id, None)
            processed += 1
        return processed

    def _log_safe_failure(
        self,
        event_id: str,
        *,
        classification: str,
        code: str,
        error: Exception,
    ) -> None:
        signature = (classification, code, type(error).__name__)
        if self._reported_failures.get(event_id) == signature:
            return
        self._reported_failures[event_id] = signature
        self._logger.warning(
            "dogfood reconciliation deferred event_id=%s failure_class=%s "
            "failure_code=%s exception_type=%s",
            event_id,
            classification,
            code,
            type(error).__name__,
        )

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("consumer is already started")
        self._thread = Thread(target=self._run, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(5.0, self._poll_seconds * 2))
            self._thread = None

    def liveness(self) -> Mapping[str, Any]:
        """Expose the polling state for service diagnostics and health checks."""
        return {
            "status": "degraded" if self._last_poll_error else "healthy",
            "lastPollAt": self._last_poll_at.isoformat() if self._last_poll_at else None,
            "lastPollError": self._last_poll_error,
        }

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as error:
                self._last_poll_error = f"{type(error).__name__}: {error}"
                self._logger.exception("dogfood reconciliation polling failed; retrying")
            else:
                self._last_poll_error = None
            self._last_poll_at = datetime.now(UTC)
            self._stop.wait(self._poll_seconds)


def _reconciliation_revision(
    store: DurableJsonRuntimeObjectStore,
    execution_id: str,
    resolve_revision: Callable[[], str],
) -> str | None:
    """Reuse immutable execution inputs across retries and restarts."""
    existing = store.get(execution_id)
    if existing is None:
        return resolve_revision()
    if existing.get("status") in {"SUCCEEDED", "FAILED", "CANCELLED"}:
        return None
    revision = existing.get("repositoryRevision")
    if not isinstance(revision, str) or not revision:
        raise DogfoodReconciliationError(
            "existing WorkflowExecution has no repositoryRevision"
        )
    return revision


def _pinned_workspace_reader(
    workspace: Path, expected_revision: str
) -> Callable[[str, str, int], str]:
    root = workspace.resolve()

    def read(path: str, revision: str, max_bytes: int) -> str:
        if revision != expected_revision:
            raise ValueError("planning-evidence revision does not match the execution checkout")
        relative = Path(path)
        if relative.is_absolute() or ".." in relative.parts or relative.parts[0].casefold() == ".git":
            raise ValueError("planning-evidence path is unsafe")
        target = (root / relative).resolve()
        try:
            target.relative_to(root)
        except ValueError as error:
            raise ValueError("planning-evidence path escapes the execution checkout") from error
        from aep.planning_evidence import PlanningEvidenceInspectionError
        if not target.exists():
            raise PlanningEvidenceInspectionError(
                "TARGET_MISSING", path=path, applied_ceiling=max_bytes)
        if not target.is_file():
            raise PlanningEvidenceInspectionError(
                "NON_REGULAR_FILE", path=path, applied_ceiling=max_bytes)
        size = target.stat().st_size
        if size > max_bytes:
            raise PlanningEvidenceInspectionError(
                "SIZE_LIMIT_EXCEEDED", path=path, blob_size=size,
                applied_ceiling=max_bytes)
        data = target.read_bytes()
        if len(data) != size:
            raise PlanningEvidenceInspectionError(
                "CONCURRENT_SIZE_DRIFT", path=path, blob_size=len(data),
                applied_ceiling=max_bytes)
        if b"\x00" in data:
            raise PlanningEvidenceInspectionError(
                "BINARY_CONTENT", path=path, blob_size=size,
                applied_ceiling=max_bytes)
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise PlanningEvidenceInspectionError(
                "INVALID_UTF8", path=path, blob_size=size,
                applied_ceiling=max_bytes) from error

    return read


def _workflow_execution_id(event_id: str, workflow: Resource) -> str:
    identity = f"{event_id}:{workflow.kind}/{workflow.name}:{workflow.version}"
    return f"workflowexecution-{uuid5(NAMESPACE_URL, f'workflow-execution:{identity}')}"


def _git_adapter_for_peer(
    root: Path,
    checkout: Any,
    *,
    log_store: Any,
    sandbox: Any,
) -> Any:
    from aep.git_tool import GitToolAdapter

    return GitToolAdapter(
        repository=root,
        repository_id=checkout.repository.canonical,
        expected_revision=checkout.base_revision,
        working_branch=checkout.branch,
        log_store=log_store,
        sandbox=sandbox,
    )
