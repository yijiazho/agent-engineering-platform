"""Durable repository-bound reconciliation worker for the dogfood deployment."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import codecs
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
import logging
import json
from pathlib import Path
import re
import subprocess
from threading import Event, RLock, Thread
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from aep.analyze_issue import AnalyzeIssueTaskHandler
from aep.build_implementation_plan import BuildImplementationPlanTaskHandler
from aep.capability_policy import ApplicablePolicy, PolicyScope, PreExecutionCapabilityPolicy
from aep.context_builder import ContextBuilder
from aep.context_identity import context_identity
from aep.create_pull_request import CreatePullRequestTaskHandler
from aep.execution_replay import ExecutionReplayService, ReplayError
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
from aep.resource_loader import Resource, ResourceCollection, ResourceLoader, ResourceRef
from aep.run_validation import RunValidationTaskHandler
from aep.runtime_store import DurableJsonRuntimeObjectStore, RuntimeObject, StatusConflictError
from aep.task_execution import FailureClass
from aep.task_dag import resolve_task_dag
from aep.webhook_dispatch import SQLiteReconciliationDispatcher
from aep.workflow_execution import WorkflowExecutionCreator
from aep.workflow_resolver import resolve_workflow_for_event
from aep.workflow_scheduler import TaskExecutionResult, WorkflowScheduler


class DogfoodReconciliationError(RuntimeError):
    """Safe terminal reconciliation failure suitable for persisted evidence."""


def _historical_replay_inputs(
    source: Mapping[str, Any], related: tuple[RuntimeObject, ...],
) -> tuple[dict[str, Any], dict[tuple[str, str, str], Mapping[str, Any]]]:
    by_id = {str(item.get("id")): item for item in related if item.get("id")}
    contexts: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    event_candidates: list[Mapping[str, Any]] = []
    task_ids = source.get("taskExecutionIds")
    if not isinstance(task_ids, list):
        raise ReplayError("source TaskExecution references are malformed")
    for task_id in task_ids:
        task = by_id.get(task_id)
        if not isinstance(task, Mapping) or task.get("kind") != "TaskExecution":
            raise ReplayError("source TaskExecution evidence is missing")
        context_id = task.get("contextPackageId")
        context = by_id.get(context_id) if isinstance(context_id, str) else None
        if not isinstance(context, Mapping) or context.get("kind") != "ContextPackage":
            continue
        task_ref = context.get("taskRef")
        if not isinstance(task_ref, Mapping):
            continue
        task_key = (
            str(task_ref.get("kind", "")), str(task_ref.get("name", "")),
            str(task_ref.get("version", "")),
        )
        contexts[task_key] = context
        for element in context.get("elements", ()):
            if not isinstance(element, Mapping) or element.get("type") != "event":
                continue
            if element.get("truncated") is True or not isinstance(element.get("content"), Mapping):
                raise ReplayError("historical Event context is truncated or malformed")
            event_candidates.append(element["content"])
    matches = [
        event for event in event_candidates
        if event.get("id") == source.get("eventId")
    ]
    unique = {
        json.dumps(dict(event), sort_keys=True, separators=(",", ":"))
        for event in matches
    }
    if len(unique) != 1:
        raise ReplayError("source execution does not contain one complete historical Event")
    event = json.loads(next(iter(unique)))
    required_fields = ("id", "source", "type", "receivedAt", "deduplicationKey")
    if any(not isinstance(event.get(field), str) or not event[field] for field in required_fields):
        raise ReplayError("historical Event is missing required normalized fields")
    return event, contexts


def _replay_resource_collection(
    historical: ResourceCollection,
    available: ResourceCollection,
    replay_provenance: Mapping[str, Any],
) -> ResourceCollection:
    resources = {resource.ref: resource for resource in historical.resources}
    overrides = replay_provenance.get("overrides", ())
    if not isinstance(overrides, (list, tuple)):
        raise ReplayError("replay Resource overrides are malformed")
    for raw in overrides:
        if not isinstance(raw, Mapping):
            raise ReplayError("replay Resource override is malformed")
        ref = ResourceRef.from_mapping(dict(raw))
        candidate = resources.get(ref) or available.get(ref)
        if candidate is None:
            raise ReplayError("an overridden Resource version cannot be reconstructed")
        resources[ref] = candidate
        pending = [candidate]
        while pending:
            current = pending.pop()
            for dependency in current.references:
                if dependency in resources:
                    continue
                resolved = available.get(dependency)
                if resolved is None:
                    raise ReplayError("an overridden Resource dependency cannot be reconstructed")
                resources[dependency] = resolved
                pending.append(resolved)

    inherited = replay_provenance.get("inheritedResourceRefs", ())
    effective = replay_provenance.get("resourceRefs", ())
    override_by_name: dict[tuple[str, str], ResourceRef] = {}
    for item in overrides:
        if not isinstance(item, Mapping):
            raise ReplayError("replay Resource override is malformed")
        ref = ResourceRef.from_mapping(dict(item))
        override_by_name[(ref.kind, ref.name)] = ref
    replacements: dict[tuple[str, str, str], tuple[str, str, str]] = {}
    for original in inherited:
        if not isinstance(original, Mapping):
            continue
        replacement = override_by_name.get((original.get("kind"), original.get("name")))
        if replacement is not None and replacement.version != original.get("version"):
            replacements[(str(original["kind"]), str(original["name"]), str(original["version"]))] = (
                replacement.kind, replacement.name, replacement.version,
            )

    rewritten = []
    for resource in resources.values():
        data = _replace_versioned_refs(deepcopy(resource.data), replacements)
        references = tuple(
            ResourceRef(*replacements.get((ref.kind, ref.name, ref.version), (ref.kind, ref.name, ref.version)))
            for ref in resource.references
        )
        rewritten.append(Resource(resource.ref, resource.path, data, references))
    workspace = resources.get(historical.workspace.ref, historical.workspace)
    collection = ResourceCollection(workspace, tuple(sorted(rewritten, key=lambda item: (item.kind, item.name, item.version))))
    for reference in effective:
        if not isinstance(reference, Mapping) or collection.get(ResourceRef.from_mapping(dict(reference))) is None:
            raise ReplayError("a required historical Resource version is unavailable")
    return collection


def _replace_versioned_refs(
    value: Any,
    replacements: Mapping[tuple[str, str, str], tuple[str, str, str]],
) -> Any:
    if isinstance(value, Mapping):
        result = {str(key): _replace_versioned_refs(item, replacements) for key, item in value.items()}
        if all(field in result for field in ("kind", "name", "version")):
            key = (str(result["kind"]), str(result["name"]), str(result["version"]))
            replacement = replacements.get(key)
            if replacement is not None:
                result.update(kind=replacement[0], name=replacement[1], version=replacement[2])
        return result
    if isinstance(value, list):
        return [_replace_versioned_refs(item, replacements) for item in value]
    return value


def _workflow_lifecycle_event(
    execution: Mapping[str, Any],
    *,
    event_type: str,
    sequence: int,
    emitted_at: str,
    status: str,
    payload: Mapping[str, Any] | None = None,
    failure: Mapping[str, Any] | None = None,
) -> RuntimeObject:
    execution_id = str(execution["id"])
    event_uuid = uuid5(NAMESPACE_URL, f"execution-event:{execution_id}:{event_type}")
    values = {"status": status, **dict(payload or {})}
    source_id = execution.get("sourceExecutionId")
    if isinstance(source_id, str):
        values["sourceExecutionId"] = source_id
    if failure is not None:
        values["failureClass"] = failure.get("class")
    provenance = execution.get("provenance", {})
    return {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "ExecutionEvent",
        "id": f"executionevent-{event_uuid}",
        "traceId": execution["traceId"],
        "createdAt": emitted_at,
        "updatedAt": emitted_at,
        "provenance": {
            "actor": "execution-replay" if source_id else "workflow-controller",
            "workflowExecutionId": execution_id,
            "repositoryRevision": execution["repositoryRevision"],
            "knowledgeGraphVersion": execution.get("knowledgeGraphVersion", "unknown"),
            "resourceRefs": deepcopy(provenance.get("resourceRefs", [])),
        },
        "eventType": event_type,
        "subject": {"kind": "WorkflowExecution", "id": execution_id},
        "sequence": sequence,
        "emittedAt": emitted_at,
        "payload": values,
    }


def _new_runtime_evidence(related: tuple[RuntimeObject, ...]) -> list[dict[str, str]]:
    evidence = [
        {"kind": str(item["kind"]), "id": str(item["id"])}
        for item in related
        if item.get("kind") != "ExecutionEvent"
        and isinstance(item.get("id"), str)
        and isinstance(item.get("kind"), str)
    ]
    return sorted(evidence, key=lambda item: (item["kind"], item["id"]))


def _workflow_failure(related: tuple[RuntimeObject, ...]) -> dict[str, str]:
    for item in reversed(related):
        failure = item.get("failure")
        if item.get("kind") == "TaskExecution" and item.get("status") == "FAILED" and isinstance(failure, Mapping):
            try:
                classification = FailureClass(str(failure.get("class"))).value
            except ValueError:
                classification = FailureClass.PERMANENT.value
            return {"class": classification, "message": "one or more replay Tasks failed"}
    return {"class": FailureClass.PERMANENT.value, "message": "one or more replay Tasks failed"}


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

    def __init__(
        self,
        environment: Mapping[str, str],
        *,
        state_file: Path | str | None = None,
    ) -> None:
        self._environment = dict(environment)
        self._resource_lock = RLock()
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
            state_file or self._state_root / "runtime" / "objects.json"
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

    def replay(
        self,
        source_id: str,
        *,
        overrides: tuple[str, ...] = (),
        context_mode: str = "reuse",
        publication: bool = False,
    ) -> str:
        with self._resource_lock:
            return self._replay_locked(
                source_id,
                overrides=overrides,
                context_mode=context_mode,
                publication=publication,
            )

    def _replay_locked(
        self,
        source_id: str,
        *,
        overrides: tuple[str, ...] = (),
        context_mode: str = "reuse",
        publication: bool = False,
    ) -> str:
        """Create and execute a replay using the source revision and Resources."""
        replay = ExecutionReplayService(self._store).replay(
            source_id,
            overrides=overrides,
            context_mode=context_mode,
            publication=publication,
        )
        replay_id = str(replay["id"])
        original_resources = self._resources
        try:
            source = self._store.get(source_id)
            if source is None:
                raise ReplayError("source WorkflowExecution disappeared")
            event, inherited_contexts = _historical_replay_inputs(
                source, self._store.list_by_workflow_execution(source_id)
            )
            expected_repository = (
                f"{self._required('AEP_REPOSITORY_OWNER')}/"
                f"{self._required('AEP_REPOSITORY_NAME')}"
            ).casefold()
            if (
                not isinstance(event.get("repository"), Mapping)
                or str(event["repository"].get("full_name", "")).casefold()
                != expected_repository
            ):
                raise ReplayError(
                    "historical Event repository differs from the configured execution repository"
                )
            manager = self._checkout_manager()
            checkout, _ = manager.provision_orchestration(
                CheckoutRequest(
                    execution_id=replay_id,
                    repository=RepositoryIdentity(
                        "github",
                        self._required("AEP_REPOSITORY_OWNER"),
                        self._required("AEP_REPOSITORY_NAME"),
                    ),
                    default_branch=self._required("AEP_REPOSITORY_DEFAULT_BRANCH"),
                    base_revision=str(replay["repositoryRevision"]),
                    knowledge_revision=str(replay["repositoryRevision"]),
                )
            )
            historical_resources = ResourceLoader(
                checkout.workspace_path,
                schema_root=checkout.workspace_path / "schemas" / "resources" / "v1",
            ).load()
            self._resources = _replay_resource_collection(
                historical_resources,
                original_resources,
                replay["replayProvenance"],
            )
            for reference in replay["replayProvenance"]["resourceRefs"]:
                if self._resources.get(ResourceRef.from_mapping(reference)) is None:
                    raise ReplayError(
                        "a required historical or overridden Resource version is unavailable"
                    )
            resolution = resolve_workflow_for_event(event, self._resources)
            if resolution.workflow_ref != ResourceRef.from_mapping(source["workflowRef"]):
                raise ReplayError("pinned Workflow does not match the source execution")
            expected_context_identities: dict[tuple[str, str, str], str] = {}
            reused_contexts: dict[tuple[str, str, str], Mapping[str, Any]] = {}
            for task_key, package in inherited_contexts.items():
                if context_mode == "reuse":
                    reused_contexts[task_key] = package
                else:
                    expected_context_identities[task_key] = context_identity(package)
            return self.run({
                "event": event,
                "workflowExecutionId": replay_id,
                "isReplay": True,
                "publicationEnabled": publication,
                "sourceExecutionId": source_id,
                "reusedContexts": reused_contexts,
                "expectedContextIdentities": expected_context_identities,
                "requireReusedContext": context_mode == "reuse",
                "requireExpectedContextIdentity": context_mode == "rebuild",
            })
        except Exception as error:
            self._fail_replay(replay_id, error)
            if isinstance(error, ReplayError):
                raise
            raise ReplayError("replay could not be dispatched from its pinned inputs") from error
        finally:
            self._resources = original_resources

    def _checkout_manager(self) -> ExecutionCheckoutManager:
        return ExecutionCheckoutManager(
            source=GitRepositorySource(
                "https://github.com/yijiazho/agent-engineering-platform.git"
            ),
            source_cache_root=self._state_root / "repository-cache",
            worktree_root=self._state_root / "execution-worktrees",
            registry=local_checkout_registry(self._state_root),
            credential_provider=self._github.credentials,
        )

    def _commit_workflow_transition(
        self,
        execution: Mapping[str, Any],
        status: str,
        *,
        expected_status: str,
        event_type: str,
        changes: Mapping[str, Any] | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> RuntimeObject:
        timestamp = _timestamp()
        sequence = (
            3 if execution.get("sourceExecutionId") and execution.get("startedAt")
            else 2
        )
        event = _workflow_lifecycle_event(
            execution,
            event_type=event_type,
            sequence=sequence,
            emitted_at=timestamp,
            status=status,
            payload=payload,
            failure=(changes or {}).get("failure"),
        )
        return self._store.commit_workflow_execution_transition(
            str(execution["id"]),
            status,
            event=event,
            expected_status=expected_status,
            updated_at=timestamp,
            changes=dict(changes or {}),
        )

    def _fail_replay(self, replay_id: str, error: Exception) -> None:
        execution = self._store.get(replay_id)
        if execution is None or execution.get("status") in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            return
        status = str(execution.get("status"))
        candidate_class = getattr(error, "classification", None)
        if candidate_class is None:
            candidate_class = (
                FailureClass.CONFIGURATION.value
                if isinstance(error, (ReplayError, DogfoodReconciliationError, ValueError))
                else FailureClass.PERMANENT.value
            )
        try:
            failure_class = FailureClass(str(getattr(candidate_class, "value", candidate_class))).value
        except ValueError:
            failure_class = FailureClass.PERMANENT.value
        message = str(error) if isinstance(error, (ReplayError, DogfoodReconciliationError)) else "replay execution failed"
        related = self._store.list_by_workflow_execution(replay_id)
        replay_provenance = dict(execution.get("replayProvenance", {}))
        replay_provenance["newRuntimeEvidence"] = _new_runtime_evidence(related)
        self._commit_workflow_transition(
            execution,
            "FAILED",
            expected_status=status,
            event_type="WorkflowExecutionFailed",
            changes={
                "failure": {"class": failure_class, "message": message},
                "replayProvenance": replay_provenance,
            },
        )

    def run(self, request: Mapping[str, Any]) -> str:
        with self._resource_lock:
            return self._run_locked(request)

    def is_reconciliation_complete(self, request: Mapping[str, Any]) -> bool:
        event = request.get("event")
        if not isinstance(event, Mapping):
            return False
        resolution = resolve_workflow_for_event(event, self._resources)
        if resolution.workflow_ref is None:
            return False
        workflow = self._resources.get(resolution.workflow_ref)
        if workflow is None:
            return False
        execution_id = str(
            request.get("workflowExecutionId")
            if request.get("isReplay")
            else _workflow_execution_id(str(event["id"]), workflow)
        )
        execution = self._store.get(execution_id)
        return isinstance(execution, Mapping) and execution.get("status") in {
            "SUCCEEDED", "FAILED", "CANCELLED",
        }

    def _run_locked(self, request: Mapping[str, Any]) -> str:
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
        replay_execution = bool(request.get("isReplay"))
        execution_id = str(
            request.get("workflowExecutionId")
            if replay_execution
            else _workflow_execution_id(str(event["id"]), workflow)
        )
        revision = _reconciliation_revision(
            self._store,
            execution_id,
            self._github.client.resolve_default_branch_revision,
        )
        if revision is None:
            return execution_id
        checkout, orchestration = self._checkout_manager().provision_orchestration(
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
        if replay_execution:
            execution = self._store.get(execution_id)
            if (
                execution is None
                or execution.get("sourceExecutionId") != request.get("sourceExecutionId")
                or execution.get("eventId") != event.get("id")
                or execution.get("workflowRef") != {
                    "kind": workflow.kind,
                    "name": workflow.name,
                    "version": workflow.version,
                }
                or execution.get("eventRef") != {
                    "kind": event_resource.kind,
                    "name": event_resource.name,
                    "version": event_resource.version,
                }
            ):
                raise DogfoodReconciliationError(
                    "replay root does not match its pinned Event and Workflow"
                )
            if (
                snapshot.repository_revision != revision
                or snapshot.snapshot_version != execution.get("knowledgeGraphVersion")
            ):
                raise ReplayError(
                    "pinned repository knowledge identity differs from the source execution"
                )
            if execution.get("status") == "PENDING":
                try:
                    execution = self._commit_workflow_transition(
                        execution,
                        "RUNNING",
                        expected_status="PENDING",
                        event_type="WorkflowExecutionStarted",
                        changes={"startedAt": _timestamp()},
                        payload={"sourceExecutionId": request.get("sourceExecutionId")},
                    )
                except StatusConflictError:
                    execution = self._store.get(execution_id)
                    if execution is None or execution.get("status") not in {"RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"}:
                        raise
            elif execution.get("status") in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                return execution_id
        else:
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
            reused_contexts=request.get("reusedContexts"),
            expected_context_identities=request.get("expectedContextIdentities"),
            require_reused_context=bool(request.get("requireReusedContext")),
            require_expected_context_identity=bool(
                request.get("requireExpectedContextIdentity")
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
                publication_guard=(
                    (lambda: False)
                    if replay_execution and not request.get("publicationEnabled")
                    else None
                ),
                publication_disabled_message=(
                    "publication is disabled for replay"
                    if replay_execution and not request.get("publicationEnabled")
                    else "publication is emergency-disabled by the operator"
                ),
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
        in_flight = _has_in_flight_tasks(tasks)
        failed = successful_names != expected_names and not in_flight
        current = self._store.get(execution_id) or execution
        if current.get("status") == "RUNNING" and not in_flight:
            changes: dict[str, Any] = {}
            if replay_execution:
                changes["replayProvenance"] = {
                    **dict(current.get("replayProvenance", {})),
                    "newRuntimeEvidence": _new_runtime_evidence(
                        self._store.list_by_workflow_execution(execution_id)
                    ),
                }
            if failed:
                changes["failure"] = _workflow_failure(
                    self._store.list_by_workflow_execution(execution_id)
                )
            terminal_status = "FAILED" if failed else "SUCCEEDED"
            self._commit_workflow_transition(
                current,
                terminal_status,
                expected_status="RUNNING",
                event_type=(
                    "WorkflowExecutionFailed" if failed else "WorkflowExecutionCompleted"
                ),
                changes=changes,
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
            completion_check = getattr(self._runner, "is_reconciliation_complete", None)
            if callable(completion_check) and not completion_check(request):
                self._reported_failures.pop(event_id, None)
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


def _has_in_flight_tasks(tasks: Sequence[Mapping[str, Any]]) -> bool:
    return any(
        item.get("status") in {"PENDING", "RUNNING", "AWAITING_APPROVAL"}
        for item in tasks
    )


def _pinned_workspace_reader(
    workspace: Path, expected_revision: str
) -> Any:
    root = workspace.resolve()

    class PinnedWorkspaceReader:
        def __call__(self, path: str, revision: str, max_bytes: int) -> str:
            return self.inspect(
                path, revision, max_bytes=max_bytes,
                strategy="COMPLETE_BLOB_SCAN", status_scan_bytes=max_bytes,
            ).content

        def inspect(self, path: str, revision: str, *, max_bytes: int,
                    strategy: str, status_scan_bytes: int):
            from aep.planning_evidence import (
                PlanningEvidenceInspection, PlanningEvidenceInspectionError,
                _is_nonempty_document_title,
            )
            if revision != expected_revision:
                raise PlanningEvidenceInspectionError(
                    "REVISION_MISMATCH", path=path)
            relative = Path(path)
            if (relative.is_absolute() or ".." in relative.parts
                    or relative.parts[0].casefold() == ".git"):
                raise PlanningEvidenceInspectionError("UNSAFE_PATH", path=path)
            entry = self._entry(path, revision)
            if entry is None:
                raise PlanningEvidenceInspectionError(
                    "TARGET_MISSING", path=path, applied_ceiling=max_bytes)
            mode, object_type = entry
            if object_type != "blob" or mode not in {"100644", "100755"}:
                raise PlanningEvidenceInspectionError(
                    "NON_REGULAR_FILE", path=path, applied_ceiling=max_bytes)
            object_name = f"{revision}:{path}"
            size_result = subprocess.run(
                ["git", "-C", str(root), "cat-file", "-s", object_name],
                capture_output=True, check=False,
            )
            if size_result.returncode != 0:
                raise PlanningEvidenceInspectionError(
                    "REVISION_MISMATCH", path=path, applied_ceiling=max_bytes)
            size = int(size_result.stdout.decode("ascii").strip())
            if size > max_bytes:
                raise PlanningEvidenceInspectionError(
                    "SIZE_LIMIT_EXCEEDED", path=path, blob_size=size,
                    applied_ceiling=max_bytes)
            digest = sha256()
            data = bytearray() if strategy == "COMPLETE_BLOB_SCAN" else None
            inspected = 0
            decoder = codecs.getincrementaldecoder("utf-8")()
            status_pattern = re.compile(
                r"^\*\*Status:\*\*[^\S\r\n]*(?P<value>\S(?:[^\r\n]*\S)?)[^\S\r\n]*$"
            )
            status_prefix_pattern = re.compile(r"^\*\*Status:\*\*")
            status_fields: list[tuple[str, int]] = []
            status_buffer = ""
            status_line = 1
            status_active = data is None
            status_after_cr = False
            title_seen = False
            status_seen = False

            def consume_status_line(line: str) -> None:
                nonlocal status_active, title_seen, status_seen, status_line
                text = line.removesuffix("\r")
                if text and not all(character in " \t" for character in text):
                    match = status_pattern.fullmatch(text)
                    if match:
                        status_fields.append((match.group("value"), status_line))
                        status_seen = True
                    elif status_prefix_pattern.match(text):
                        raise PlanningEvidenceInspectionError(
                            "STATUS_FIELD_MALFORMED", path=path, blob_size=size,
                            applied_ceiling=max_bytes, strategy=strategy,
                            evaluation_complete=True,
                        )
                    elif not title_seen and not status_seen and _is_nonempty_document_title(text):
                        title_seen = True
                    else:
                        status_active = False
                status_line += 1

            def retain_status_fragment(fragment: str) -> None:
                nonlocal status_active, status_buffer
                candidate = status_buffer + fragment
                if candidate and not all(
                    character in " \t" for character in candidate
                ):
                    status_prefix = "**Status:**"
                    if candidate.startswith(status_prefix):
                        possible_metadata = True
                    elif status_prefix.startswith(candidate):
                        possible_metadata = True
                    elif not title_seen and not status_seen:
                        possible_metadata = bool(
                            re.match(
                                r"^ {0,3}#(?:[ \t].*)?$", candidate
                            )
                        )
                    else:
                        possible_metadata = False
                    if not possible_metadata:
                        status_active = False
                        status_buffer = ""
                        return
                retained_bytes = len(status_buffer.encode("utf-8"))
                fragment_bytes = len(fragment.encode("utf-8"))
                if retained_bytes + fragment_bytes > status_scan_bytes:
                    raise PlanningEvidenceInspectionError(
                        "STATUS_FIELD_SCAN_LIMIT_EXCEEDED", path=path,
                        blob_size=size, applied_ceiling=status_scan_bytes,
                        strategy=strategy, evaluation_complete=False,
                    )
                status_buffer = candidate

            def consume_status_text(value: str) -> None:
                nonlocal status_after_cr, status_buffer
                remainder = value
                if status_after_cr:
                    if remainder.startswith("\n"):
                        remainder = remainder[1:]
                    status_after_cr = False
                while remainder and status_active:
                    cr_boundary = remainder.find("\r")
                    lf_boundary = remainder.find("\n")
                    boundaries = tuple(
                        item for item in (cr_boundary, lf_boundary) if item >= 0
                    )
                    if not boundaries:
                        retain_status_fragment(remainder)
                        return
                    boundary = min(boundaries)
                    retain_status_fragment(remainder[:boundary])
                    if not status_active:
                        return
                    consume_status_line(status_buffer)
                    status_buffer = ""
                    delimiter = remainder[boundary]
                    remainder = remainder[boundary + 1:]
                    if delimiter == "\r":
                        if remainder.startswith("\n"):
                            remainder = remainder[1:]
                        elif not remainder:
                            status_after_cr = True
            process = subprocess.Popen(
                ["git", "-C", str(root), "cat-file", "blob", object_name],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            assert process.stdout is not None
            with process.stdout as stream:
                while chunk := stream.read(64 * 1024):
                    inspected += len(chunk)
                    if inspected > max_bytes:
                        raise PlanningEvidenceInspectionError(
                            "SIZE_LIMIT_EXCEEDED", path=path, blob_size=inspected,
                            applied_ceiling=max_bytes, strategy=strategy)
                    digest.update(chunk)
                    if b"\x00" in chunk:
                        raise PlanningEvidenceInspectionError(
                            "BINARY_CONTENT", path=path, blob_size=size,
                            applied_ceiling=max_bytes, strategy=strategy)
                    try:
                        decoded = decoder.decode(chunk, final=False)
                    except UnicodeDecodeError as error:
                        raise PlanningEvidenceInspectionError(
                            "INVALID_UTF8", path=path, blob_size=size,
                            applied_ceiling=max_bytes, strategy=strategy) from error
                    if data is not None:
                        data.extend(chunk)
                    elif status_active:
                        consume_status_text(decoded)
            try:
                final_text = decoder.decode(b"", final=True)
            except UnicodeDecodeError as error:
                raise PlanningEvidenceInspectionError(
                    "INVALID_UTF8", path=path, blob_size=size,
                    applied_ceiling=max_bytes, strategy=strategy) from error
            if data is None and status_active:
                consume_status_text(final_text)
                if status_buffer:
                    consume_status_line(status_buffer)
            assert process.stderr is not None
            stderr = process.stderr.read()
            return_code = process.wait()
            if return_code != 0:
                raise PlanningEvidenceInspectionError(
                    "REVISION_MISMATCH", path=path, blob_size=inspected,
                    applied_ceiling=max_bytes, strategy=strategy)
            if inspected != size:
                raise PlanningEvidenceInspectionError(
                    "CONCURRENT_SIZE_DRIFT", path=path, blob_size=inspected,
                    applied_ceiling=max_bytes)
            raw = bytes(data) if data is not None else b""
            try:
                content = raw.decode("utf-8") if data is not None else ""
            except UnicodeDecodeError as error:
                raise PlanningEvidenceInspectionError(
                    "INVALID_UTF8", path=path, blob_size=size,
                    applied_ceiling=max_bytes) from error
            return PlanningEvidenceInspection(
                content=content, blob_size=size, blob_sha256=digest.hexdigest(),
                inspected_bytes=inspected, status_fields=tuple(status_fields),
            )

        def verify_absent(self, path: str, revision: str) -> bool:
            from aep.planning_evidence import PlanningEvidenceInspectionError
            if revision != expected_revision:
                raise PlanningEvidenceInspectionError(
                    "REVISION_MISMATCH", path=path)
            relative = Path(path)
            if (relative.is_absolute() or ".." in relative.parts
                    or relative.parts[0].casefold() == ".git"):
                raise PlanningEvidenceInspectionError("UNSAFE_PATH", path=path)
            entry = self._entry(path, revision)
            if entry is None:
                return True
            mode, object_type = entry
            if object_type != "blob" or mode not in {"100644", "100755"}:
                raise PlanningEvidenceInspectionError(
                    "NON_REGULAR_FILE", path=path)
            return False

        @staticmethod
        def _entry(path: str, revision: str) -> tuple[str, str] | None:
            result = subprocess.run(
                ["git", "-C", str(root), "ls-tree", "-z", revision, "--", path],
                capture_output=True, check=False,
            )
            if result.returncode != 0:
                from aep.planning_evidence import PlanningEvidenceInspectionError
                raise PlanningEvidenceInspectionError("REVISION_MISMATCH", path=path)
            if not result.stdout:
                return None
            header, returned_path = result.stdout[:-1].split(b"\t", 1)
            mode, object_type, _object_id = header.decode("ascii").split(" ")
            if returned_path.decode("utf-8") != path:
                return None
            return mode, object_type

    return PinnedWorkspaceReader()


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
