from collections.abc import Mapping
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
import pytest

from aep.github_tool import (
    CREATE_PR_CAPABILITY,
    CREATE_PULL_REQUEST,
    GITHUB_INPUT_SCHEMA,
    GITHUB_OUTPUT_SCHEMA,
    READ_CAPABILITY,
    READ_ISSUE,
    GitHubProviderError,
    GitHubRateLimitError,
    GitHubToolAdapter,
    invoke_github_tool,
)
from aep.tool_runtime import (
    ToolCaller,
    ToolFailureClass,
    ToolRequest,
    ToolResultStatus,
)


ROOT = Path(__file__).parents[1]


class FakeGitHubClient:
    def __init__(
        self,
        *,
        issue_outcomes: list[Mapping[str, Any] | Exception] | None = None,
        pull_request_outcomes: list[Mapping[str, Any] | Exception] | None = None,
    ) -> None:
        self.issue_outcomes = issue_outcomes or []
        self.pull_request_outcomes = pull_request_outcomes or []
        self.issue_calls: list[tuple[str, int]] = []
        self.pull_request_calls: list[dict[str, str]] = []

    def read_issue(self, repository: str, issue_number: int) -> Mapping[str, Any]:
        self.issue_calls.append((repository, issue_number))
        return self._next(self.issue_outcomes)

    def create_pull_request(
        self,
        repository: str,
        *,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> Mapping[str, Any]:
        self.pull_request_calls.append(
            {
                "repository": repository,
                "head": head,
                "base": base,
                "title": title,
                "body": body,
            }
        )
        return self._next(self.pull_request_outcomes)

    @staticmethod
    def _next(outcomes: list[Mapping[str, Any] | Exception]) -> Mapping[str, Any]:
        if not outcomes:
            raise AssertionError("fake client has no configured outcome")
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def request(
    input_value: dict[str, Any],
    *,
    capabilities: tuple[str, ...],
) -> ToolRequest:
    return ToolRequest(
        tool_ref={"kind": "Tool", "name": "github", "version": "1.0.0"},
        input=input_value,
        caller=ToolCaller(kind="TaskExecution", id="taskexecution-github123456"),
        capabilities=capabilities,
        timeout_ms=5000,
        trace_id="trace-github-001",
    )


def read_input() -> dict[str, Any]:
    return {
        "operation": READ_ISSUE,
        "repository": "acme/widgets",
        "issueNumber": 42,
    }


def create_input(
    *,
    technical_outcome: str = "PASS",
    publication_decision: str = "ALLOW",
) -> dict[str, Any]:
    return {
        "operation": CREATE_PULL_REQUEST,
        "repository": "acme/widgets",
        "issueNumber": 42,
        "head": "aep/issue-42",
        "base": "main",
        "title": "Implement issue 42",
        "body": "Closes #42",
        "technicalEvaluation": {
            "outcome": technical_outcome,
            "evaluationResultIds": ["evaluationresult-build123456"],
            "traceId": "trace-github-001",
        },
        "publicationPolicy": {
            "decision": publication_decision,
            "policyDecisionId": "policydecision-publication123456",
            "action": CREATE_PR_CAPABILITY,
            "traceId": "trace-github-001",
        },
    }


def issue_response() -> dict[str, Any]:
    return {
        "number": 42,
        "title": "Add widgets",
        "body": "Please add widgets.",
        "state": "open",
        "url": "https://github.com/acme/widgets/issues/42",
        "author": "octocat",
        "labels": ["feature"],
        "requestId": "github-request-read-1",
    }


def pull_request_response() -> dict[str, Any]:
    return {
        "number": 84,
        "url": "https://github.com/acme/widgets/pull/84",
        "requestId": "github-request-create-1",
    }


def test_read_issue_returns_structured_data_and_provider_metadata() -> None:
    client = FakeGitHubClient(issue_outcomes=[issue_response()])

    result = invoke_github_tool(
        request(read_input(), capabilities=(READ_CAPABILITY,)),
        pre_execute_authorize=lambda _: True,
        adapter=GitHubToolAdapter(client),
    )

    assert result.status is ToolResultStatus.SUCCEEDED
    assert result.output["repository"] == "acme/widgets"
    assert result.output["issue"] == {
        "number": 42,
        "title": "Add widgets",
        "body": "Please add widgets.",
        "state": "open",
        "url": "https://github.com/acme/widgets/issues/42",
        "author": "octocat",
        "labels": ("feature",),
    }
    assert result.output["metadata"] == {
        "traceId": "trace-github-001",
        "providerRequestId": "github-request-read-1",
        "attemptCount": 1,
    }
    assert client.issue_calls == [("acme/widgets", 42)]


def test_create_pull_request_runs_all_gates_before_publication() -> None:
    events: list[str] = []
    client = FakeGitHubClient(pull_request_outcomes=[pull_request_response()])

    def authorize(tool_request: ToolRequest) -> bool:
        events.append("pre-execution")
        assert tool_request.capabilities == (CREATE_PR_CAPABILITY,)
        return True

    result = invoke_github_tool(
        request(create_input(), capabilities=(CREATE_PR_CAPABILITY,)),
        pre_execute_authorize=authorize,
        adapter=GitHubToolAdapter(client),
    )

    assert result.status is ToolResultStatus.SUCCEEDED
    assert events == ["pre-execution"]
    assert client.pull_request_calls == [
        {
            "repository": "acme/widgets",
            "head": "aep/issue-42",
            "base": "main",
            "title": "Implement issue 42",
            "body": "Closes #42",
        }
    ]
    assert result.output["pullRequest"] == {
        "number": 84,
        "url": "https://github.com/acme/widgets/pull/84",
        "head": "aep/issue-42",
        "base": "main",
    }
    assert result.output["metadata"]["providerRequestId"] == "github-request-create-1"


@pytest.mark.parametrize(
    "technical,publication,gate,message",
    [
        ("FAIL", "ALLOW", "technical-evaluation", "technical evaluation"),
        ("PASS", "DENY", "publication-policy", "Publication Policy"),
        ("PASS", "REQUIRE_APPROVAL", "publication-policy", "Publication Policy"),
    ],
)
def test_publication_is_blocked_before_pre_execution_authorization(
    technical: str,
    publication: str,
    gate: str,
    message: str,
) -> None:
    client = FakeGitHubClient(pull_request_outcomes=[pull_request_response()])
    authorization_calls: list[ToolRequest] = []

    result = invoke_github_tool(
        request(
            create_input(
                technical_outcome=technical,
                publication_decision=publication,
            ),
            capabilities=(CREATE_PR_CAPABILITY,),
        ),
        pre_execute_authorize=lambda value: authorization_calls.append(value) or True,
        adapter=GitHubToolAdapter(client),
    )

    assert result.status is ToolResultStatus.DENIED
    assert result.failure_class is ToolFailureClass.POLICY
    assert message in result.failure_message
    assert result.output["failure"]["gate"] == gate
    assert result.output["failure"]["retryable"] is False
    assert authorization_calls == []
    assert client.pull_request_calls == []


def test_pre_execution_denial_blocks_provider_call() -> None:
    client = FakeGitHubClient(pull_request_outcomes=[pull_request_response()])

    result = invoke_github_tool(
        request(create_input(), capabilities=(CREATE_PR_CAPABILITY,)),
        pre_execute_authorize=lambda _: False,
        adapter=GitHubToolAdapter(client),
    )

    assert result.status is ToolResultStatus.DENIED
    assert result.failure_class is ToolFailureClass.POLICY
    assert client.pull_request_calls == []


@pytest.mark.parametrize(
    "evidence,gate",
    [
        ("technicalEvaluation", "technical-evaluation"),
        ("publicationPolicy", "publication-policy"),
    ],
)
def test_publication_evidence_must_match_the_tool_trace(
    evidence: str, gate: str
) -> None:
    client = FakeGitHubClient(pull_request_outcomes=[pull_request_response()])
    input_value = create_input()
    input_value[evidence]["traceId"] = "trace-other-execution"

    result = invoke_github_tool(
        request(input_value, capabilities=(CREATE_PR_CAPABILITY,)),
        pre_execute_authorize=lambda _: True,
        adapter=GitHubToolAdapter(client),
    )

    assert result.status is ToolResultStatus.DENIED
    assert result.output["failure"]["gate"] == gate
    assert client.pull_request_calls == []


def test_capability_mismatch_is_denied_before_authorization() -> None:
    client = FakeGitHubClient(pull_request_outcomes=[pull_request_response()])
    authorization_calls: list[ToolRequest] = []

    result = invoke_github_tool(
        request(create_input(), capabilities=(READ_CAPABILITY,)),
        pre_execute_authorize=lambda value: authorization_calls.append(value) or True,
        adapter=GitHubToolAdapter(client),
    )

    assert result.status is ToolResultStatus.DENIED
    assert result.output["failure"]["gate"] == "pre-execution-capability"
    assert authorization_calls == []
    assert client.pull_request_calls == []


def test_invalid_input_is_rejected_without_authorization_or_provider_call() -> None:
    client = FakeGitHubClient(issue_outcomes=[issue_response()])
    invalid = read_input()
    invalid["issueNumber"] = 0
    authorization_calls: list[ToolRequest] = []

    result = invoke_github_tool(
        request(invalid, capabilities=(READ_CAPABILITY,)),
        pre_execute_authorize=lambda value: authorization_calls.append(value) or True,
        adapter=GitHubToolAdapter(client),
    )

    assert result.status is ToolResultStatus.FAILED
    assert result.failure_class is ToolFailureClass.VALIDATION
    assert authorization_calls == []
    assert client.issue_calls == []


def test_rate_limited_read_retries_and_records_attempt_count() -> None:
    client = FakeGitHubClient(
        issue_outcomes=[
            GitHubRateLimitError(
                request_id="github-rate-1", retry_after_ms=1000
            ),
            issue_response(),
        ]
    )

    result = invoke_github_tool(
        request(read_input(), capabilities=(READ_CAPABILITY,)),
        pre_execute_authorize=lambda _: True,
        adapter=GitHubToolAdapter(client, max_read_attempts=2),
    )

    assert result.status is ToolResultStatus.SUCCEEDED
    assert result.output["metadata"]["attemptCount"] == 2
    assert client.issue_calls == [("acme/widgets", 42), ("acme/widgets", 42)]


def test_exhausted_rate_limit_has_stable_recoverable_failure() -> None:
    client = FakeGitHubClient(
        issue_outcomes=[
            GitHubRateLimitError(
                request_id="github-rate-1", retry_after_ms=1000
            ),
            GitHubRateLimitError(
                request_id="github-rate-2", retry_after_ms=2000
            ),
        ]
    )

    result = invoke_github_tool(
        request(read_input(), capabilities=(READ_CAPABILITY,)),
        pre_execute_authorize=lambda _: True,
        adapter=GitHubToolAdapter(client, max_read_attempts=2),
    )

    assert result.status is ToolResultStatus.FAILED
    assert result.failure_class is ToolFailureClass.ADAPTER
    assert result.output["failure"] == {
        "category": "RATE_LIMIT",
        "retryable": True,
        "retryAfterMs": 2000,
        "attemptCount": 2,
        "providerRequestId": "github-rate-2",
        "traceId": "trace-github-001",
    }


def test_create_pull_request_is_not_retried_after_provider_failure() -> None:
    client = FakeGitHubClient(
        pull_request_outcomes=[
            GitHubProviderError(
                "service unavailable",
                retryable=True,
                request_id="github-create-failed",
            ),
            pull_request_response(),
        ]
    )

    result = invoke_github_tool(
        request(create_input(), capabilities=(CREATE_PR_CAPABILITY,)),
        pre_execute_authorize=lambda _: True,
        adapter=GitHubToolAdapter(client),
    )

    assert result.status is ToolResultStatus.FAILED
    assert result.output["failure"]["category"] == "PROVIDER"
    assert result.output["failure"]["retryable"] is True
    assert result.output["failure"]["attemptCount"] == 1
    assert len(client.pull_request_calls) == 1


def test_provider_contract_failure_is_permanent_and_traceable() -> None:
    client = FakeGitHubClient(issue_outcomes=[RuntimeError("bad provider response")])

    result = invoke_github_tool(
        request(read_input(), capabilities=(READ_CAPABILITY,)),
        pre_execute_authorize=lambda _: True,
        adapter=GitHubToolAdapter(client),
    )

    assert result.status is ToolResultStatus.FAILED
    assert result.failure_message == "bad provider response"
    assert result.output["failure"]["retryable"] is False
    assert result.output["failure"]["traceId"] == "trace-github-001"


@pytest.mark.parametrize(
    "schema_name,module_schema",
    [
        ("github-input.schema.json", GITHUB_INPUT_SCHEMA),
        ("github-output.schema.json", GITHUB_OUTPUT_SCHEMA),
    ],
)
def test_published_github_schemas_are_valid(
    schema_name: str, module_schema: dict[str, Any]
) -> None:
    published = json.loads(
        (ROOT / "schemas/tools/v1" / schema_name).read_text(encoding="utf-8")
    )

    Draft202012Validator.check_schema(published)
    Draft202012Validator.check_schema(module_schema)


@pytest.mark.parametrize(
    "fixture_name",
    ["read-issue-success.json", "create-pull-request-success.json"],
)
def test_github_fixtures_match_published_contracts(fixture_name: str) -> None:
    fixture = json.loads(
        (ROOT / "fixtures/github-tool" / fixture_name).read_text(encoding="utf-8")
    )
    input_schema = json.loads(
        (ROOT / "schemas/tools/v1/github-input.schema.json").read_text(
            encoding="utf-8"
        )
    )
    output_schema = json.loads(
        (ROOT / "schemas/tools/v1/github-output.schema.json").read_text(
            encoding="utf-8"
        )
    )

    Draft202012Validator(input_schema).validate(fixture["input"])
    Draft202012Validator(output_schema).validate(fixture["output"])
