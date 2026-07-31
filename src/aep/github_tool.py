"""GitHub Tool adapter with explicit publication and capability gates."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
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
    invoke_tool,
)


JsonObject = Mapping[str, Any]
READ_ISSUE = "readIssue"
CREATE_PULL_REQUEST = "createPullRequest"
READ_CAPABILITY = "github.issue.read"
CREATE_PR_CAPABILITY = "github.create_pr"


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
                "technicalEvaluation",
                "publicationPolicy",
            ],
            "properties": {
                "operation": {"const": CREATE_PULL_REQUEST},
                "repository": {"type": "string", "pattern": r"^[^/\s]+/[^/\s]+$"},
                "head": {"type": "string", "minLength": 1},
                "base": {"type": "string", "minLength": 1},
                "title": {"type": "string", "minLength": 1},
                "body": {"type": "string"},
                "issueNumber": {"type": "integer", "minimum": 1},
                "technicalEvaluation": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["outcome", "evaluationResultIds", "traceId"],
                    "properties": {
                        "outcome": {"enum": ["PASS", "FAIL"]},
                        "evaluationResultIds": {
                            "type": "array",
                            "minItems": 1,
                            "uniqueItems": True,
                            "items": {"type": "string", "minLength": 1},
                        },
                        "traceId": {"type": "string", "minLength": 1},
                    },
                },
                "publicationPolicy": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "decision",
                        "policyDecisionId",
                        "action",
                        "traceId",
                    ],
                    "properties": {
                        "decision": {
                            "enum": ["ALLOW", "DENY", "REQUIRE_APPROVAL"]
                        },
                        "policyDecisionId": {"type": "string", "minLength": 1},
                        "action": {"const": CREATE_PR_CAPABILITY},
                        "traceId": {"type": "string", "minLength": 1},
                    },
                },
            },
        },
    ],
}


_METADATA_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["traceId", "providerRequestId", "attemptCount"],
    "properties": {
        "traceId": {"type": "string", "minLength": 1},
        "providerRequestId": {"type": ["string", "null"]},
        "attemptCount": {"type": "integer", "minimum": 1},
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
                        "labels": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
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


class GitHubClient(Protocol):
    """Provider-neutral client boundary implemented by a GitHub integration."""

    def read_issue(self, repository: str, issue_number: int) -> JsonObject:
        """Return provider issue data and an optional ``requestId``."""

    def create_pull_request(
        self,
        repository: str,
        *,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> JsonObject:
        """Create a pull request and return provider data."""


class GitHubProviderError(RuntimeError):
    """Provider failure carrying stable retry and request metadata."""

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


@dataclass
class _CompletedExecution(ToolExecution):
    result: ToolResult
    cleaned_up: bool = False

    def wait(self, timeout_ms: int) -> ToolResult:
        return self.result

    def terminate(self) -> None:
        return None

    def kill(self) -> None:
        return None

    def cleanup(self) -> None:
        self.cleaned_up = True


class GitHubToolAdapter(ToolAdapter):
    """Execute structured GitHub operations against an injected client."""

    def __init__(self, client: GitHubClient, *, max_read_attempts: int = 2) -> None:
        if max_read_attempts < 1:
            raise ValueError("max_read_attempts must be positive")
        self._client = client
        self._max_read_attempts = max_read_attempts

    def start(self, request: ToolRequest) -> ToolExecution:
        started_at = datetime.now(UTC)
        started_clock = monotonic()
        operation = request.input["operation"]
        attempts = self._max_read_attempts if operation == READ_ISSUE else 1

        for attempt in range(1, attempts + 1):
            try:
                if operation == READ_ISSUE:
                    provider = self._client.read_issue(
                        request.input["repository"], request.input["issueNumber"]
                    )
                    output = _issue_output(request, provider, attempt)
                else:
                    provider = self._client.create_pull_request(
                        request.input["repository"],
                        head=request.input["head"],
                        base=request.input["base"],
                        title=request.input["title"],
                        body=request.input["body"],
                    )
                    output = _pull_request_output(request, provider, attempt)
                return _CompletedExecution(
                    _result(
                        started_at,
                        started_clock,
                        ToolResultStatus.SUCCEEDED,
                        output,
                    )
                )
            except GitHubProviderError as error:
                if error.retryable and attempt < attempts:
                    continue
                return _CompletedExecution(
                    _provider_failure(
                        request, started_at, started_clock, error, attempt
                    )
                )
            except Exception as error:
                normalized = GitHubProviderError(str(error) or type(error).__name__)
                return _CompletedExecution(
                    _provider_failure(
                        request, started_at, started_clock, normalized, attempt
                    )
                )

        raise AssertionError("GitHub attempt loop did not return")


def invoke_github_tool(
    request: ToolRequest,
    *,
    pre_execute_authorize: Callable[[ToolRequest], bool],
    adapter: GitHubToolAdapter,
) -> ToolResult:
    """Apply technical, publication, then pre-execution gates and invoke GitHub."""

    validator = JsonSchemaToolValidator(GITHUB_INPUT_SCHEMA, GITHUB_OUTPUT_SCHEMA)
    try:
        validator.validate_input(request.input)
    except ToolSchemaValidationError:
        # The common runtime produces the canonical validation failure.
        return invoke_tool(
            request,
            validator=validator,
            authorize=pre_execute_authorize,
            adapter=adapter,
        )

    operation = request.input["operation"]
    expected = (
        (READ_CAPABILITY,)
        if operation == READ_ISSUE
        else (CREATE_PR_CAPABILITY,)
    )
    if tuple(request.capabilities) != expected:
        return _policy_failure(
            request,
            "request capabilities do not match the GitHub operation",
            gate="pre-execution-capability",
        )

    if operation == CREATE_PULL_REQUEST:
        if request.input["technicalEvaluation"]["traceId"] != request.trace_id:
            return _policy_failure(
                request,
                "technical evaluation trace does not match the Tool trace",
                gate="technical-evaluation",
            )
        if request.input["technicalEvaluation"]["outcome"] != "PASS":
            return _policy_failure(
                request,
                "technical evaluation did not pass",
                gate="technical-evaluation",
            )
        if request.input["publicationPolicy"]["traceId"] != request.trace_id:
            return _policy_failure(
                request,
                "Publication Policy trace does not match the Tool trace",
                gate="publication-policy",
            )
        publication = request.input["publicationPolicy"]["decision"]
        if publication != "ALLOW":
            return _policy_failure(
                request,
                f"Publication Policy decision was {publication}",
                gate="publication-policy",
            )

    return invoke_tool(
        request,
        validator=validator,
        authorize=pre_execute_authorize,
        adapter=adapter,
    )


def _issue_output(
    request: ToolRequest, provider: JsonObject, attempt: int
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
        "metadata": _metadata(request, provider.get("requestId"), attempt),
    }


def _pull_request_output(
    request: ToolRequest, provider: JsonObject, attempt: int
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
        "metadata": _metadata(request, provider.get("requestId"), attempt),
    }


def _metadata(
    request: ToolRequest, provider_request_id: Any, attempt: int
) -> dict[str, Any]:
    return {
        "traceId": request.trace_id,
        "providerRequestId": (
            provider_request_id if isinstance(provider_request_id, str) else None
        ),
        "attemptCount": attempt,
    }


def _provider_failure(
    request: ToolRequest,
    started_at: datetime,
    started_clock: float,
    error: GitHubProviderError,
    attempt: int,
) -> ToolResult:
    category = "RATE_LIMIT" if isinstance(error, GitHubRateLimitError) else "PROVIDER"
    return _result(
        started_at,
        started_clock,
        ToolResultStatus.FAILED,
        {
            "operation": request.input["operation"],
            "repository": request.input["repository"],
            "failure": {
                "category": category,
                "retryable": error.retryable,
                "retryAfterMs": error.retry_after_ms,
                "attemptCount": attempt,
                "providerRequestId": error.request_id,
                "traceId": request.trace_id,
            },
        },
        failure_class=ToolFailureClass.ADAPTER,
        failure_message=str(error),
    )


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


def _result(
    started_at: datetime,
    started_clock: float,
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
            duration_ms=max(0, round((monotonic() - started_clock) * 1000))
        ),
        started_at=started_at.isoformat().replace("+00:00", "Z"),
        completed_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        failure_class=failure_class,
        failure_message=failure_message,
    )
