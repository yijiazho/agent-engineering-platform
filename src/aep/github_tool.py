"""GitHub Tool adapter with trusted publication evidence and bounded execution."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic, sleep
from typing import Any, Protocol

from aep.tool_runtime import (
    JsonSchemaToolValidator,
    ToolAdapter,
    ToolExecution,
    ToolFailureClass,
    ToolMetrics,
    ToolRequest,
    ToolResult,
    ToolResultStatus,
    ToolSchemaValidationError,
    SEMVER_PATTERN,
    invoke_tool,
)


JsonObject = Mapping[str, Any]
READ_ISSUE = "readIssue"
CREATE_PULL_REQUEST = "createPullRequest"
READ_CAPABILITY = "github.issue.read"
CREATE_PR_CAPABILITY = "github.create_pr"

_ID_LIST = {
    "type": "array",
    "minItems": 1,
    "uniqueItems": True,
    "items": {"type": "string", "minLength": 1},
}
GITHUB_INPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "oneOf": [
        {
            "additionalProperties": False,
            "required": ["operation", "repository", "issueNumber"],
            "properties": {
                "operation": {"const": READ_ISSUE},
                "repository": {"type": "string", "pattern": r"^[^/\s]+/[^/\s]+$"},
                "issueNumber": {"type": "integer", "minimum": 1},
            },
        },
        {
            "additionalProperties": False,
            "required": [
                "operation",
                "repository",
                "head",
                "base",
                "title",
                "body",
                "publicationEvidence",
            ],
            "properties": {
                "operation": {"const": CREATE_PULL_REQUEST},
                "repository": {"type": "string", "pattern": r"^[^/\s]+/[^/\s]+$"},
                "head": {"type": "string", "minLength": 1},
                "base": {"type": "string", "minLength": 1},
                "title": {"type": "string", "minLength": 1},
                "body": {"type": "string"},
                "issueNumber": {"type": "integer", "minimum": 1},
                "publicationEvidence": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "policyDecisionId",
                        "taskExecutionId",
                        "workflowExecutionId",
                        "repositoryRevision",
                        "headRevision",
                        "evaluationResultIds",
                        "generatedArtifactIds",
                        "commitToolInvocationId",
                        "pushToolInvocationId",
                    ],
                    "properties": {
                        "policyDecisionId": {"type": "string", "minLength": 1},
                        "taskExecutionId": {"type": "string", "minLength": 1},
                        "workflowExecutionId": {"type": "string", "minLength": 1},
                        "repositoryRevision": {"type": "string", "minLength": 7},
                        "headRevision": {"type": "string", "minLength": 7},
                        "evaluationResultIds": deepcopy(_ID_LIST),
                        "generatedArtifactIds": deepcopy(_ID_LIST),
                        "pushToolInvocationId": {
                            "type": "string",
                            "minLength": 1,
                        },
                        "commitToolInvocationId": {
                            "type": "string",
                            "minLength": 1,
                        },
                    },
                },
            },
        },
    ],
}

_ATTEMPT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "attempt",
        "providerRequestId",
        "classification",
        "retryable",
        "retryAfterMs",
        "outcome",
    ],
    "properties": {
        "attempt": {"type": "integer", "minimum": 1},
        "providerRequestId": {"type": ["string", "null"]},
        "classification": {
            "enum": [
                "SUCCESS",
                "AUTHENTICATION",
                "AUTHORIZATION",
                "VALIDATION",
                "RATE_LIMIT",
                "PROVIDER",
                "AMBIGUOUS_MUTATION",
                "TIMEOUT",
            ]
        },
        "retryable": {"type": "boolean"},
        "retryAfterMs": {"type": ["integer", "null"], "minimum": 0},
        "outcome": {"enum": ["SUCCEEDED", "FAILED", "TIMED_OUT"]},
    },
}
_METADATA_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["traceId", "providerRequestId", "attemptCount", "attempts"],
    "properties": {
        "traceId": {"type": "string", "minLength": 1},
        "providerRequestId": {"type": ["string", "null"]},
        "attemptCount": {"type": "integer", "minimum": 1},
        "attempts": {"type": "array", "minItems": 1, "items": _ATTEMPT_SCHEMA},
    },
}
GITHUB_OUTPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "oneOf": [
        {
            "additionalProperties": False,
            "required": ["operation", "repository", "issue", "metadata"],
            "properties": {
                "operation": {"const": READ_ISSUE},
                "repository": {"type": "string"},
                "issue": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "number",
                        "title",
                        "body",
                        "state",
                        "url",
                        "author",
                        "labels",
                    ],
                    "properties": {
                        "number": {"type": "integer", "minimum": 1},
                        "title": {"type": "string"},
                        "body": {"type": ["string", "null"]},
                        "state": {"type": "string"},
                        "url": {"type": "string", "minLength": 1},
                        "author": {"type": ["string", "null"]},
                        "labels": {"type": "array", "items": {"type": "string"}},
                    },
                },
                "metadata": _METADATA_SCHEMA,
            },
        },
        {
            "additionalProperties": False,
            "required": ["operation", "repository", "pullRequest", "metadata"],
            "properties": {
                "operation": {"const": CREATE_PULL_REQUEST},
                "repository": {"type": "string"},
                "pullRequest": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["number", "url", "head", "base"],
                    "properties": {
                        "number": {"type": "integer", "minimum": 1},
                        "url": {"type": "string", "minLength": 1},
                        "head": {"type": "string", "minLength": 1},
                        "base": {"type": "string", "minLength": 1},
                    },
                },
                "metadata": _METADATA_SCHEMA,
            },
        },
    ],
}


class GitHubProviderOperation(Protocol):
    """Cancellable provider operation, normally backed by an isolated process."""

    def wait(self, timeout_ms: int) -> JsonObject | Exception | None: ...

    @property
    def request_id(self) -> str | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def cleanup(self) -> None: ...


class GitHubClient(Protocol):
    """Client boundary whose start methods must return before network work blocks."""

    def start_read_issue(
        self, repository: str, issue_number: int
    ) -> GitHubProviderOperation: ...

    def start_create_pull_request(
        self,
        repository: str,
        *,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> GitHubProviderOperation: ...


class GitHubProviderError(RuntimeError):
    classification = "PROVIDER"

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        request_id: str | None = None,
        retry_after_ms: int | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.request_id = request_id
        self.retry_after_ms = retry_after_ms


class GitHubRateLimitError(GitHubProviderError):
    classification = "RATE_LIMIT"

    def __init__(
        self,
        message: str = "GitHub rate limit exceeded",
        *,
        request_id: str | None = None,
        retry_after_ms: int | None = None,
    ) -> None:
        super().__init__(
            message,
            retryable=True,
            request_id=request_id,
            retry_after_ms=retry_after_ms,
        )


@dataclass(frozen=True)
class PublicationVerification:
    allowed: bool
    reason: str


class PublicationPolicyVerifier(Protocol):
    """Trusted boundary resolving immutable publication and evaluation evidence."""

    def verify(self, request: ToolRequest) -> PublicationVerification: ...


class PersistedPublicationPolicyVerifier:
    """Verify a publication decision and its complete persisted evidence graph."""

    def __init__(self, resolve: Callable[[str], JsonObject | None]) -> None:
        self._resolve = resolve

    def verify(self, request: ToolRequest) -> PublicationVerification:
        evidence = request.input["publicationEvidence"]
        if (
            request.caller.kind != "TaskExecution"
            or request.caller.id != evidence["taskExecutionId"]
        ):
            return PublicationVerification(False, "Publication task identity mismatch")
        artifacts = self._resolve_many(evidence["generatedArtifactIds"])
        evaluations = self._resolve_many(evidence["evaluationResultIds"])
        policy = self._resolve(evidence["policyDecisionId"])
        commit = self._resolve(evidence["commitToolInvocationId"])
        push = self._resolve(evidence["pushToolInvocationId"])
        if policy is None:
            return PublicationVerification(False, "Publication Policy decision not found")
        if commit is None:
            return PublicationVerification(False, "Git commit evidence not found")
        if push is None:
            return PublicationVerification(False, "Git push evidence not found")

        workflow_id = evidence["workflowExecutionId"]
        revision = evidence["repositoryRevision"]
        head_revision = evidence["headRevision"]
        if head_revision == revision:
            return PublicationVerification(False, "Published head did not advance")
        artifact_ids = tuple(evidence["generatedArtifactIds"])
        evaluation_ids = tuple(evidence["evaluationResultIds"])

        for artifact_id, artifact in zip(artifact_ids, artifacts, strict=True):
            if not _matches(
                artifact,
                kind="GeneratedArtifact",
                id=artifact_id,
                traceId=request.trace_id,
            ):
                return PublicationVerification(False, "GeneratedArtifact identity mismatch")
            if artifact.get("repositoryRevision") != revision:
                return PublicationVerification(False, "GeneratedArtifact revision mismatch")
            provenance = artifact.get("provenance", {})
            if provenance.get("workflowExecutionId") != workflow_id:
                return PublicationVerification(False, "GeneratedArtifact workflow mismatch")
            if provenance.get("repositoryRevision") != revision:
                return PublicationVerification(False, "GeneratedArtifact provenance mismatch")
            artifact_evaluations = set(artifact.get("evaluationResultIds", ()))
            if not artifact_evaluations or not artifact_evaluations.issubset(
                set(evaluation_ids)
            ):
                return PublicationVerification(
                    False, "GeneratedArtifact evaluation binding mismatch"
                )
        patch_artifacts = [
            artifact
            for artifact in artifacts
            if artifact.get("artifactType") == "PATCH"
        ]
        if len(patch_artifacts) != 1:
            return PublicationVerification(False, "Published PATCH evidence mismatch")
        patch_address = str(patch_artifacts[0].get("contentAddress", ""))
        patch_algorithm, patch_separator, patch_digest = patch_address.partition(":")
        if not (
            patch_algorithm == "sha256"
            and patch_separator == ":"
            and len(patch_digest) == 64
        ):
            return PublicationVerification(False, "Published PATCH digest mismatch")

        for evaluation_id, evaluation in zip(
            evaluation_ids, evaluations, strict=True
        ):
            if not _matches(
                evaluation,
                kind="EvaluationResult",
                id=evaluation_id,
                traceId=request.trace_id,
            ):
                return PublicationVerification(False, "EvaluationResult identity mismatch")
            if evaluation.get("status") != "SUCCEEDED" or evaluation.get("outcome") != "PASS":
                return PublicationVerification(False, "Technical evaluation did not pass")
            provenance = evaluation.get("provenance", {})
            if provenance.get("workflowExecutionId") != workflow_id:
                return PublicationVerification(False, "EvaluationResult workflow mismatch")
            if provenance.get("repositoryRevision") != revision:
                return PublicationVerification(False, "EvaluationResult revision mismatch")
            target = evaluation.get("target", {})
            if target.get("type") == "GeneratedArtifact" and target.get("id") not in artifact_ids:
                return PublicationVerification(False, "EvaluationResult artifact mismatch")
            if target.get("type") not in {
                "TaskExecution",
                "GeneratedArtifact",
                "AgentInvocation",
                "ModelInvocation",
                "ToolInvocation",
            }:
                return PublicationVerification(False, "EvaluationResult target mismatch")

        if not _matches(
            commit,
            kind="ToolInvocation",
            id=evidence["commitToolInvocationId"],
            traceId=request.trace_id,
            taskExecutionId=evidence["taskExecutionId"],
            status="SUCCEEDED",
        ):
            return PublicationVerification(False, "Git commit identity mismatch")
        commit_tool_ref = commit.get("toolRef", {})
        commit_tool_version = commit_tool_ref.get("version")
        if (
            commit_tool_ref.get("kind") != "Tool"
            or commit_tool_ref.get("name") != "git"
            or not isinstance(commit_tool_version, str)
            or not SEMVER_PATTERN.fullmatch(commit_tool_version)
        ):
            return PublicationVerification(False, "Git commit Tool reference mismatch")
        commit_input = commit.get("input", {})
        commit_output = commit.get("output", {})
        if not (
            commit_input.get("operation") == "commit_changes"
            and commit_input.get("expectedRevision") == revision
            and commit_input.get("branch") == request.input["head"]
            and isinstance(commit_input.get("commitMessage"), str)
            and commit_input.get("commitMessage")
            and commit_input.get("expectedPatchSha256") == patch_digest
            and commit_output.get("operation") == "commit_changes"
            and commit_output.get("repository") == request.input["repository"]
            and commit_output.get("branch") == request.input["head"]
            and commit_output.get("baseRevision") == revision
            and commit_output.get("revision") == head_revision
            and commit_output.get("remoteMutationState") == "NOT_ATTEMPTED"
        ):
            return PublicationVerification(False, "Git commit evidence mismatch")
        commit_provenance = commit.get("provenance", {})
        expected_git_provenance = {
            "workflowExecutionId": workflow_id,
            "taskExecutionId": evidence["taskExecutionId"],
            "repositoryRevision": revision,
        }
        if any(
            commit_provenance.get(key) != value
            for key, value in expected_git_provenance.items()
        ):
            return PublicationVerification(False, "Git commit provenance mismatch")

        if not _matches(
            push,
            kind="ToolInvocation",
            id=evidence["pushToolInvocationId"],
            traceId=request.trace_id,
            taskExecutionId=evidence["taskExecutionId"],
            status="SUCCEEDED",
        ):
            return PublicationVerification(False, "Git push identity mismatch")
        push_provenance = push.get("provenance", {})
        if any(
            push_provenance.get(key) != value
            for key, value in expected_git_provenance.items()
        ):
            return PublicationVerification(False, "Git push provenance mismatch")
        tool_ref = push.get("toolRef", {})
        version = tool_ref.get("version")
        if (
            tool_ref.get("kind") != "Tool"
            or tool_ref.get("name") != "git"
            or not isinstance(version, str)
            or not SEMVER_PATTERN.fullmatch(version)
        ):
            return PublicationVerification(False, "Git push Tool reference mismatch")
        if dict(tool_ref) != dict(commit_tool_ref):
            return PublicationVerification(False, "Git Tool version mismatch")
        expected_push_input = {
            "operation": "push_branch",
            "expectedRevision": revision,
            "branch": request.input["head"],
        }
        if dict(push.get("input", {})) != expected_push_input:
            return PublicationVerification(False, "Git push input mismatch")
        expected_push_output = {
            "operation": "push_branch",
            "repository": request.input["repository"],
            "branch": request.input["head"],
            "revision": head_revision,
            "remoteMutationState": "CONFIRMED",
        }
        if any(
            push.get("output", {}).get(key) != value
            for key, value in expected_push_output.items()
        ):
            return PublicationVerification(False, "Git push target mismatch")
        push_policy_id = push.get("policyDecisionId")
        if not isinstance(push_policy_id, str):
            return PublicationVerification(False, "Git push PolicyDecision missing")
        push_policy = self._resolve(push_policy_id)
        if push_policy is None:
            return PublicationVerification(False, "Git push PolicyDecision not found")
        if commit.get("policyDecisionId") != push_policy_id:
            return PublicationVerification(False, "Git commit policy mismatch")
        expected_push_policy = {
            "kind": "PolicyDecision",
            "id": push_policy_id,
            "traceId": request.trace_id,
            "taskExecutionId": evidence["taskExecutionId"],
            "gate": "PRE_EXECUTION_CAPABILITY",
            "action": "git.push",
            "decision": "ALLOW",
        }
        if any(
            push_policy.get(key) != value
            for key, value in expected_push_policy.items()
        ):
            return PublicationVerification(False, "Git push policy mismatch")
        policy_provenance = push_policy.get("provenance", {})
        if any(
            policy_provenance.get(key) != value
            for key, value in expected_git_provenance.items()
        ):
            return PublicationVerification(False, "Git push policy provenance mismatch")
        expected_push_scope = {
            "repository": request.input["repository"],
            "branch": request.input["head"],
            "repositoryRevision": revision,
            "toolRef": dict(tool_ref),
        }
        push_scope = push_policy.get("resourceScope", {})
        if any(
            push_scope.get(key) != value
            for key, value in expected_push_scope.items()
        ):
            return PublicationVerification(False, "Git push policy target mismatch")

        expected_policy = {
            "kind": "PolicyDecision",
            "id": evidence["policyDecisionId"],
            "traceId": request.trace_id,
            "taskExecutionId": evidence["taskExecutionId"],
            "gate": "PUBLICATION",
            "action": CREATE_PR_CAPABILITY,
            "decision": "ALLOW",
            "repositoryRevision": revision,
        }
        if any(policy.get(key) != value for key, value in expected_policy.items()):
            return PublicationVerification(False, "Publication Policy evidence mismatch")
        if set(policy.get("evaluationResultIds", ())) != set(evaluation_ids):
            return PublicationVerification(False, "Publication evaluation binding mismatch")
        if set(policy.get("generatedArtifactIds", ())) != set(artifact_ids):
            return PublicationVerification(False, "Publication artifact binding mismatch")
        provenance = policy.get("provenance", {})
        if provenance.get("workflowExecutionId") != workflow_id:
            return PublicationVerification(False, "Publication Policy workflow mismatch")
        scope = policy.get("resourceScope", {})
        if scope.get("repository") != request.input["repository"]:
            return PublicationVerification(False, "Publication Policy repository mismatch")
        target = policy.get("publicationTarget", {})
        expected_target = {
            "repository": request.input["repository"],
            "head": request.input["head"],
            "base": request.input["base"],
            "repositoryRevision": revision,
            "headRevision": head_revision,
            "commitToolInvocationId": evidence["commitToolInvocationId"],
            "pushToolInvocationId": evidence["pushToolInvocationId"],
        }
        if any(target.get(key) != value for key, value in expected_target.items()):
            return PublicationVerification(False, "Publication target mismatch")
        return PublicationVerification(True, policy.get("reason", "Publication allowed"))

    def _resolve_many(self, ids: Sequence[str]) -> list[JsonObject]:
        values: list[JsonObject] = []
        for runtime_id in ids:
            value = self._resolve(runtime_id)
            if value is None:
                return [{} for _ in ids]
            values.append(value)
        return values


def _matches(value: JsonObject, **expected: Any) -> bool:
    return all(value.get(key) == item for key, item in expected.items())


class GitHubToolAdapter(ToolAdapter):
    """Create a cancellable Tool execution over an injected GitHub client."""

    def __init__(
        self,
        client: GitHubClient,
        *,
        max_read_attempts: int = 2,
        clock: Callable[[], float] = monotonic,
        wait_retry: Callable[[int], None] | None = None,
    ) -> None:
        if max_read_attempts < 1:
            raise ValueError("max_read_attempts must be positive")
        self._client = client
        self._max_read_attempts = max_read_attempts
        self._clock = clock
        self._wait_retry = wait_retry or (lambda ms: sleep(ms / 1000))

    def start(self, request: ToolRequest) -> ToolExecution:
        return _GitHubExecution(
            request,
            self._client,
            self._max_read_attempts,
            self._clock,
            self._wait_retry,
        )


class _GitHubExecution(ToolExecution):
    def __init__(
        self,
        request: ToolRequest,
        client: GitHubClient,
        max_read_attempts: int,
        clock: Callable[[], float],
        wait_retry: Callable[[int], None],
    ) -> None:
        self._request = request
        self._client = client
        self._max_attempts = (
            max_read_attempts
            if request.input["operation"] == READ_ISSUE
            else 1
        )
        self._clock = clock
        self._wait_retry = wait_retry
        self._current: GitHubProviderOperation | None = None
        self._current_pending = False
        self._terminated = False
        self._operations: list[GitHubProviderOperation] = []
        self._attempts: list[dict[str, Any]] = []
        self._started_at = datetime.now(UTC)
        self._started_clock = clock()

    def wait(self, timeout_ms: int) -> ToolResult | None:
        if self._terminated:
            return None
        deadline = self._clock() + timeout_ms / 1000
        for attempt in range(len(self._attempts) + 1, self._max_attempts + 1):
            remaining_ms = max(0, int((deadline - self._clock()) * 1000))
            if remaining_ms < 1:
                return None
            try:
                if not self._current_pending:
                    self._current = self._start_operation()
                    self._operations.append(self._current)
                    self._current_pending = True
                outcome = self._current.wait(remaining_ms)
            except Exception as error:
                outcome = error
            if outcome is None:
                return self._timeout(attempt)
            self._current_pending = False
            if isinstance(outcome, Exception):
                error = (
                    outcome
                    if isinstance(outcome, GitHubProviderError)
                    else GitHubProviderError(str(outcome) or type(outcome).__name__)
                )
                record = _attempt_record(attempt, error)
                self._attempts.append(record)
                if error.retryable and attempt < self._max_attempts:
                    retry_after = error.retry_after_ms or 0
                    remaining_ms = max(0, int((deadline - self._clock()) * 1000))
                    if retry_after >= remaining_ms:
                        return self._failure(error)
                    self._wait_retry(retry_after)
                    continue
                return self._failure(error)
            request_id = (
                outcome.get("requestId")
                if isinstance(outcome.get("requestId"), str)
                else None
            )
            self._attempts.append(
                {
                    "attempt": attempt,
                    "providerRequestId": request_id,
                    "classification": "SUCCESS",
                    "retryable": False,
                    "retryAfterMs": None,
                    "outcome": "SUCCEEDED",
                }
            )
            output = (
                _issue_output(self._request, outcome, self._attempts)
                if self._request.input["operation"] == READ_ISSUE
                else _pull_request_output(self._request, outcome, self._attempts)
            )
            return self._result(ToolResultStatus.SUCCEEDED, output)
        raise AssertionError("GitHub attempt loop did not return")

    def terminate(self) -> None:
        self._terminated = True
        if self._current is not None:
            self._current.terminate()

    def kill(self) -> None:
        if self._current is not None:
            self._current.kill()

    def cleanup(self) -> None:
        for operation in self._operations:
            operation.cleanup()

    def _start_operation(self) -> GitHubProviderOperation:
        value = self._request.input
        if value["operation"] == READ_ISSUE:
            return self._client.start_read_issue(
                value["repository"], value["issueNumber"]
            )
        return self._client.start_create_pull_request(
            value["repository"],
            head=value["head"],
            base=value["base"],
            title=value["title"],
            body=value["body"],
        )

    def _failure(self, error: GitHubProviderError) -> ToolResult:
        category = error.classification
        return self._result(
            ToolResultStatus.FAILED,
            {
                "operation": self._request.input["operation"],
                "repository": self._request.input["repository"],
                "failure": {
                    "category": category,
                    "mutationState": (
                        "UNKNOWN"
                        if error.classification == "AMBIGUOUS_MUTATION"
                        else "NOT_ATTEMPTED"
                    ),
                    "retryable": error.retryable,
                    "retryAfterMs": error.retry_after_ms,
                    "attemptCount": len(self._attempts),
                    "providerRequestId": error.request_id,
                    "traceId": self._request.trace_id,
                    "attempts": deepcopy(self._attempts),
                },
            },
            failure_class=ToolFailureClass.ADAPTER,
            failure_message=str(error),
        )

    def _timeout(self, attempt: int) -> ToolResult:
        if self._current is None:
            raise AssertionError("provider timeout without an active operation")
        self._current.terminate()
        if self._current.wait(100) is None:
            self._current.kill()
        self._current_pending = False
        request_id = getattr(self._current, "request_id", None)
        ambiguous_publication = (
            self._request.input["operation"] == CREATE_PULL_REQUEST
            and bool(getattr(self._current, "mutation_started", True))
        )
        self._attempts.append(
            {
                "attempt": attempt,
                "providerRequestId": request_id,
                "classification": "TIMEOUT",
                "retryable": not ambiguous_publication,
                "retryAfterMs": None,
                "outcome": "TIMED_OUT",
            }
        )
        return self._result(
            ToolResultStatus.TIMED_OUT,
            {
                "operation": self._request.input["operation"],
                "repository": self._request.input["repository"],
                "failure": {
                    "category": "TIMEOUT",
                    "mutationState": (
                        "UNKNOWN" if ambiguous_publication else "NOT_ATTEMPTED"
                    ),
                    "retryable": not ambiguous_publication,
                    "ambiguousPublication": ambiguous_publication,
                    "attemptCount": len(self._attempts),
                    "providerRequestId": request_id,
                    "traceId": self._request.trace_id,
                    "attempts": deepcopy(self._attempts),
                },
            },
            failure_class=ToolFailureClass.TIMEOUT,
            failure_message=(
                f"GitHub provider exceeded timeout of "
                f"{self._request.timeout_ms}ms"
            ),
        )

    def _result(
        self,
        status: ToolResultStatus,
        output: Any,
        *,
        failure_class: ToolFailureClass | None = None,
        failure_message: str | None = None,
    ) -> ToolResult:
        return ToolResult(
            status=status,
            output=output,
            logs_ref=None,
            metrics=ToolMetrics(
                duration_ms=max(
                    0, round((self._clock() - self._started_clock) * 1000)
                )
            ),
            started_at=self._started_at.isoformat().replace("+00:00", "Z"),
            completed_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            failure_class=failure_class,
            failure_message=failure_message,
        )


def invoke_github_tool(
    request: ToolRequest,
    *,
    pre_execute_authorize: Callable[[ToolRequest], bool],
    adapter: GitHubToolAdapter,
    publication_verifier: PublicationPolicyVerifier | None = None,
) -> ToolResult:
    """Validate, verify publication evidence, authorize, and invoke GitHub."""

    validator = JsonSchemaToolValidator(GITHUB_INPUT_SCHEMA, GITHUB_OUTPUT_SCHEMA)
    try:
        validator.validate_input(request.input)
    except ToolSchemaValidationError:
        return invoke_tool(
            request,
            validator=validator,
            authorize=pre_execute_authorize,
            adapter=adapter,
        )

    operation = request.input["operation"]
    expected = (READ_CAPABILITY,) if operation == READ_ISSUE else (CREATE_PR_CAPABILITY,)
    if tuple(request.capabilities) != expected:
        return _policy_failure(
            request,
            "request capabilities do not match the GitHub operation",
            gate="pre-execution-capability",
        )
    if operation == CREATE_PULL_REQUEST:
        if publication_verifier is None:
            return _policy_failure(
                request,
                "trusted Publication Policy verifier is required",
                gate="publication-policy",
            )
        verification = publication_verifier.verify(request)
        if not verification.allowed:
            return _policy_failure(
                request, verification.reason, gate="publication-policy"
            )
    return invoke_tool(
        request,
        validator=validator,
        authorize=pre_execute_authorize,
        adapter=adapter,
    )


def _attempt_record(attempt: int, error: GitHubProviderError) -> dict[str, Any]:
    return {
        "attempt": attempt,
        "providerRequestId": error.request_id,
        "classification": error.classification,
        "retryable": error.retryable,
        "retryAfterMs": error.retry_after_ms,
        "outcome": "FAILED",
    }


def _issue_output(
    request: ToolRequest, provider: JsonObject, attempts: Sequence[JsonObject]
) -> dict[str, Any]:
    return {
        "operation": READ_ISSUE,
        "repository": request.input["repository"],
        "issue": {
            "number": provider["number"],
            "title": provider["title"],
            "body": provider.get("body"),
            "state": provider["state"],
            "url": provider["url"],
            "author": provider.get("author"),
            "labels": list(provider.get("labels", [])),
        },
        "metadata": _metadata(request, provider.get("requestId"), attempts),
    }


def _pull_request_output(
    request: ToolRequest, provider: JsonObject, attempts: Sequence[JsonObject]
) -> dict[str, Any]:
    return {
        "operation": CREATE_PULL_REQUEST,
        "repository": request.input["repository"],
        "pullRequest": {
            "number": provider["number"],
            "url": provider["url"],
            "head": request.input["head"],
            "base": request.input["base"],
        },
        "metadata": _metadata(request, provider.get("requestId"), attempts),
    }


def _metadata(
    request: ToolRequest, provider_request_id: Any, attempts: Sequence[JsonObject]
) -> dict[str, Any]:
    return {
        "traceId": request.trace_id,
        "providerRequestId": (
            provider_request_id if isinstance(provider_request_id, str) else None
        ),
        "attemptCount": len(attempts),
        "attempts": deepcopy(list(attempts)),
    }


def _policy_failure(request: ToolRequest, message: str, *, gate: str) -> ToolResult:
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return ToolResult(
        status=ToolResultStatus.DENIED,
        output={
            "operation": request.input.get("operation"),
            "repository": request.input.get("repository"),
            "failure": {
                "category": "POLICY",
                "gate": gate,
                "retryable": False,
                "traceId": request.trace_id,
            },
        },
        logs_ref=None,
        metrics=ToolMetrics(duration_ms=0),
        started_at=now,
        completed_at=now,
        failure_class=ToolFailureClass.POLICY,
        failure_message=message,
    )
