"""Policy-gated, retry-safe CreatePullRequest Task handler."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from hashlib import sha256
import json
from typing import Any
from uuid import uuid4

from aep.analyze_issue import _correlation, _ref_record, _spec
from aep.capability_policy import (
    ApplicablePolicy,
    CapabilityPolicyContractError,
    PolicyScope,
    PreExecutionCapabilityPolicy,
)
from aep.generated_artifact_store import (
    GeneratedArtifactStore,
    GeneratedArtifactStoreError,
)
from aep.git_tool import (
    GitInvocationIdentityConflictError,
    GitInvocationInProgressError,
    GitTool,
)
from aep.github_tool import (
    CREATE_PR_CAPABILITY,
    CREATE_PULL_REQUEST,
    GitHubToolAdapter,
    PersistedPublicationPolicyVerifier,
    invoke_github_tool,
)
from aep.publication_policy import PublicationPolicy, PublicationPolicyContractError
from aep.resource_loader import Resource, ResourceCollection, ResourceRef
from aep.runtime_store import RuntimeObject, RuntimeObjectStore, StatusConflictError
from aep.runtime_validation import is_rfc3339_timestamp
from aep.task_execution import FailureClass
from aep.tool_runtime import (
    ToolCaller,
    ToolFailureClass,
    ToolMetrics,
    ToolRequest,
    ToolResult,
    ToolResultStatus,
)
from aep.workflow_scheduler import TaskExecutionResult


JsonMapping = Mapping[str, Any]
Clock = Callable[[], str]
EventResolver = Callable[[str], JsonMapping | None]

GIT_PUSH_CAPABILITY = "git.push"
EXPECTED_ARTIFACT_TYPES = (
    "ISSUE_ANALYSIS",
    "IMPLEMENTATION_PLAN",
    "PATCH",
    "EVALUATION_REPORT",
)


class CreatePullRequestContractError(ValueError):
    """Raised when publication cannot be bound to immutable workflow inputs."""


class GitHubInvocationIdentityConflictError(CreatePullRequestContractError):
    """Raised when a persisted GitHub invocation identity changes inputs."""


class GitHubInvocationInProgressError(RuntimeError):
    """Raised rather than repeating an ambiguous in-flight publication."""


class CreatePullRequestTaskHandler:
    """Publish one accepted execution without ever merging its pull request."""

    task_name = "create-pull-request"
    runtime_id_namespace = "create-pull-request"

    def __init__(
        self,
        *,
        resources: ResourceCollection,
        runtime_store: RuntimeObjectStore,
        artifact_store: GeneratedArtifactStore,
        git_tool: GitTool,
        github_adapter: GitHubToolAdapter,
        event_resolver: EventResolver,
        clock: Clock,
    ) -> None:
        if not isinstance(resources, ResourceCollection):
            raise TypeError("resources must be a ResourceCollection")
        if not isinstance(artifact_store, GeneratedArtifactStore):
            raise TypeError("artifact_store must implement GeneratedArtifactStore")
        if not isinstance(git_tool, GitTool):
            raise TypeError("git_tool must be a GitTool")
        if not isinstance(github_adapter, GitHubToolAdapter):
            raise TypeError("github_adapter must be a GitHubToolAdapter")
        if not callable(event_resolver) or not callable(clock):
            raise TypeError("event_resolver and clock must be callable")
        self._resources = resources
        self._runtime_store = runtime_store
        self._artifact_store = artifact_store
        self._git_tool = git_tool
        self._github_adapter = github_adapter
        self._event_resolver = event_resolver
        self._clock = clock
        self._publication_policy = PublicationPolicy(runtime_store)
        self._capability_policy = PreExecutionCapabilityPolicy(runtime_store)

    def execute(
        self, task: Resource, task_execution: RuntimeObject
    ) -> TaskExecutionResult:
        try:
            workflow = self._validate_inputs(task, task_execution)
            configuration = self._configuration(task, task_execution, workflow)
            evidence = self._accepted_evidence(task_execution, workflow)
            issue = self._issue(workflow, configuration["repository"])
            title, body = self._description(issue, evidence)
            push_policy = self._evaluate_capability(
                task_execution,
                workflow,
                configuration,
                capability=GIT_PUSH_CAPABILITY,
                tool=configuration["gitTool"],
                resource_scope={
                    "repository": configuration["repository"],
                    "branch": configuration["head"],
                    "repositoryRevision": workflow["repositoryRevision"],
                    "toolRef": _ref_record(configuration["gitTool"].ref),
                },
                policies=configuration["capabilityPolicies"],
            )
            self._attach(
                task_execution["id"],
                {"policyDecisionIds": [push_policy["id"]]},
            )
            if push_policy["decision"] != "ALLOW":
                return TaskExecutionResult.failure(
                    FailureClass.POLICY,
                    f"git.push {push_policy['decision']}: {push_policy['reason']}",
                )

            commit_id = self._runtime_id(
                "toolinvocation", f"{task_execution['id']}:git-commit"
            )
            patch_artifact = next(
                item
                for item in evidence["artifacts"]
                if item.get("artifactType") == "PATCH"
            )
            patch_address = str(patch_artifact.get("contentAddress", ""))
            algorithm, separator, patch_digest = patch_address.partition(":")
            if not (
                algorithm == "sha256"
                and separator == ":"
                and len(patch_digest) == 64
            ):
                raise CreatePullRequestContractError(
                    "accepted PATCH requires a SHA-256 content address"
                )
            commit_request = ToolRequest(
                tool_ref=_ref_record(configuration["gitTool"].ref),
                input={
                    "operation": "commit_changes",
                    "expectedRevision": workflow["repositoryRevision"],
                    "branch": configuration["head"],
                    "commitMessage": (
                        f"Implement issue #{issue['number']}: {issue['title']}"
                    )[:256],
                    "expectedPatchSha256": patch_digest,
                },
                caller=ToolCaller("TaskExecution", str(task_execution["id"])),
                capabilities=(GIT_PUSH_CAPABILITY,),
                timeout_ms=configuration["timeoutMs"],
                correlation=_correlation(task_execution),
            )
            commit_result, commit_invocation = self._git_tool.invoke(
                invocation_id=commit_id,
                task_execution_id=str(task_execution["id"]),
                request=commit_request,
                authorize=self._authorization(push_policy, commit_request),
                policy_decision_id=str(push_policy["id"]),
            )
            self._attach(
                task_execution["id"],
                {"toolInvocationIds": [commit_invocation["id"]]},
            )
            if commit_result.status is not ToolResultStatus.SUCCEEDED:
                return _tool_failure("Git commit", commit_result)
            head_revision = commit_invocation.get("output", {}).get("revision")
            if not (
                isinstance(head_revision, str)
                and len(head_revision) == 40
                and head_revision != workflow["repositoryRevision"]
            ):
                return TaskExecutionResult.failure(
                    FailureClass.PERMANENT,
                    "Git commit did not produce a distinct immutable head revision",
                )

            push_id = self._runtime_id(
                "toolinvocation", f"{task_execution['id']}:git-push"
            )
            candidate_action = {
                "action": CREATE_PR_CAPABILITY,
                "target": {
                    "repository": configuration["repository"],
                    "head": configuration["head"],
                    "base": configuration["base"],
                    "repositoryRevision": workflow["repositoryRevision"],
                    "headRevision": head_revision,
                    "commitToolInvocationId": commit_invocation["id"],
                    "pushToolInvocationId": push_id,
                },
            }
            publication = self._evaluate_publication(
                task,
                task_execution,
                workflow,
                configuration,
                evidence,
                candidate_action,
            )
            self._attach(
                task_execution["id"],
                {"policyDecisionIds": [publication["id"]]},
            )
            if publication["decision"] != "ALLOW":
                return TaskExecutionResult.failure(
                    FailureClass.POLICY,
                    f"Publication Policy {publication['decision']}: {publication['reason']}",
                )

            push_request = ToolRequest(
                tool_ref=_ref_record(configuration["gitTool"].ref),
                input={
                    "operation": "push_branch",
                    "expectedRevision": workflow["repositoryRevision"],
                    "branch": configuration["head"],
                },
                caller=ToolCaller("TaskExecution", str(task_execution["id"])),
                capabilities=(GIT_PUSH_CAPABILITY,),
                timeout_ms=configuration["timeoutMs"],
                correlation=_correlation(task_execution),
            )
            push_result, push_invocation = self._git_tool.invoke(
                invocation_id=push_id,
                task_execution_id=str(task_execution["id"]),
                request=push_request,
                authorize=self._authorization(push_policy, push_request),
                policy_decision_id=str(push_policy["id"]),
            )
            self._attach(
                task_execution["id"], {"toolInvocationIds": [push_invocation["id"]]}
            )
            if push_result.status is not ToolResultStatus.SUCCEEDED:
                return _tool_failure("Git push", push_result)
            if not (
                push_invocation.get("output", {}).get("remoteMutationState")
                == "CONFIRMED"
                and push_invocation.get("output", {}).get("revision")
                == head_revision
            ):
                return TaskExecutionResult.failure(
                    FailureClass.PERMANENT,
                    "Git push did not confirm the committed head revision",
                )

            github_policy = self._evaluate_capability(
                task_execution,
                workflow,
                configuration,
                capability=CREATE_PR_CAPABILITY,
                tool=configuration["githubTool"],
                resource_scope={
                    "repository": configuration["repository"],
                    "head": configuration["head"],
                    "base": configuration["base"],
                    "repositoryRevision": workflow["repositoryRevision"],
                    "toolRef": _ref_record(configuration["githubTool"].ref),
                },
                policies=configuration["capabilityPolicies"],
            )
            self._attach(
                task_execution["id"],
                {"policyDecisionIds": [github_policy["id"]]},
            )
            if github_policy["decision"] != "ALLOW":
                return TaskExecutionResult.failure(
                    FailureClass.POLICY,
                    f"github.create_pr {github_policy['decision']}: {github_policy['reason']}",
                )

            github_request = ToolRequest(
                tool_ref=_ref_record(configuration["githubTool"].ref),
                input={
                    "operation": CREATE_PULL_REQUEST,
                    "repository": configuration["repository"],
                    "issueNumber": issue["number"],
                    "head": configuration["head"],
                    "base": configuration["base"],
                    "title": title,
                    "body": body,
                    "publicationEvidence": {
                        "policyDecisionId": publication["id"],
                        "taskExecutionId": task_execution["id"],
                        "workflowExecutionId": workflow["id"],
                        "repositoryRevision": workflow["repositoryRevision"],
                        "headRevision": head_revision,
                        "evaluationResultIds": evidence["evaluationIds"],
                        "generatedArtifactIds": evidence["artifactIds"],
                        "commitToolInvocationId": commit_invocation["id"],
                        "pushToolInvocationId": push_invocation["id"],
                    },
                },
                caller=ToolCaller("TaskExecution", str(task_execution["id"])),
                capabilities=(CREATE_PR_CAPABILITY,),
                timeout_ms=configuration["timeoutMs"],
                correlation=_correlation(task_execution),
            )
            github_id = self._runtime_id(
                "toolinvocation", f"{task_execution['id']}:github-create-pr"
            )
            github_result, github_invocation = self._invoke_github_persisted(
                invocation_id=github_id,
                task_execution=task_execution,
                workflow=workflow,
                request=github_request,
                policy_decision_id=str(github_policy["id"]),
                publication_policy_decision_id=str(publication["id"]),
                authorize=self._authorization(github_policy, github_request),
            )
            self._attach(
                task_execution["id"],
                {"toolInvocationIds": [github_invocation["id"]]},
            )
            if github_result.status is not ToolResultStatus.SUCCEEDED:
                return _tool_failure("GitHub pull-request creation", github_result)

            artifact = self._publish_description(
                task,
                task_execution,
                workflow,
                configuration,
                evidence,
                title,
                body,
                publication,
                push_policy,
                github_policy,
                commit_invocation,
                push_invocation,
                github_invocation,
            )
            self._attach(
                task_execution["id"],
                {"generatedArtifactIds": [artifact["id"]]},
            )
            return TaskExecutionResult.success()
        except GitHubInvocationInProgressError as error:
            return TaskExecutionResult.failure(FailureClass.RECOVERABLE, str(error))
        except GitInvocationInProgressError as error:
            return TaskExecutionResult.failure(FailureClass.RECOVERABLE, str(error))
        except GeneratedArtifactStoreError as error:
            return TaskExecutionResult.failure(FailureClass.RECOVERABLE, str(error))
        except (
            CapabilityPolicyContractError,
            CreatePullRequestContractError,
            GitHubInvocationIdentityConflictError,
            GitInvocationIdentityConflictError,
            PublicationPolicyContractError,
            KeyError,
            TypeError,
            ValueError,
        ) as error:
            return TaskExecutionResult.failure(FailureClass.CONFIGURATION, str(error))

    def _validate_inputs(
        self, task: Resource, task_execution: RuntimeObject
    ) -> RuntimeObject:
        if not isinstance(task, Resource) or task.kind != "Task":
            raise CreatePullRequestContractError("task must be a loaded Task Resource")
        if task.name != self.task_name:
            raise CreatePullRequestContractError(
                "CreatePullRequest handler requires Task create-pull-request"
            )
        if not isinstance(task_execution, Mapping):
            raise CreatePullRequestContractError("task_execution must be a mapping")
        if dict(task_execution.get("taskRef", {})) != _ref_record(task.ref):
            raise CreatePullRequestContractError(
                "TaskExecution.taskRef does not match Task"
            )
        if (
            task_execution.get("kind") != "TaskExecution"
            or task_execution.get("status") != "RUNNING"
        ):
            raise CreatePullRequestContractError("TaskExecution must be RUNNING")
        workflow = self._runtime_store.get(
            str(task_execution.get("workflowExecutionId"))
        )
        if not (
            isinstance(workflow, Mapping)
            and workflow.get("kind") == "WorkflowExecution"
            and workflow.get("status") == "RUNNING"
            and workflow.get("traceId") == task_execution.get("traceId")
            and isinstance(workflow.get("repositoryRevision"), str)
            and len(str(workflow["repositoryRevision"])) == 40
        ):
            raise CreatePullRequestContractError(
                "WorkflowExecution is not a correlated running revision"
            )
        return workflow

    def _configuration(
        self, task: Resource, task_execution: JsonMapping, workflow: JsonMapping
    ) -> dict[str, Any]:
        publication = _spec(task).get("publication")
        if not isinstance(publication, Mapping):
            raise CreatePullRequestContractError(
                "CreatePullRequest Task requires spec.publication"
            )
        git_tool = self._resource(
            publication.get("gitToolRef"), "Tool", "publication.gitToolRef"
        )
        github_tool = self._resource(
            publication.get("githubToolRef"), "Tool", "publication.githubToolRef"
        )
        if git_tool.name != "git" or GIT_PUSH_CAPABILITY not in _spec(git_tool).get(
            "capabilities", ()
        ):
            raise CreatePullRequestContractError(
                "configured Git Tool must be named git and declare git.push"
            )
        if CREATE_PR_CAPABILITY not in _spec(github_tool).get("capabilities", ()):
            raise CreatePullRequestContractError(
                "configured GitHub Tool must declare github.create_pr"
            )
        timeout_ms = publication.get("timeoutMs")
        if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) or timeout_ms < 1:
            raise CreatePullRequestContractError("publication.timeoutMs must be positive")

        repository_spec = _spec(self._resources.workspace).get("repository")
        if not isinstance(repository_spec, Mapping):
            raise CreatePullRequestContractError(
                "Workspace.spec.repository must be configured"
            )
        owner = repository_spec.get("owner")
        name = repository_spec.get("name")
        base = repository_spec.get("defaultBranch")
        head = task_execution.get("workingBranch")
        if not all(isinstance(value, str) and value for value in (owner, name, base, head)):
            raise CreatePullRequestContractError(
                "repository owner, name, default branch, and workingBranch are required"
            )
        repository = f"{owner}/{name}"
        if head == base:
            raise CreatePullRequestContractError(
                "workingBranch must differ from the repository default branch"
            )

        publication_policies: list[ApplicablePolicy] = []
        capability_policies: list[ApplicablePolicy] = []
        policy_values = _spec(task).get("policies", ())
        if isinstance(policy_values, (str, bytes)) or not isinstance(
            policy_values, Sequence
        ):
            raise CreatePullRequestContractError("Task.spec.policies must be an array")
        for value in policy_values:
            policy = self._resource(value, "Policy", "Task.spec.policies")
            policy_type = _spec(policy).get("type")
            binding = ApplicablePolicy(PolicyScope.TASK, policy.data)
            if policy_type == "publication":
                publication_policies.append(binding)
            elif policy_type == "pre-execution-capability":
                capability_policies.append(binding)
            else:
                raise CreatePullRequestContractError(
                    f"unsupported CreatePullRequest Policy type {policy_type!r}"
                )
        if not publication_policies or not capability_policies:
            raise CreatePullRequestContractError(
                "CreatePullRequest requires publication and capability Policies"
            )
        return {
            "repository": repository,
            "base": base,
            "head": head,
            "timeoutMs": timeout_ms,
            "gitTool": git_tool,
            "githubTool": github_tool,
            "publicationPolicies": tuple(publication_policies),
            "capabilityPolicies": tuple(capability_policies),
        }

    def _accepted_evidence(
        self, task_execution: JsonMapping, workflow: JsonMapping
    ) -> dict[str, Any]:
        dependencies = _string_list(
            task_execution.get("dependencyTaskExecutionIds"),
            "CreatePullRequest dependencyTaskExecutionIds",
        )
        if len(dependencies) != 1:
            raise CreatePullRequestContractError(
                "CreatePullRequest requires exactly one dependency TaskExecution"
            )
        acceptance_task = self._runtime_store.get(dependencies[0])
        if not (
            isinstance(acceptance_task, Mapping)
            and acceptance_task.get("kind") == "TaskExecution"
            and acceptance_task.get("status") == "SUCCEEDED"
            and acceptance_task.get("workflowExecutionId") == workflow.get("id")
            and acceptance_task.get("traceId") == workflow.get("traceId")
            and acceptance_task.get("taskRef", {}).get("name")
            == "evaluate-acceptance"
            and acceptance_task.get("taskRef", {}).get("version")
            not in {None, "", "latest"}
        ):
            raise CreatePullRequestContractError(
                "dependency must be a correlated successful EvaluateAcceptance Task"
            )
        acceptance_ids = _string_list(
            acceptance_task.get("evaluationResultIds"),
            "EvaluateAcceptance evaluationResultIds",
        )
        if len(acceptance_ids) != 1:
            raise CreatePullRequestContractError(
                "EvaluateAcceptance must attach exactly one final EvaluationResult"
            )
        acceptance = self._runtime_store.get(acceptance_ids[0])
        acceptance_evidence = (
            acceptance.get("evidence") if isinstance(acceptance, Mapping) else None
        )
        provenance = (
            acceptance.get("provenance") if isinstance(acceptance, Mapping) else None
        )
        if not (
            isinstance(acceptance, Mapping)
            and acceptance.get("kind") == "EvaluationResult"
            and acceptance.get("taskExecutionId") == acceptance_task.get("id")
            and acceptance.get("traceId") == workflow.get("traceId")
            and acceptance.get("status") == "SUCCEEDED"
            and acceptance.get("outcome") == "PASS"
            and isinstance(provenance, Mapping)
            and provenance.get("workflowExecutionId") == workflow.get("id")
            and provenance.get("repositoryRevision")
            == workflow.get("repositoryRevision")
            and isinstance(acceptance_evidence, Mapping)
            and acceptance_evidence.get("type") == "acceptance-summary"
            and not acceptance_evidence.get("issues")
        ):
            raise CreatePullRequestContractError(
                "final acceptance EvaluationResult is not a correlated PASS"
            )
        artifact_ids = _string_list(
            acceptance_evidence.get("requiredArtifactIds"),
            "acceptance requiredArtifactIds",
        )
        prior_evaluation_ids = _string_list(
            acceptance_evidence.get("requiredEvaluationResultIds"),
            "acceptance requiredEvaluationResultIds",
        )
        evaluation_ids = (*prior_evaluation_ids, str(acceptance["id"]))
        artifacts: list[RuntimeObject] = []
        artifact_content: dict[str, Any] = {}
        for artifact_id in artifact_ids:
            artifact = self._artifact_store.get(artifact_id)
            if artifact is None:
                raise CreatePullRequestContractError(
                    f"required GeneratedArtifact {artifact_id!r} was not found"
                )
            artifacts.append(artifact)
            artifact_type = str(artifact["artifactType"])
            if artifact_type != "PATCH":
                try:
                    artifact_content[artifact_type] = json.loads(
                        self._artifact_store.get_content(artifact_id)
                    )
                except (GeneratedArtifactStoreError, json.JSONDecodeError) as error:
                    raise CreatePullRequestContractError(
                        f"required GeneratedArtifact {artifact_id!r} has invalid JSON content"
                    ) from error
        if tuple(item.get("artifactType") for item in artifacts) != EXPECTED_ARTIFACT_TYPES:
            raise CreatePullRequestContractError(
                f"accepted artifacts must be ordered as {EXPECTED_ARTIFACT_TYPES!r}"
            )
        evaluations: list[RuntimeObject] = []
        for evaluation_id in evaluation_ids:
            evaluation = self._runtime_store.get(evaluation_id)
            if not (
                isinstance(evaluation, Mapping)
                and evaluation.get("kind") == "EvaluationResult"
                and evaluation.get("status") == "SUCCEEDED"
                and evaluation.get("outcome") == "PASS"
            ):
                raise CreatePullRequestContractError(
                    f"required EvaluationResult {evaluation_id!r} did not pass"
                )
            evaluations.append(evaluation)
        prior_decisions: list[RuntimeObject] = []
        for predecessor_id in acceptance_evidence.get(
            "predecessorTaskExecutionIds", ()
        ):
            predecessor = self._runtime_store.get(str(predecessor_id))
            if not isinstance(predecessor, Mapping):
                raise CreatePullRequestContractError(
                    f"acceptance predecessor {predecessor_id!r} was not found"
                )
            for decision_id in predecessor.get("policyDecisionIds", ()):
                decision = self._runtime_store.get(str(decision_id))
                if not isinstance(decision, Mapping):
                    raise CreatePullRequestContractError(
                        f"prior PolicyDecision {decision_id!r} was not found"
                    )
                prior_decisions.append(decision)
        return {
            "acceptance": acceptance,
            "acceptanceTask": acceptance_task,
            "artifactIds": list(artifact_ids),
            "artifacts": artifacts,
            "content": artifact_content,
            "evaluationIds": list(evaluation_ids),
            "evaluations": evaluations,
            "priorDecisions": prior_decisions,
        }

    def _issue(self, workflow: JsonMapping, repository: str) -> dict[str, Any]:
        event_id = workflow.get("eventId")
        event = self._event_resolver(str(event_id)) if isinstance(event_id, str) else None
        if not isinstance(event, Mapping):
            raise CreatePullRequestContractError("triggering Event was not found")
        event_repository = event.get("repository")
        issue = event.get("issue")
        if not (
            isinstance(event_repository, Mapping)
            and event_repository.get("full_name") == repository
            and isinstance(issue, Mapping)
            and isinstance(issue.get("number"), int)
            and not isinstance(issue.get("number"), bool)
            and issue.get("number", 0) > 0
            and isinstance(issue.get("title"), str)
            and issue.get("title")
        ):
            raise CreatePullRequestContractError(
                "triggering Event issue does not match the configured repository"
            )
        return {"number": issue["number"], "title": issue["title"]}

    def _description(
        self, issue: JsonMapping, evidence: JsonMapping
    ) -> tuple[str, str]:
        analysis = evidence["content"]["ISSUE_ANALYSIS"]
        plan = evidence["content"]["IMPLEMENTATION_PLAN"]
        validation = evidence["content"]["EVALUATION_REPORT"]
        if not all(isinstance(value, Mapping) for value in (analysis, plan, validation)):
            raise CreatePullRequestContractError(
                "analysis, plan, and validation artifacts must contain JSON objects"
            )
        title = f"Issue #{issue['number']}: {issue['title']}"
        plan_steps = _markdown_list(plan.get("implementationSteps", ()))
        tests = _markdown_list(plan.get("tests", ()))
        changed_files = _markdown_list(
            next(
                (
                    item.get("changedFiles", ())
                    for item in evidence["artifacts"]
                    if item.get("artifactType") == "PATCH"
                ),
                (),
            )
        )
        acceptance = evidence["acceptance"]
        build = validation.get("build", {})
        test = validation.get("test", {})
        body = "\n".join(
            (
                f"Closes #{issue['number']}",
                "",
                "## Requested change",
                "",
                str(analysis.get("requestedChange", issue["title"])),
                "",
                "## Implementation plan",
                "",
                plan_steps,
                "",
                "## Changed files",
                "",
                changed_files,
                "",
                "## Validation",
                "",
                f"Status: `{validation.get('status', 'UNKNOWN')}`",
                "",
                f"Build: `{_evaluation_outcome(build)}`",
                f"Test: `{_evaluation_outcome(test)}`",
                "",
                "Planned test coverage:",
                "",
                tests,
                "",
                "## Acceptance evidence",
                "",
                f"EvaluationResult: `{acceptance['id']}` (`{acceptance['outcome']}`)",
            )
        )
        return title, body

    def _evaluate_publication(
        self,
        task: Resource,
        task_execution: JsonMapping,
        workflow: JsonMapping,
        configuration: JsonMapping,
        evidence: JsonMapping,
        candidate_action: JsonMapping,
    ) -> RuntimeObject:
        decision_id = self._runtime_id(
            "policydecision", f"{task_execution['id']}:publication"
        )
        return self._publication_policy.evaluate(
            decision_id=decision_id,
            task_execution_id=str(task_execution["id"]),
            candidate_action=candidate_action,
            required_artifact_ids=evidence["artifactIds"],
            artifacts=evidence["artifacts"],
            required_evaluation_ids=evidence["evaluationIds"],
            evaluation_results=evidence["evaluations"],
            prior_policy_decisions=evidence["priorDecisions"],
            applicable_policies=configuration["publicationPolicies"],
            actor=f"TaskExecution:{task_execution['id']}",
            resource_scope={"repository": configuration["repository"]},
            correlation=_correlation(task_execution),
            timestamp=self._timestamp(),
        )

    def _evaluate_capability(
        self,
        task_execution: JsonMapping,
        workflow: JsonMapping,
        configuration: JsonMapping,
        *,
        capability: str,
        tool: Resource,
        resource_scope: JsonMapping,
        policies: Sequence[ApplicablePolicy],
    ) -> RuntimeObject:
        decision_id = self._runtime_id(
            "policydecision", f"{task_execution['id']}:{capability}"
        )
        return self._capability_policy.evaluate(
            decision_id=decision_id,
            task_execution_id=str(task_execution["id"]),
            capability=capability,
            actor=f"TaskExecution:{task_execution['id']}",
            resource_scope=resource_scope,
            execution_context={
                "workflowExecutionId": workflow["id"],
                "repositoryRevision": workflow["repositoryRevision"],
                "toolRef": _ref_record(tool.ref),
                "repository": configuration["repository"],
            },
            applicable_policies=policies,
            correlation=_correlation(task_execution),
            timestamp=self._timestamp(),
        )

    @staticmethod
    def _authorization(
        decision: JsonMapping, expected_request: ToolRequest
    ) -> Callable[[ToolRequest], bool]:
        def authorize(request: ToolRequest) -> bool:
            return (
                decision.get("decision") == "ALLOW"
                and tuple(request.capabilities) == tuple(expected_request.capabilities)
                and dict(request.tool_ref) == dict(expected_request.tool_ref)
                and dict(request.input) == dict(expected_request.input)
                and request.caller == expected_request.caller
                and request.trace_id == expected_request.trace_id
            )

        return authorize

    def _invoke_github_persisted(
        self,
        *,
        invocation_id: str,
        task_execution: JsonMapping,
        workflow: JsonMapping,
        request: ToolRequest,
        policy_decision_id: str,
        publication_policy_decision_id: str,
        authorize: Callable[[ToolRequest], bool],
    ) -> tuple[ToolResult, RuntimeObject]:
        previous = self._matching_github_invocation(workflow, request)
        if previous is not None:
            if previous.get("id") == invocation_id:
                if previous.get("status") in {"SUCCEEDED", "FAILED"}:
                    return _github_result_from_invocation(previous), previous
                raise GitHubInvocationInProgressError(
                    "The GitHub publication is already in progress; "
                    "the provider operation will not be repeated"
                )
            if previous.get("status") in {"SUCCEEDED", "FAILED"}:
                verification = PersistedPublicationPolicyVerifier(
                    self._runtime_store.get
                ).verify(request)
                if not verification.allowed:
                    raise CreatePullRequestContractError(
                        f"GitHub reconciliation evidence was denied: {verification.reason}"
                    )
                return self._persist_github_reconciliation(
                    invocation_id=invocation_id,
                    task_execution=task_execution,
                    workflow=workflow,
                    request=request,
                    policy_decision_id=policy_decision_id,
                    publication_policy_decision_id=publication_policy_decision_id,
                    previous=previous,
                )
            raise GitHubInvocationInProgressError(
                "A matching GitHub publication is already in progress; "
                "the provider operation will not be repeated"
            )
        fingerprint = _github_fingerprint(
            task_execution_id=str(task_execution["id"]),
            request=request,
            policy_decision_id=policy_decision_id,
            publication_policy_decision_id=publication_policy_decision_id,
        )
        owner_token = str(uuid4())
        timestamp = self._timestamp()
        pending = {
            "apiVersion": "aep.dev/v1alpha1",
            "kind": "ToolInvocation",
            "id": invocation_id,
            "traceId": request.trace_id,
            "createdAt": timestamp,
            "updatedAt": timestamp,
            "provenance": {
                "actor": "tool-runtime",
                "caller": f"TaskExecution:{task_execution['id']}",
                "workflowExecutionId": workflow["id"],
                "taskExecutionId": task_execution["id"],
                "repositoryRevision": workflow["repositoryRevision"],
                "resourceRefs": [dict(request.tool_ref)],
            },
            "taskExecutionId": task_execution["id"],
            "toolRef": dict(request.tool_ref),
            "status": "PENDING",
            "input": _json_record(request.input),
            "capabilities": list(request.capabilities),
            "policyDecisionId": policy_decision_id,
            "publicationPolicyDecisionId": publication_policy_decision_id,
            "requestFingerprint": fingerprint,
            "ownerToken": owner_token,
        }
        created = self._runtime_store.create(
            pending,
            deterministic_key=f"github-tool-invocation:{invocation_id}",
        )
        if created.get("requestFingerprint") != fingerprint:
            raise GitHubInvocationIdentityConflictError(
                f"GitHub invocation {invocation_id!r} changed immutable inputs"
            )
        if created.get("ownerToken") != owner_token:
            if created.get("status") in {"SUCCEEDED", "FAILED"}:
                return _github_result_from_invocation(created), created
            raise GitHubInvocationInProgressError(
                f"GitHub invocation {invocation_id!r} is already in progress; "
                "the provider operation will not be repeated"
            )
        result = invoke_github_tool(
            request,
            pre_execute_authorize=authorize,
            publication_verifier=PersistedPublicationPolicyVerifier(
                self._runtime_store.get
            ),
            adapter=self._github_adapter,
        )
        status = (
            "SUCCEEDED"
            if result.status is ToolResultStatus.SUCCEEDED
            else "FAILED"
        )
        persisted = self._runtime_store.update_status(
            invocation_id,
            status,
            expected_status="PENDING",
            updated_at=result.completed_at,
            changes=_github_terminal_changes(result),
        )
        return result, persisted

    def _persist_github_reconciliation(
        self,
        *,
        invocation_id: str,
        task_execution: JsonMapping,
        workflow: JsonMapping,
        request: ToolRequest,
        policy_decision_id: str,
        publication_policy_decision_id: str,
        previous: JsonMapping,
    ) -> tuple[ToolResult, RuntimeObject]:
        timestamp = self._timestamp()
        prior_result = _github_result_from_invocation(previous)
        record: dict[str, Any] = {
            "apiVersion": "aep.dev/v1alpha1",
            "kind": "ToolInvocation",
            "id": invocation_id,
            "traceId": request.trace_id,
            "createdAt": timestamp,
            "updatedAt": timestamp,
            "provenance": {
                "actor": "tool-runtime",
                "caller": f"TaskExecution:{task_execution['id']}",
                "workflowExecutionId": workflow["id"],
                "taskExecutionId": task_execution["id"],
                "repositoryRevision": workflow["repositoryRevision"],
                "resourceRefs": [dict(request.tool_ref)],
            },
            "taskExecutionId": task_execution["id"],
            "toolRef": dict(request.tool_ref),
            "status": previous["status"],
            "input": _json_record(request.input),
            "capabilities": list(request.capabilities),
            "policyDecisionId": policy_decision_id,
            "publicationPolicyDecisionId": publication_policy_decision_id,
            "requestFingerprint": _github_fingerprint(
                task_execution_id=str(task_execution["id"]),
                request=request,
                policy_decision_id=policy_decision_id,
                publication_policy_decision_id=publication_policy_decision_id,
            ),
            "reconciledFromToolInvocationId": previous["id"],
            "resultStatus": prior_result.status.value,
            "output": prior_result.output_record(),
            "metrics": ToolMetrics(duration_ms=0).as_record(),
            "startedAt": timestamp,
            "completedAt": timestamp,
        }
        if prior_result.failure_class is not None:
            record["failureClass"] = prior_result.failure_class.value
            record["failure"] = {
                "class": _runtime_failure_class(prior_result.failure_class),
                "message": (
                    prior_result.failure_message or prior_result.failure_class.value
                ),
                "retryable": False,
            }
        persisted = self._runtime_store.create(
            record,
            deterministic_key=(
                f"github-tool-reconciliation:{invocation_id}:{previous['id']}"
            ),
        )
        return _github_result_from_invocation(persisted), persisted

    def _matching_github_invocation(
        self, workflow: JsonMapping, request: ToolRequest
    ) -> RuntimeObject | None:
        expected = _publication_identity(request.input)
        matches = [
            item
            for item in self._runtime_store.list_by_workflow_execution(
                str(workflow["id"])
            )
            if item.get("kind") == "ToolInvocation"
            and item.get("toolRef") == dict(request.tool_ref)
            and _publication_identity(item.get("input")) == expected
        ]
        if not matches:
            return None
        succeeded = [item for item in matches if item.get("status") == "SUCCEEDED"]
        if succeeded:
            return succeeded[0]
        pending = [item for item in matches if item.get("status") == "PENDING"]
        return pending[0] if pending else matches[0]

    def _publish_description(
        self,
        task: Resource,
        task_execution: JsonMapping,
        workflow: JsonMapping,
        configuration: JsonMapping,
        evidence: JsonMapping,
        title: str,
        body: str,
        publication: JsonMapping,
        push_policy: JsonMapping,
        github_policy: JsonMapping,
        commit_invocation: JsonMapping,
        push_invocation: JsonMapping,
        github_invocation: JsonMapping,
    ) -> RuntimeObject:
        head_revision = commit_invocation.get("output", {}).get("revision")
        artifact_id = self._runtime_id(
            "generatedartifact", str(task_execution["id"])
        )
        existing = self._artifact_store.get(artifact_id)
        if existing is not None:
            if not (
                existing.get("artifactType") == "PULL_REQUEST_DESCRIPTION"
                and existing.get("taskExecutionId") == task_execution.get("id")
                and existing.get("repositoryRevision")
                == workflow.get("repositoryRevision")
                and existing.get("provenance", {}).get("workflowExecutionId")
                == workflow.get("id")
                and existing.get("headRevision") == head_revision
            ):
                raise CreatePullRequestContractError(
                    "existing pull-request description artifact conflicts"
                )
            return existing
        output = github_invocation.get("output")
        pull_request = output.get("pullRequest") if isinstance(output, Mapping) else None
        metadata = output.get("metadata") if isinstance(output, Mapping) else None
        if not isinstance(pull_request, Mapping) or not isinstance(metadata, Mapping):
            raise CreatePullRequestContractError(
                "successful GitHub invocation lacks pull-request evidence"
            )
        timestamp = self._timestamp()
        policy_ids = [push_policy["id"], publication["id"], github_policy["id"]]
        content = {
            "title": title,
            "body": body,
            "pullRequest": deepcopy(dict(pull_request)),
            "provider": {
                "requestId": metadata.get("providerRequestId"),
                "attemptCount": metadata.get("attemptCount"),
            },
            "evidence": {
                "publicationPolicyDecisionId": publication["id"],
                "gitCommitToolInvocationId": commit_invocation["id"],
                "gitPushToolInvocationId": push_invocation["id"],
                "githubToolInvocationId": github_invocation["id"],
            },
        }
        return self._artifact_store.publish(
            {
                "apiVersion": "aep.dev/v1alpha1",
                "kind": "GeneratedArtifact",
                "id": artifact_id,
                "traceId": task_execution["traceId"],
                "createdAt": timestamp,
                "updatedAt": timestamp,
                "provenance": {
                    "actor": "create-pull-request-task-handler",
                    "workflowExecutionId": workflow["id"],
                    "taskExecutionId": task_execution["id"],
                    "repositoryRevision": workflow["repositoryRevision"],
                    "resourceRefs": [
                        _ref_record(task.ref),
                        _ref_record(configuration["gitTool"].ref),
                        _ref_record(configuration["githubTool"].ref),
                    ],
                    "inputArtifactRefs": [
                        {
                            "generatedArtifactId": artifact["id"],
                            "contentAddress": artifact["contentAddress"],
                        }
                        for artifact in evidence["artifacts"]
                    ],
                },
                "taskExecutionId": task_execution["id"],
                "artifactType": "PULL_REQUEST_DESCRIPTION",
                "repositoryRevision": workflow["repositoryRevision"],
                "headRevision": head_revision,
                "mediaType": "application/json",
                "evaluationResultIds": [evidence["acceptance"]["id"]],
                "policyDecisionIds": policy_ids,
                "pullRequestNumber": pull_request["number"],
                "pullRequestUrl": pull_request["url"],
                "providerRequestId": metadata.get("providerRequestId"),
                "gitCommitToolInvocationId": commit_invocation["id"],
                "gitPushToolInvocationId": push_invocation["id"],
                "githubToolInvocationId": github_invocation["id"],
            },
            content,
        )

    def _resource(self, value: Any, kind: str, field: str) -> Resource:
        if not isinstance(value, Mapping):
            raise CreatePullRequestContractError(
                f"{field} must be an explicit {kind} reference"
            )
        try:
            ref = ResourceRef.from_mapping(dict(value))
        except (KeyError, TypeError, ValueError):
            raise CreatePullRequestContractError(
                f"{field} contains an invalid Resource reference"
            ) from None
        if ref.kind != kind or ref.version in {"", "latest"}:
            raise CreatePullRequestContractError(
                f"{field} must reference an explicit {kind} version"
            )
        resource = self._resources.get(ref)
        if resource is None:
            raise CreatePullRequestContractError(
                f"missing Resource {_ref_record(ref)!r}"
            )
        return resource

    def _attach(self, task_execution_id: object, changes: JsonMapping) -> None:
        execution_id = str(task_execution_id)
        current = self._runtime_store.get(execution_id)
        if current is None or current.get("status") != "RUNNING":
            raise CreatePullRequestContractError("TaskExecution is no longer RUNNING")
        merged: dict[str, Any] = {}
        for field, values in changes.items():
            prior = current.get(field, ())
            merged[field] = list(dict.fromkeys([*prior, *values]))
        try:
            self._runtime_store.update_status(
                execution_id,
                "RUNNING",
                expected_status="RUNNING",
                updated_at=self._timestamp(),
                changes=merged,
            )
        except StatusConflictError as error:
            raise CreatePullRequestContractError(
                "TaskExecution evidence attachment lost a status race"
            ) from error

    def _timestamp(self) -> str:
        value = self._clock()
        if not isinstance(value, str) or not is_rfc3339_timestamp(value):
            raise CreatePullRequestContractError(
                "clock must return an RFC3339 timestamp"
            )
        return value

    def _runtime_id(self, prefix: str, discriminator: str) -> str:
        digest = sha256(
            f"{self.runtime_id_namespace}:{discriminator}:{prefix}".encode()
        ).hexdigest()[:24]
        return f"{prefix}-{digest}"


def _string_list(value: Any, field: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise CreatePullRequestContractError(f"{field} must be an array")
    if (
        not all(isinstance(item, str) and item for item in value)
        or len(set(value)) != len(value)
    ):
        raise CreatePullRequestContractError(
            f"{field} must contain unique runtime identifiers"
        )
    return tuple(value)


def _markdown_list(value: Any) -> str:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return "- Not provided"
    items = [str(item).strip() for item in value if str(item).strip()]
    return "\n".join(f"- {item}" for item in items) or "- None"


def _evaluation_outcome(value: Any) -> str:
    if not isinstance(value, Mapping):
        return "UNKNOWN"
    return str(value.get("outcome") or value.get("status") or "UNKNOWN")


def _github_fingerprint(
    *,
    task_execution_id: str,
    request: ToolRequest,
    policy_decision_id: str,
    publication_policy_decision_id: str,
) -> str:
    value = {
        "taskExecutionId": task_execution_id,
        "toolRef": dict(request.tool_ref),
        "input": _json_record(request.input),
        "caller": request.caller.as_record(),
        "capabilities": list(request.capabilities),
        "timeoutMs": request.timeout_ms,
        "traceId": request.trace_id,
        "policyDecisionId": policy_decision_id,
        "publicationPolicyDecisionId": publication_policy_decision_id,
    }
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return "sha256:" + sha256(canonical.encode()).hexdigest()


def _publication_identity(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping) or value.get("operation") != CREATE_PULL_REQUEST:
        return None
    evidence = value.get("publicationEvidence")
    if not isinstance(evidence, Mapping):
        return None
    return {
        "operation": value.get("operation"),
        "repository": value.get("repository"),
        "issueNumber": value.get("issueNumber"),
        "head": value.get("head"),
        "base": value.get("base"),
        "title": value.get("title"),
        "body": value.get("body"),
        "workflowExecutionId": evidence.get("workflowExecutionId"),
        "repositoryRevision": evidence.get("repositoryRevision"),
        "headRevision": evidence.get("headRevision"),
        "evaluationResultIds": list(evidence.get("evaluationResultIds", ())),
        "generatedArtifactIds": list(evidence.get("generatedArtifactIds", ())),
    }


def _github_terminal_changes(result: ToolResult) -> dict[str, Any]:
    changes: dict[str, Any] = {
        "resultStatus": result.status.value,
        "output": result.output_record(),
        "metrics": result.metrics.as_record(),
        "startedAt": result.started_at,
    }
    if result.failure_class is not None:
        changes["failureClass"] = result.failure_class.value
        changes["failure"] = {
            "class": _runtime_failure_class(result.failure_class),
            "message": result.failure_message or result.failure_class.value,
            "retryable": False,
        }
    return changes


def _json_record(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _json_record(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_record(item) for item in value]
    return deepcopy(value)


def _github_result_from_invocation(invocation: JsonMapping) -> ToolResult:
    failure_class = invocation.get("failureClass")
    failure = invocation.get("failure")
    metrics = invocation.get("metrics", {})
    return ToolResult(
        status=ToolResultStatus(str(invocation["resultStatus"])),
        output=invocation.get("output"),
        logs_ref=None,
        metrics=ToolMetrics(duration_ms=int(metrics.get("durationMs", 0))),
        started_at=str(invocation["startedAt"]),
        completed_at=str(invocation["completedAt"]),
        failure_class=(
            ToolFailureClass(str(failure_class)) if failure_class else None
        ),
        failure_message=(
            failure.get("message") if isinstance(failure, Mapping) else None
        ),
    )


def _runtime_failure_class(failure: ToolFailureClass) -> str:
    return {
        ToolFailureClass.VALIDATION: "CONFIGURATION",
        ToolFailureClass.POLICY: "POLICY",
        ToolFailureClass.TIMEOUT: "PERMANENT",
        ToolFailureClass.ADAPTER: "PERMANENT",
        ToolFailureClass.STARTUP: "PERMANENT",
        ToolFailureClass.NONZERO_EXIT: "PERMANENT",
        ToolFailureClass.BOUNDARY: "POLICY",
        ToolFailureClass.NOT_FOUND: "PERMANENT",
        ToolFailureClass.IO: "PERMANENT",
    }[failure]


def _tool_failure(label: str, result: ToolResult) -> TaskExecutionResult:
    failure = result.failure_class or ToolFailureClass.ADAPTER
    if failure in {ToolFailureClass.POLICY, ToolFailureClass.BOUNDARY}:
        classification = FailureClass.POLICY
    elif label == "Git push" and failure in {
        ToolFailureClass.TIMEOUT,
        ToolFailureClass.IO,
        ToolFailureClass.STARTUP,
    }:
        classification = FailureClass.RECOVERABLE
    elif failure is ToolFailureClass.VALIDATION:
        classification = FailureClass.CONFIGURATION
    else:
        classification = FailureClass.PERMANENT
    return TaskExecutionResult.failure(
        classification,
        f"{label} failed: {result.failure_message or result.status.value}",
    )
