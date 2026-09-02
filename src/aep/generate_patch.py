"""GeneratePatch Task handler composed from governed AEP runtime boundaries."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import PurePosixPath
from typing import Any

from aep.agent_invocation import AgentInvocationContractError, invoke_agent
from aep.agent_resolver import AgentResolutionError, AgentToolDeniedError, resolve_agent
from aep.analyze_issue import (
    AnalyzeIssueContractError,
    AnalyzeIssueTaskHandler,
    _artifact_resource_refs,
    _correlation,
    _failure_class,
    _model_configuration,
    _ref_record,
    _required_ref,
    _spec,
)
from aep.context_builder import ContextBuilderError
from aep.filesystem_tool import FilesystemTool
from aep.generated_artifact_store import GeneratedArtifactStoreError
from aep.git_tool import GitTool
from aep.patch_evaluation import PatchEvaluationContractError, evaluate_patch
from aep.planning_evidence import PlanningEvidenceError, reconcile_dispositions
from aep.resource_loader import Resource, ResourceRef
from aep.runtime_store import RuntimeObject, RuntimeStoreError
from aep.task_execution import FailureClass
from aep.tool_runtime import (
    AuthorizationHook,
    ToolCaller,
    ToolFailureClass,
    ToolRequest,
    ToolResult,
    ToolResultStatus,
)
from aep.workflow_scheduler import TaskExecutionResult


JsonMapping = Mapping[str, Any]


class GeneratePatchContractError(AnalyzeIssueContractError):
    """Raised when GeneratePatch inputs cannot be bound safely."""


class DisallowedPatchPathError(GeneratePatchContractError):
    """Raised before mutation when model output exceeds the plan boundary."""


class GeneratePatchTaskHandler(AnalyzeIssueTaskHandler):
    """Author scoped workspace changes and publish an evaluated patch."""

    task_name = "generate-patch"
    task_label = "GeneratePatch"
    invocation_label = "Code Generator"
    artifact_type = "PATCH"
    artifact_actor = "generate-patch-task-handler"
    runtime_id_namespace = "generate-patch"

    def __init__(
        self,
        *,
        filesystem_tool: FilesystemTool,
        workspace_git_tool: GitTool,
        evaluation_git_tool: GitTool,
        authorize_filesystem: AuthorizationHook,
        authorize_git: AuthorizationHook,
        working_branch: str,
        tool_timeout_ms: int = 5_000,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        if not isinstance(filesystem_tool, FilesystemTool):
            raise TypeError("filesystem_tool must be a FilesystemTool")
        if not isinstance(workspace_git_tool, GitTool):
            raise TypeError("workspace_git_tool must be a GitTool")
        if not isinstance(evaluation_git_tool, GitTool):
            raise TypeError("evaluation_git_tool must be a GitTool")
        if not callable(authorize_filesystem) or not callable(authorize_git):
            raise TypeError("Tool authorization hooks must be callable")
        if not isinstance(working_branch, str) or not working_branch:
            raise ValueError("working_branch must be a non-empty string")
        if (
            not isinstance(tool_timeout_ms, int)
            or isinstance(tool_timeout_ms, bool)
            or tool_timeout_ms < 1
        ):
            raise ValueError("tool_timeout_ms must be a positive integer")
        self._filesystem_tool = filesystem_tool
        self._workspace_git_tool = workspace_git_tool
        self._evaluation_git_tool = evaluation_git_tool
        self._authorize_filesystem = authorize_filesystem
        self._authorize_git = authorize_git
        self._working_branch = working_branch
        self._tool_timeout_ms = tool_timeout_ms

    def execute(
        self, task: Resource, task_execution: RuntimeObject
    ) -> TaskExecutionResult:
        """Run one already-running GeneratePatch attempt."""

        applied: list[dict[str, str]] = []
        targets_by_path: dict[str, JsonMapping] = {}
        filesystem_write_ref: JsonMapping | None = None
        active_invocation_id: str | None = None
        try:
            workflow, event = self._validate_inputs(task, task_execution)
            plan, producer_id = self._implementation_plan(task_execution, workflow)
            allowed_paths = _allowed_paths(plan)
            deletion_authorized_paths = _deletion_authorized_paths(plan, allowed_paths)
            no_change_paths = _no_change_paths(plan, allowed_paths)
            required_insertions = _required_insertions(plan, allowed_paths)
            unsupported_criteria = _unsupported_acceptance_criteria(plan)
            task_spec = _spec(task)
            patch_evaluation = self._patch_evaluation(task_spec)
            output_schema = task_spec.get("outputs")
            if not isinstance(output_schema, Mapping) or not output_schema:
                raise GeneratePatchContractError("GeneratePatch Task requires spec.outputs")

            filesystem_read_ref = self._context_filesystem_ref(task_spec)
            git_read_ref = self._context_git_ref(task_spec)
            clean_result, clean_evidence = self._invoke_git_diff(
                invocation_id=self._runtime_id("toolinvocation", f"{task_execution['id']}:git:preflight"),
                task_execution=task_execution,
                tool_ref=git_read_ref,
                paths=allowed_paths,
                include_ignored=True,
            )
            self._attach(task_execution["id"], {"toolInvocationIds": [clean_evidence["id"]]})
            clean_output = clean_result.output_record() if clean_result.output is not None else {}
            clean_diff = clean_output.get("diff")
            if (
                clean_result.status is not ToolResultStatus.SUCCEEDED
                or clean_output.get("revision") != workflow["repositoryRevision"]
                or clean_output.get("changedFiles")
                or (isinstance(clean_diff, Mapping) and clean_diff.get("byteLength") != 0)
            ):
                raise GeneratePatchContractError(
                    "GeneratePatch requires a clean checkout at the recorded repository revision"
                )
            editable_targets = self._read_editable_targets(
                task_execution=task_execution,
                repository_revision=str(workflow["repositoryRevision"]),
                paths=allowed_paths,
                tool_ref=filesystem_read_ref,
                max_bytes=max(1, int(task_spec["inputContextTokenBudget"]) * 4),
            )
            self._verify_targets_at_revision(
                task_execution=task_execution,
                repository_revision=str(workflow["repositoryRevision"]),
                paths=allowed_paths,
                targets=editable_targets,
                tool_ref=git_read_ref,
                max_bytes=max(1, int(task_spec["inputContextTokenBudget"]) * 4),
            )
            _verify_no_change_targets(
                no_change_paths, required_insertions, editable_targets
            )
            context_package = self._context_builder.build(
                task=task,
                task_execution=task_execution,
                workflow_execution=workflow,
                event=event,
                knowledge_bases=self._resolve_declared(
                    task_spec.get("knowledgeBases", ()), "KnowledgeBase"
                ),
                policies=self._resolve_declared(task_spec.get("policies", ()), "Policy"),
                prior_task_execution_ids=(producer_id,),
                editable_targets=editable_targets,
                created_at=self._timestamp(),
            )
            self._attach(task_execution["id"], {"contextPackageId": context_package["id"]})

            agent_ref = _required_ref(
                task_spec.get("agentRef"), "Agent", "Task.spec.agentRef"
            )
            resolved = resolve_agent(
                task.ref,
                agent_ref,
                self._resources,
                correlation=_correlation(task_execution),
                resolved_at=self._timestamp(),
            ).as_dict()
            saved_agent = self._runtime_store.create(
                resolved,
                deterministic_key=f"resolved-agent:{task_execution['id']}",
            )
            if dict(saved_agent) != resolved:
                raise GeneratePatchContractError(
                    "TaskExecution already has a different ResolvedAgent"
                )
            self._attach(task_execution["id"], {"resolvedAgentId": saved_agent["id"]})
            if dict(saved_agent.get("outputSchema", {})) != dict(output_schema):
                raise GeneratePatchContractError(
                    "GeneratePatch Agent outputSchema must match Task.spec.outputs"
                )
            tools = self._authorized_tools(saved_agent)

            prompt = self._require_resource(
                ResourceRef.from_mapping(dict(saved_agent["promptRef"])), "Prompt"
            )
            model = self._require_resource(
                ResourceRef.from_mapping(dict(saved_agent["modelRef"])), "Model"
            )
            invocation_id = self._runtime_id(
                "agentinvocation", str(task_execution["id"])
            )
            active_invocation_id = invocation_id
            invocation = invoke_agent(
                store=self._runtime_store,
                invocation_id=invocation_id,
                model_invocation_id=self._runtime_id(
                    "modelinvocation", str(task_execution["id"])
                ),
                resolved_agent=saved_agent,
                context_package=context_package,
                prompt=prompt.data,
                model_configuration=_model_configuration(model),
                adapter=self._model_adapter,
                started_at=self._timestamp(),
                completed_at=self._timestamp(),
            )
            self._attach(task_execution["id"], {"agentInvocationIds": [invocation_id]})
            if invocation["status"] != "SUCCEEDED":
                failure = invocation.get("failure", {})
                return TaskExecutionResult.failure(
                    _failure_class(failure.get("class")),
                    str(failure.get("message") or "Code Generator invocation failed"),
                )

            changes = _validated_changes(invocation.get("output"), allowed_paths, editable_targets)
            explicit_dispositions = _explicit_dispositions(invocation.get("output"))
            if explicit_dispositions:
                criteria_by_path = _criteria_by_path(plan)
                try:
                    reconciliation = reconcile_dispositions(
                        plan_id=str(plan.get("artifactId", producer_id)),
                        repository_revision=str(workflow["repositoryRevision"]),
                        original_required_paths=tuple(set(allowed_paths) - set(no_change_paths)),
                        targets=editable_targets, dispositions=explicit_dispositions,
                        criteria_by_path=criteria_by_path,
                        evaluator_ref={"kind": "Evaluation", "name": "plan-reconciliation", "version": "1.0.0"},
                    )
                except PlanningEvidenceError as error:
                    raise GeneratePatchContractError(str(error)) from error
                no_change_paths = tuple(reconciliation["verifiedNoChangePaths"])
            missing = sorted(set(allowed_paths) - set(no_change_paths) - {item["path"] for item in changes})
            if missing:
                raise GeneratePatchContractError(
                    f"Code Generator omitted required planned files: {missing!r}"
                )
            verified_targets = self._read_editable_targets(
                task_execution=task_execution,
                repository_revision=str(workflow["repositoryRevision"]),
                paths=allowed_paths,
                tool_ref=filesystem_read_ref,
                purpose="preimage-verification",
                max_bytes=max(1, int(task_spec["inputContextTokenBudget"]) * 4),
            )
            if any(
                current["exists"] != original["exists"]
                or current["preimageSha256"] != original["preimageSha256"]
                or current["mode"] != original["mode"]
                for current, original in zip(verified_targets, editable_targets, strict=True)
            ):
                raise GeneratePatchContractError(
                    "editable target preimage changed after ContextPackage construction"
                )
            targets_by_path = {item["path"]: item for item in editable_targets}
            filesystem_write_ref = tools["filesystem"]["ref"]
            for index, change in enumerate(changes):
                target = targets_by_path[change["path"]]
                if change["operation"] == "delete" and (
                    not target["exists"]
                    or change["path"] not in deletion_authorized_paths
                ):
                    raise GeneratePatchContractError(
                        f"Code Generator delete for {change['path']!r} is not deletion-authorized"
                    )
                operation = "compare_delete" if change["operation"] == "delete" else "compare_write"
                payload: dict[str, Any] = {
                    "operation": operation, "path": change["path"],
                    "expectedExists": target["exists"], "expectedSha256": target["preimageSha256"],
                    "expectedMode": target["mode"],
                }
                if operation == "compare_write":
                    payload["content"] = change["content"]
                request = ToolRequest(
                    tool_ref=tools["filesystem"]["ref"],
                    input=payload,
                    caller=ToolCaller(kind="AgentInvocation", id=invocation_id),
                    capabilities=("filesystem.write",),
                    timeout_ms=self._tool_timeout_ms,
                    correlation=_correlation(task_execution),
                )
                tool_id = self._runtime_id(
                    "toolinvocation", f"{task_execution['id']}:filesystem:{index}"
                )
                result, evidence = self._filesystem_tool.invoke(
                    invocation_id=tool_id,
                    task_execution_id=str(task_execution["id"]),
                    request=request,
                    authorize=self._authorize_filesystem,
                )
                if result.status is not ToolResultStatus.SUCCEEDED:
                    self._rollback_applied_changes(
                        task_execution=task_execution,
                        invocation_id=invocation_id,
                        tool_ref=tools["filesystem"]["ref"],
                        targets=targets_by_path,
                        applied=applied,
                    )
                    return _tool_failure(result, f"Filesystem write for {change['path']!r}")
                applied.append(change)
                self._attach(task_execution["id"], {"toolInvocationIds": [evidence["id"]]})

            diff_id = self._runtime_id(
                "toolinvocation", f"{task_execution['id']}:git:diff"
            )
            diff_result, diff_evidence = self._invoke_git_diff(
                invocation_id=diff_id,
                task_execution=task_execution,
                tool_ref=tools["git"]["ref"],
                paths=allowed_paths,
            )
            self._attach(task_execution["id"], {"toolInvocationIds": [diff_evidence["id"]]})
            if diff_result.status is not ToolResultStatus.SUCCEEDED:
                self._rollback_applied_changes(task_execution=task_execution, invocation_id=invocation_id, tool_ref=tools["filesystem"]["ref"], targets=targets_by_path, applied=applied)
                return _tool_failure(diff_result, "Git diff")
            diff_output = diff_result.output_record()
            patch = diff_output.get("diff") if isinstance(diff_output, Mapping) else None
            patch_text = patch.get("text") if isinstance(patch, Mapping) else None
            all_no_change = not changes and set(no_change_paths) == set(allowed_paths)
            if not isinstance(patch_text, str) or (not patch_text and not all_no_change):
                self._rollback_applied_changes(task_execution=task_execution, invocation_id=invocation_id, tool_ref=tools["filesystem"]["ref"], targets=targets_by_path, applied=applied)
                return TaskExecutionResult.failure(
                    FailureClass.EVALUATION, "GeneratePatch produced an empty patch"
                )
            changed_files = (
                [] if all_no_change else _changed_paths(diff_output.get("changedFiles"))
            )

            artifact_id = self._runtime_id(
                "generatedartifact", str(task_execution["id"])
            )
            evaluation_id = self._runtime_id(
                "evaluationresult", str(task_execution["id"])
            )
            evaluation_ref = _ref_record(patch_evaluation.ref)
            metadata = {
                "apiVersion": "aep.dev/v1alpha1",
                "kind": "GeneratedArtifact",
                "id": artifact_id,
                "traceId": task_execution["traceId"],
                "createdAt": self._timestamp(),
                "updatedAt": self._timestamp(),
                "provenance": {
                    "actor": self.artifact_actor,
                    "workflowExecutionId": workflow["id"],
                    "taskExecutionId": task_execution["id"],
                    "repositoryRevision": workflow["repositoryRevision"],
                    "resourceRefs": _artifact_resource_refs(
                        saved_agent, evaluation_ref
                    ),
                },
                "taskExecutionId": task_execution["id"],
                "artifactType": "PATCH",
                "repositoryRevision": workflow["repositoryRevision"],
                "mediaType": "text/x-diff; charset=utf-8",
                "contentAddress": "sha256:"
                + sha256(patch_text.encode("utf-8")).hexdigest(),
                "evaluationResultIds": [evaluation_id],
                "changedFiles": changed_files,
            }
            evaluation_result = evaluate_patch(
                store=self._runtime_store,
                git_adapter=self._evaluation_git_tool.adapter,
                authorize_git=self._authorize_git,
                result_id=evaluation_id,
                task_execution_id=str(task_execution["id"]),
                evaluation_ref=evaluation_ref,
                patch_artifact=metadata,
                patch_content=patch_text,
                expected_revision=str(workflow["repositoryRevision"]),
                allowed_paths=allowed_paths,
                required_paths=tuple(path for path in allowed_paths if path not in no_change_paths),
                no_change_paths=no_change_paths,
                deletion_authorized_paths=deletion_authorized_paths,
                required_insertions=tuple(
                    item for item in required_insertions
                    if item["path"] not in no_change_paths
                ),
                unsupported_acceptance_criteria=unsupported_criteria,
                working_branch=self._working_branch,
                correlation=_correlation(task_execution),
                timestamp=self._timestamp(),
                provenance={
                    "actor": "patch-evaluator",
                    "workflowExecutionId": workflow["id"],
                    "taskExecutionId": task_execution["id"],
                    "repositoryRevision": workflow["repositoryRevision"],
                    "resourceRefs": [evaluation_ref, tools["git"]["ref"]],
                },
                git_tool_ref=tools["git"]["ref"],
                git_tool=self._evaluation_git_tool,
                tool_invocation_id=self._runtime_id(
                    "toolinvocation", f"{task_execution['id']}:git:check-patch"
                ),
                timeout_ms=self._tool_timeout_ms,
            )
            evaluation_tool_id = evaluation_result["evidence"]["git"].get(
                "toolInvocationId"
            )
            if isinstance(evaluation_tool_id, str):
                self._attach(
                    task_execution["id"],
                    {"toolInvocationIds": [evaluation_tool_id]},
                )
            self._attach(task_execution["id"], {"evaluationResultIds": [evaluation_id]})
            if evaluation_result["outcome"] != "PASS":
                details = "; ".join(evaluation_result.get("logs", ()))
                self._rollback_applied_changes(task_execution=task_execution, invocation_id=invocation_id, tool_ref=tools["filesystem"]["ref"], targets=targets_by_path, applied=applied)
                return TaskExecutionResult.failure(
                    FailureClass.EVALUATION,
                    f"GeneratePatch failed Patch Evaluation: {details}",
                )
            if changed_files != evaluation_result["evidence"]["changedFiles"]:
                self._rollback_applied_changes(task_execution=task_execution, invocation_id=invocation_id, tool_ref=tools["filesystem"]["ref"], targets=targets_by_path, applied=applied)
                applied.clear()
                raise GeneratePatchContractError(
                    "Git diff and Patch Evaluation changed-file evidence disagree"
                )

            artifact = self._artifact_store.publish(metadata, patch_text)
            self._attach(task_execution["id"], {"generatedArtifactIds": [artifact["id"]]})
            return TaskExecutionResult.success()
        except AgentToolDeniedError as error:
            self._rollback_if_needed(task_execution, active_invocation_id, filesystem_write_ref, targets_by_path, applied)
            return TaskExecutionResult.failure(FailureClass.POLICY, str(error))
        except DisallowedPatchPathError as error:
            self._rollback_if_needed(task_execution, active_invocation_id, filesystem_write_ref, targets_by_path, applied)
            return TaskExecutionResult.failure(FailureClass.POLICY, str(error))
        except (
            AgentResolutionError,
            AgentInvocationContractError,
            AnalyzeIssueContractError,
            ContextBuilderError,
            GeneratedArtifactStoreError,
            PatchEvaluationContractError,
            RuntimeStoreError,
            KeyError,
            TypeError,
            ValueError,
        ) as error:
            self._rollback_if_needed(task_execution, active_invocation_id, filesystem_write_ref, targets_by_path, applied)
            return TaskExecutionResult.failure(FailureClass.CONFIGURATION, str(error))

    def _rollback_if_needed(
        self, task_execution: JsonMapping, invocation_id: str | None,
        tool_ref: JsonMapping | None, targets: Mapping[str, JsonMapping],
        applied: Sequence[Mapping[str, str]],
    ) -> None:
        if applied and invocation_id is not None and tool_ref is not None:
            self._rollback_applied_changes(
                task_execution=task_execution, invocation_id=invocation_id,
                tool_ref=tool_ref, targets=targets, applied=applied,
            )
            if isinstance(applied, list):
                applied.clear()

    def _rollback_applied_changes(
        self,
        *,
        task_execution: JsonMapping,
        invocation_id: str,
        tool_ref: JsonMapping,
        targets: Mapping[str, JsonMapping],
        applied: Sequence[Mapping[str, str]],
    ) -> None:
        """Restore already-mutated files when a later compare-write fails."""

        for index, change in enumerate(reversed(applied)):
            target = targets[change["path"]]
            if change["operation"] == "delete":
                payload = {
                    "operation": "compare_write", "path": change["path"],
                    "content": target["content"], "expectedExists": False,
                    "expectedSha256": sha256(b"").hexdigest(),
                    "expectedMode": None,
                    "mode": target["mode"],
                }
            elif target["exists"]:
                payload = {
                    "operation": "compare_write",
                    "path": change["path"],
                    "content": target["content"],
                    "expectedExists": True,
                    "expectedSha256": sha256(change["content"].encode()).hexdigest(),
                    "expectedMode": target["mode"],
                }
            else:
                payload = {
                    "operation": "compare_delete",
                    "path": change["path"],
                    "expectedExists": True,
                    "expectedSha256": sha256(change["content"].encode()).hexdigest(),
                    "expectedMode": 0o600,
                }
            request = ToolRequest(
                tool_ref=tool_ref,
                input=payload,
                caller=ToolCaller(kind="TaskExecution", id=str(task_execution["id"])),
                capabilities=("filesystem.write",),
                timeout_ms=self._tool_timeout_ms,
                correlation=_correlation(task_execution),
            )
            result, evidence = self._filesystem_tool.invoke(
                invocation_id=self._runtime_id(
                    "toolinvocation", f"{task_execution['id']}:filesystem:rollback:{index}"
                ),
                task_execution_id=str(task_execution["id"]),
                request=request,
                authorize=self._authorize_filesystem,
            )
            self._attach(task_execution["id"], {"toolInvocationIds": [evidence["id"]]})
            if result.status is not ToolResultStatus.SUCCEEDED:
                raise GeneratePatchContractError(
                    f"failed to roll back {change['path']!r} after a write failure"
                )

    def _read_editable_targets(
        self, *, task_execution: JsonMapping, repository_revision: str, paths: Sequence[str],
        tool_ref: JsonMapping,
        max_bytes: int = 4 * 1024 * 1024,
        purpose: str = "editable-target",
    ) -> tuple[dict[str, Any], ...]:
        targets: list[dict[str, Any]] = []
        for index, path in enumerate(paths):
            request = ToolRequest(
                tool_ref=tool_ref,
                input={"operation": "read", "path": path, "maxBytes": max_bytes},
                caller=ToolCaller(kind="ContextBuilder", id=str(task_execution["id"])),
                capabilities=("filesystem.read",),
                timeout_ms=self._tool_timeout_ms,
                correlation=_correlation(task_execution),
            )
            invocation_id = self._runtime_id(
                "toolinvocation", f"{task_execution['id']}:{purpose}:{index}"
            )
            result, evidence = self._filesystem_tool.invoke(
                invocation_id=invocation_id,
                task_execution_id=str(task_execution["id"]),
                request=request,
                authorize=self._authorize_filesystem,
            )
            self._attach(task_execution["id"], {"toolInvocationIds": [evidence["id"]]})
            if (
                result.status is not ToolResultStatus.SUCCEEDED
                and result.failure_class is not ToolFailureClass.NOT_FOUND
            ):
                if result.failure_class is ToolFailureClass.POLICY:
                    raise AgentToolDeniedError(
                        f"editable target read for {path!r} was denied by policy"
                    )
                raise GeneratePatchContractError(
                    f"editable target {path!r} could not be materialized: "
                    f"{result.failure_class.value if result.failure_class else result.status.value}"
                )
            exists = result.status is ToolResultStatus.SUCCEEDED
            output = result.output_record() if exists else {}
            content = output.get("content", "")
            if "\x00" in content:
                raise GeneratePatchContractError(
                    f"editable target {path!r} contains binary NUL bytes"
                )
            digest = output.get("sha256", sha256(b"").hexdigest())
            targets.append({
                "path": path,
                "exists": exists,
                "content": content,
                "preimageSha256": digest,
                "mode": output.get("mode"),
                "repositoryRevision": repository_revision,
                "provenance": {
                    "actor": "context-builder",
                    "taskExecutionId": task_execution["id"],
                    "repositoryRevision": repository_revision,
                    "resourceRefs": [],
                },
            })
        return tuple(targets)

    def _verify_targets_at_revision(
        self, *, task_execution: JsonMapping, repository_revision: str,
        paths: Sequence[str], targets: Sequence[JsonMapping], tool_ref: JsonMapping,
        max_bytes: int,
    ) -> None:
        for index, (path, target) in enumerate(zip(paths, targets, strict=True)):
            request = ToolRequest(
                tool_ref=tool_ref,
                input={"operation": "read_blob", "expectedRevision": repository_revision,
                       "branch": self._working_branch, "paths": [path], "maxBytes": max_bytes},
                caller=ToolCaller(kind="TaskExecution", id=str(task_execution["id"])),
                capabilities=("git.read",), timeout_ms=self._tool_timeout_ms,
                correlation=_correlation(task_execution),
            )
            result, evidence = self._workspace_git_tool.invoke(
                invocation_id=self._runtime_id(
                    "toolinvocation", f"{task_execution['id']}:git:editable-blob:{index}"
                ),
                task_execution_id=str(task_execution["id"]), request=request,
                authorize=self._authorize_git,
            )
            self._attach(task_execution["id"], {"toolInvocationIds": [evidence["id"]]})
            output = result.output_record() if result.output is not None else {}
            blob = output.get("blob")
            if result.status is not ToolResultStatus.SUCCEEDED or not isinstance(blob, Mapping):
                raise GeneratePatchContractError(
                    f"editable target {path!r} could not be bound to the recorded Git revision"
                )
            blob_mode = blob.get("mode")
            target_mode = target.get("mode")
            if (
                blob.get("exists") != target.get("exists")
                or blob.get("sha256") != target.get("preimageSha256")
                or (
                    blob.get("exists")
                    and (not isinstance(blob_mode, int) or not isinstance(target_mode, int)
                         or (blob_mode & 0o111) != (target_mode & 0o111))
                )
            ):
                raise GeneratePatchContractError(
                    f"editable target {path!r} does not match the recorded Git revision"
                )

    def _context_filesystem_ref(self, task_spec: JsonMapping) -> dict[str, str]:
        agent_ref = _required_ref(task_spec.get("agentRef"), "Agent", "Task.spec.agentRef")
        agent = self._require_resource(agent_ref, "Agent")
        matches = []
        for value in _spec(agent).get("toolRefs", ()):
            if isinstance(value, Mapping):
                ref = ResourceRef.from_mapping(dict(value))
                if ref.name == "filesystem":
                    matches.append(ref)
        if len(matches) != 1:
            raise GeneratePatchContractError(
                "GeneratePatch Agent must reference exactly one versioned filesystem Tool"
            )
        self._require_resource(matches[0], "Tool")
        return _ref_record(matches[0])

    def _context_git_ref(self, task_spec: JsonMapping) -> dict[str, str]:
        agent_ref = _required_ref(task_spec.get("agentRef"), "Agent", "Task.spec.agentRef")
        agent = self._require_resource(agent_ref, "Agent")
        matches = []
        for value in _spec(agent).get("toolRefs", ()):
            if isinstance(value, Mapping):
                ref = ResourceRef.from_mapping(dict(value))
                if ref.name == "git":
                    matches.append(ref)
        if len(matches) != 1:
            raise GeneratePatchContractError(
                "GeneratePatch Agent must reference exactly one versioned git Tool"
            )
        self._require_resource(matches[0], "Tool")
        return _ref_record(matches[0])

    def _implementation_plan(
        self, task_execution: JsonMapping, workflow: JsonMapping
    ) -> tuple[dict[str, Any], str]:
        dependencies = task_execution.get("dependencyTaskExecutionIds")
        if (
            isinstance(dependencies, (str, bytes))
            or not isinstance(dependencies, Sequence)
            or len(dependencies) != 1
            or not isinstance(dependencies[0], str)
        ):
            raise GeneratePatchContractError(
                "GeneratePatch requires exactly one dependency TaskExecution"
            )
        producer_id = dependencies[0]
        producer = self._runtime_store.get(producer_id)
        if producer is None or producer.get("status") != "SUCCEEDED":
            raise GeneratePatchContractError(
                "GeneratePatch dependency TaskExecution must be SUCCEEDED"
            )
        producer_ref = producer.get("taskRef")
        if (
            not isinstance(producer_ref, Mapping)
            or producer_ref.get("kind") != "Task"
            or producer_ref.get("name") != "build-implementation-plan"
            or producer_ref.get("version") in {None, "", "latest"}
        ):
            raise GeneratePatchContractError(
                "GeneratePatch dependency must be a versioned build-implementation-plan Task"
            )
        artifacts = self._artifact_store.list_by_task_execution(producer_id)
        plans = [item for item in artifacts if item.get("artifactType") == "IMPLEMENTATION_PLAN"]
        if len(artifacts) != 1 or len(plans) != 1:
            raise GeneratePatchContractError(
                "GeneratePatch requires exactly one prior IMPLEMENTATION_PLAN GeneratedArtifact"
            )
        artifact = plans[0]
        if list(producer.get("generatedArtifactIds", ())) != [artifact.get("id")]:
            raise GeneratePatchContractError(
                "prior IMPLEMENTATION_PLAN is not attached to its producer TaskExecution"
            )
        if artifact.get("repositoryRevision") != workflow.get("repositoryRevision"):
            raise GeneratePatchContractError(
                "prior IMPLEMENTATION_PLAN repository revision does not match WorkflowExecution"
            )
        evaluation_ids = artifact.get("evaluationResultIds")
        if (
            isinstance(evaluation_ids, (str, bytes))
            or not isinstance(evaluation_ids, Sequence)
            or len(evaluation_ids) != 1
            or evaluation_ids[0] not in producer.get("evaluationResultIds", ())
        ):
            raise GeneratePatchContractError(
                "prior IMPLEMENTATION_PLAN must reference its producer EvaluationResult"
            )
        evaluation = self._runtime_store.get(str(evaluation_ids[0]))
        target = evaluation.get("target") if isinstance(evaluation, Mapping) else None
        provenance = evaluation.get("provenance") if isinstance(evaluation, Mapping) else None
        if not (
            isinstance(evaluation, Mapping)
            and evaluation.get("kind") == "EvaluationResult"
            and evaluation.get("status") == "SUCCEEDED"
            and evaluation.get("outcome") == "PASS"
            and evaluation.get("taskExecutionId") == producer_id
            and evaluation.get("traceId") == producer.get("traceId")
            and isinstance(target, Mapping)
            and target.get("type") == "AgentInvocation"
            and target.get("id") in producer.get("agentInvocationIds", ())
            and isinstance(provenance, Mapping)
            and provenance.get("workflowExecutionId") == workflow.get("id")
            and provenance.get("taskExecutionId") == producer_id
            and provenance.get("repositoryRevision") == workflow.get("repositoryRevision")
        ):
            raise GeneratePatchContractError(
                "prior IMPLEMENTATION_PLAN does not have a correlated PASS EvaluationResult"
            )
        raw_content = self._artifact_store.get_content(str(artifact["id"]))
        try:
            content = json.loads(raw_content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GeneratePatchContractError(
                "IMPLEMENTATION_PLAN content must be valid JSON"
            ) from error
        if not isinstance(content, Mapping):
            raise GeneratePatchContractError(
                "IMPLEMENTATION_PLAN content must be a JSON object"
            )
        return deepcopy(dict(content)), producer_id

    def _patch_evaluation(self, task_spec: JsonMapping) -> Resource:
        evaluations = self._resolve_declared(task_spec.get("evaluations", ()), "Evaluation")
        patch_evaluations = [item for item in evaluations if _spec(item).get("type") == "patch"]
        if len(patch_evaluations) != 1:
            raise GeneratePatchContractError(
                "GeneratePatch Task must declare exactly one patch Evaluation"
            )
        return patch_evaluations[0]

    def _authorized_tools(self, resolved_agent: JsonMapping) -> dict[str, dict[str, Any]]:
        records: dict[str, dict[str, Any]] = {}
        for value in resolved_agent.get("toolRefs", ()):
            if not isinstance(value, Mapping):
                raise GeneratePatchContractError("ResolvedAgent.toolRefs must contain references")
            ref = ResourceRef.from_mapping(dict(value))
            if ref.name not in {"filesystem", "git"}:
                raise GeneratePatchContractError(
                    f"GeneratePatch ResolvedAgent may not use Tool {ref.name!r}"
                )
            tool = self._require_resource(ref, "Tool")
            capabilities = _spec(tool).get("capabilities")
            if isinstance(capabilities, (str, bytes)) or not isinstance(capabilities, Sequence):
                raise GeneratePatchContractError(
                    f"Tool {ref.name!r} must declare capabilities"
                )
            records[ref.name] = {"ref": _ref_record(ref), "capabilities": tuple(capabilities)}
        required = {"filesystem": "filesystem.write", "git": "git.read"}
        for name, capability in required.items():
            if name not in records or capability not in records[name]["capabilities"]:
                raise GeneratePatchContractError(
                    f"GeneratePatch ResolvedAgent must allow {capability}"
                )
        return records

    def _invoke_git_diff(
        self,
        *,
        invocation_id: str,
        task_execution: JsonMapping,
        tool_ref: JsonMapping,
        paths: Sequence[str] = (),
        include_ignored: bool = False,
    ) -> tuple[ToolResult, RuntimeObject]:
        request = ToolRequest(
            tool_ref=tool_ref,
            input={
                "operation": "diff",
                "expectedRevision": task_execution["provenance"]["repositoryRevision"],
                "branch": self._working_branch,
                "paths": list(paths),
                "includeIgnored": include_ignored,
            },
            caller=ToolCaller(kind="TaskExecution", id=str(task_execution["id"])),
            capabilities=("git.read",),
            timeout_ms=self._tool_timeout_ms,
            correlation=_correlation(task_execution),
        )
        return self._workspace_git_tool.invoke(
            invocation_id=invocation_id,
            task_execution_id=str(task_execution["id"]),
            request=request,
            authorize=self._authorize_git,
        )


def _allowed_paths(plan: JsonMapping) -> tuple[str, ...]:
    values = plan.get("intendedFiles")
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence) or not values:
        raise GeneratePatchContractError(
            "IMPLEMENTATION_PLAN.intendedFiles must contain allowed paths"
        )
    paths = tuple(str(value) for value in values)
    if len(set(paths)) != len(paths) or any(not _safe_path(path) for path in paths):
        raise GeneratePatchContractError(
            "IMPLEMENTATION_PLAN.intendedFiles must contain unique normalized repository-relative paths"
        )
    return tuple(sorted(paths, key=lambda value: (value.casefold(), value)))


def _deletion_authorized_paths(
    plan: JsonMapping, allowed_paths: Sequence[str]
) -> tuple[str, ...]:
    values = plan.get("deletionAuthorizedFiles", ())
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise GeneratePatchContractError(
            "IMPLEMENTATION_PLAN.deletionAuthorizedFiles must be an array"
        )
    paths = tuple(str(value) for value in values)
    if len(set(paths)) != len(paths) or any(not _safe_path(path) for path in paths):
        raise GeneratePatchContractError(
            "IMPLEMENTATION_PLAN.deletionAuthorizedFiles must contain unique normalized paths"
        )
    if not set(paths).issubset(allowed_paths):
        raise GeneratePatchContractError(
            "IMPLEMENTATION_PLAN.deletionAuthorizedFiles must be a subset of intendedFiles"
        )
    return tuple(sorted(paths, key=lambda value: (value.casefold(), value)))


def _required_insertions(
    plan: JsonMapping, allowed_paths: Sequence[str]
) -> tuple[dict[str, str], ...]:
    values = plan.get("requiredInsertions", ())
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise GeneratePatchContractError("IMPLEMENTATION_PLAN.requiredInsertions must be an array")
    records: list[dict[str, str]] = []
    for value in values:
        if not isinstance(value, Mapping):
            raise GeneratePatchContractError(
                "IMPLEMENTATION_PLAN.requiredInsertions must contain path/value objects"
            )
        path, token = value.get("path"), value.get("value")
        if (
            not isinstance(path, str) or path not in allowed_paths
            or not isinstance(token, str) or not token
        ):
            raise GeneratePatchContractError(
                "IMPLEMENTATION_PLAN.requiredInsertions must bind values to intendedFiles"
            )
        records.append({"path": path, "value": token})
    if len({(item["path"], item["value"]) for item in records}) != len(records):
        raise GeneratePatchContractError("IMPLEMENTATION_PLAN.requiredInsertions must be unique")
    return tuple(records)


def _no_change_paths(plan: JsonMapping, allowed_paths: Sequence[str]) -> tuple[str, ...]:
    values = plan.get("noChangeFiles", ())
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise GeneratePatchContractError("IMPLEMENTATION_PLAN.noChangeFiles must be an array")
    paths = tuple(str(value) for value in values)
    if len(set(paths)) != len(paths) or any(not _safe_path(path) for path in paths) or not set(paths).issubset(allowed_paths):
        raise GeneratePatchContractError("IMPLEMENTATION_PLAN.noChangeFiles must be unique intendedFiles")
    return tuple(sorted(paths, key=lambda value: (value.casefold(), value)))


def _unsupported_acceptance_criteria(plan: JsonMapping) -> tuple[str, ...]:
    values = plan.get("unsupportedAcceptanceCriteria", ())
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence) or any(not isinstance(value, str) or not value for value in values):
        raise GeneratePatchContractError("IMPLEMENTATION_PLAN.unsupportedAcceptanceCriteria must contain non-empty strings")
    return tuple(dict.fromkeys(values))


def _validated_changes(
    output: object,
    allowed_paths: Sequence[str],
    editable_targets: Sequence[JsonMapping],
) -> tuple[dict[str, str], ...]:
    if not isinstance(output, Mapping):
        raise GeneratePatchContractError("Code Generator output must be an object")
    values = output.get("changes")
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise GeneratePatchContractError("Code Generator output must contain changes")
    changes: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, Mapping):
            raise GeneratePatchContractError("each Code Generator change must be an object")
        path, content = value.get("path"), value.get("content")
        operation = value.get("operation", "write")
        if not isinstance(path, str) or not _safe_path(path):
            raise GeneratePatchContractError("Code Generator change path is unsafe")
        if operation not in {"write", "delete"}:
            raise GeneratePatchContractError(f"Code Generator operation for {path!r} is invalid")
        if operation == "write" and not isinstance(content, str):
            raise GeneratePatchContractError(f"Code Generator content for {path!r} must be text")
        if operation == "write" and "\x00" in content:
            raise GeneratePatchContractError(
                f"Code Generator content for {path!r} contains binary NUL bytes"
            )
        if operation == "delete" and content not in {None, ""}:
            raise GeneratePatchContractError(f"Code Generator delete for {path!r} must not include content")
        if path in seen:
            raise GeneratePatchContractError(f"Code Generator repeats path {path!r}")
        if not any(path == rule or path.startswith(f"{rule}/") for rule in allowed_paths):
            raise DisallowedPatchPathError(
                f"Code Generator path {path!r} is outside IMPLEMENTATION_PLAN.intendedFiles"
            )
        target = next((item for item in editable_targets if item.get("path") == path), None)
        if target is None or value.get("preimageSha256") != target.get("preimageSha256"):
            raise GeneratePatchContractError(
                f"Code Generator change for {path!r} is not bound to the supplied preimage"
            )
        seen.add(path)
        changes.append({"path": path, "content": content or "", "operation": operation})
    return tuple(changes)


def _explicit_dispositions(output: object) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(output, Mapping) or "dispositions" not in output:
        return ()
    values = output["dispositions"]
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise GeneratePatchContractError("Code Generator dispositions must be an array")
    return tuple(values)


def _criteria_by_path(plan: JsonMapping) -> dict[str, tuple[Mapping[str, Any], ...]]:
    result: dict[str, list[Mapping[str, Any]]] = {}
    for item in plan.get("pathEvidence", ()):
        if not isinstance(item, Mapping) or not isinstance(item.get("path"), str):
            continue
        predicates = [entry.get("predicate") for entry in item.get("predicateResults", ()) if isinstance(entry, Mapping) and isinstance(entry.get("predicate"), Mapping)]
        result.setdefault(item["path"], []).extend(predicates)
    # Older generations express deterministic satisfaction as required insertions.
    for item in plan.get("requiredInsertions", ()):
        if isinstance(item, Mapping) and isinstance(item.get("path"), str) and isinstance(item.get("value"), str):
            result.setdefault(item["path"], []).append({"kind": "TEXT_PRESENT", "value": item["value"]})
    return {path: tuple(values) for path, values in result.items()}


def _verify_no_change_targets(
    no_change_paths: Sequence[str],
    required_insertions: Sequence[Mapping[str, str]],
    editable_targets: Sequence[JsonMapping],
) -> None:
    for path in no_change_paths:
        criteria = [item["value"] for item in required_insertions if item["path"] == path]
        target = next((item for item in editable_targets if item.get("path") == path), None)
        content = target.get("content") if isinstance(target, Mapping) else None
        if (
            not criteria
            or not isinstance(content, str)
            or any(value not in content for value in criteria)
        ):
            raise GeneratePatchContractError(
                f"no-change target {path!r} is not deterministically satisfied by its exact editable content"
            )


def _changed_paths(values: object) -> list[str]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise GeneratePatchContractError("Git diff returned malformed changed-file evidence")
    paths = {
        path
        for value in values
        if isinstance(value, Mapping)
        for path in (value.get("previousPath"), value.get("path"))
        if isinstance(path, str)
    }
    if not paths:
        raise GeneratePatchContractError("Git diff returned no changed-file evidence")
    return sorted(paths, key=lambda path: (path.casefold(), path))


def _safe_path(value: str) -> bool:
    if not value or "\\" in value:
        return False
    path = PurePosixPath(value)
    return (
        value == path.as_posix()
        and not path.is_absolute()
        and all(part not in {"", ".", ".."} for part in path.parts)
        and (not path.parts or path.parts[0].casefold() != ".git")
    )


def _tool_failure(result: ToolResult, operation: str) -> TaskExecutionResult:
    failure = result.failure_class or ToolFailureClass.ADAPTER
    return TaskExecutionResult.failure(
        _runtime_tool_failure(failure),
        f"{operation} failed: {result.failure_message or result.status.value}",
    )


def _runtime_tool_failure(value: ToolFailureClass) -> FailureClass:
    return {
        ToolFailureClass.VALIDATION: FailureClass.CONFIGURATION,
        ToolFailureClass.POLICY: FailureClass.POLICY,
        ToolFailureClass.TIMEOUT: FailureClass.RECOVERABLE,
        ToolFailureClass.IO: FailureClass.RECOVERABLE,
        ToolFailureClass.BOUNDARY: FailureClass.POLICY,
        ToolFailureClass.NOT_FOUND: FailureClass.PERMANENT,
        ToolFailureClass.STARTUP: FailureClass.RECOVERABLE,
        ToolFailureClass.NONZERO_EXIT: FailureClass.PERMANENT,
        ToolFailureClass.ADAPTER: FailureClass.PERMANENT,
    }[value]
