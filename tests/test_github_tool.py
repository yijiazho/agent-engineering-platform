from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
import pytest
from referencing import Registry, Resource as SchemaResource
from referencing.jsonschema import DRAFT202012

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
    PersistedPublicationPolicyVerifier,
    PublicationVerification,
    invoke_github_tool,
)
from aep.tool_runtime import (
    ToolCaller,
    ToolFailureClass,
    ToolRequest,
    ToolResultStatus,
)


ROOT = Path(__file__).parents[1]
TASK_ID = "taskexecution-aaaaaaaaaaaa"
PATCH_TASK_ID = "taskexecution-111111111111"
VALIDATION_TASK_ID = "taskexecution-222222222222"
ACCEPTANCE_TASK_ID = "taskexecution-333333333333"
WORKFLOW_ID = "workflowexecution-bbbbbbbbbbbb"
ARTIFACT_ID = "generatedartifact-cccccccccccc"
EVALUATION_ID = "evaluationresult-dddddddddddd"
ACCEPTANCE_EVALUATION_ID = "evaluationresult-444444444444"
POLICY_ID = "policydecision-eeeeeeeeeeee"
PUSH_ID = "toolinvocation-ffffffffffff"
PUSH_POLICY_ID = "policydecision-555555555555"
REVISION = "abc1234"
TRACE_ID = "trace-github-001"


@dataclass(frozen=True)
class PendingProviderResponse:
    request_id: str


class FakeProviderOperation:
    def __init__(
        self,
        outcome: Mapping[str, Any] | Exception | None,
        request_id: str | None = None,
    ) -> None:
        self.outcome = outcome
        self.request_id = request_id
        self.wait_timeouts: list[int] = []
        self.terminated = False
        self.killed = False
        self.cleaned_up = False

    def wait(self, timeout_ms: int) -> Mapping[str, Any] | Exception | None:
        self.wait_timeouts.append(timeout_ms)
        return self.outcome

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def cleanup(self) -> None:
        self.cleaned_up = True


class FakeGitHubClient:
    def __init__(
        self,
        *,
        issue_outcomes: list[
            Mapping[str, Any] | Exception | PendingProviderResponse | None
        ] | None = None,
        pull_request_outcomes: list[
            Mapping[str, Any] | Exception | PendingProviderResponse | None
        ] | None = None,
    ) -> None:
        self.issue_outcomes = issue_outcomes or []
        self.pull_request_outcomes = pull_request_outcomes or []
        self.issue_calls: list[tuple[str, int]] = []
        self.pull_request_calls: list[dict[str, str]] = []
        self.operations: list[FakeProviderOperation] = []

    def start_read_issue(
        self, repository: str, issue_number: int
    ) -> FakeProviderOperation:
        self.issue_calls.append((repository, issue_number))
        return self._start(self.issue_outcomes)

    def start_create_pull_request(
        self,
        repository: str,
        *,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> FakeProviderOperation:
        self.pull_request_calls.append(
            {
                "repository": repository,
                "head": head,
                "base": base,
                "title": title,
                "body": body,
            }
        )
        return self._start(self.pull_request_outcomes)

    def _start(
        self,
        outcomes: list[
            Mapping[str, Any] | Exception | PendingProviderResponse | None
        ],
    ) -> FakeProviderOperation:
        if not outcomes:
            raise AssertionError("fake client has no configured outcome")
        outcome = outcomes.pop(0)
        request_id = (
            outcome.get("requestId")
            if isinstance(outcome, Mapping)
            else getattr(outcome, "request_id", None)
        )
        operation = FakeProviderOperation(
            None if isinstance(outcome, PendingProviderResponse) else outcome,
            request_id,
        )
        self.operations.append(operation)
        return operation


class FakePublicationVerifier:
    def __init__(self, result: PublicationVerification) -> None:
        self.result = result
        self.requests: list[ToolRequest] = []

    def verify(self, request: ToolRequest) -> PublicationVerification:
        self.requests.append(request)
        return self.result


class FakeClock:
    def __init__(self) -> None:
        self.seconds = 100.0
        self.waits: list[int] = []

    def __call__(self) -> float:
        return self.seconds

    def wait(self, milliseconds: int) -> None:
        self.waits.append(milliseconds)
        self.seconds += milliseconds / 1000


def request(
    input_value: dict[str, Any],
    *,
    capabilities: tuple[str, ...],
    timeout_ms: int = 5000,
) -> ToolRequest:
    return ToolRequest(
        tool_ref={"kind": "Tool", "name": "github", "version": "1.0.0"},
        input=input_value,
        caller=ToolCaller(kind="TaskExecution", id=TASK_ID),
        capabilities=capabilities,
        timeout_ms=timeout_ms,
        correlation={
            "traceId": TRACE_ID,
            "workflowExecutionId": WORKFLOW_ID,
            "taskExecutionId": TASK_ID,
        },
    )


def read_input() -> dict[str, Any]:
    return {
        "operation": READ_ISSUE,
        "repository": "acme/widgets",
        "issueNumber": 42,
    }


def create_input() -> dict[str, Any]:
    return {
        "operation": CREATE_PULL_REQUEST,
        "repository": "acme/widgets",
        "issueNumber": 42,
        "head": "aep/issue-42",
        "base": "main",
        "title": "Implement issue 42",
        "body": "Closes #42",
        "publicationEvidence": {
            "policyDecisionId": POLICY_ID,
            "taskExecutionId": TASK_ID,
            "workflowExecutionId": WORKFLOW_ID,
            "repositoryRevision": REVISION,
            "evaluationResultIds": [EVALUATION_ID, ACCEPTANCE_EVALUATION_ID],
            "generatedArtifactIds": [ARTIFACT_ID],
            "pushToolInvocationId": PUSH_ID,
        },
    }


def persisted_evidence() -> dict[str, dict[str, Any]]:
    def base(kind: str, runtime_id: str, task_id: str, actor: str) -> dict[str, Any]:
        return {
            "apiVersion": "aep.dev/v1alpha1",
            "kind": kind,
            "id": runtime_id,
            "traceId": TRACE_ID,
            "createdAt": "2026-07-30T12:00:00Z",
            "updatedAt": "2026-07-30T12:00:00Z",
            "provenance": {
                "actor": actor,
                "workflowExecutionId": WORKFLOW_ID,
                "taskExecutionId": task_id,
                "repositoryRevision": REVISION,
                "resourceRefs": [],
            },
        }

    artifact = base(
        "GeneratedArtifact", ARTIFACT_ID, PATCH_TASK_ID, "artifact-store"
    ) | {
        "kind": "GeneratedArtifact",
        "taskExecutionId": PATCH_TASK_ID,
        "artifactType": "PATCH",
        "contentAddress": "sha256:" + "4" * 64,
        "repositoryRevision": REVISION,
        "evaluationResultIds": [EVALUATION_ID, ACCEPTANCE_EVALUATION_ID],
        "mediaType": "text/x-diff",
    }
    evaluation = base(
        "EvaluationResult",
        EVALUATION_ID,
        VALIDATION_TASK_ID,
        "evaluation-engine",
    ) | {
        "kind": "EvaluationResult",
        "taskExecutionId": VALIDATION_TASK_ID,
        "evaluationRef": {
            "kind": "Evaluation",
            "name": "build-and-test",
            "version": "1.0.0",
        },
        "status": "SUCCEEDED",
        "outcome": "PASS",
        "target": {"type": "TaskExecution", "id": VALIDATION_TASK_ID},
    }
    acceptance_evaluation = base(
        "EvaluationResult",
        ACCEPTANCE_EVALUATION_ID,
        ACCEPTANCE_TASK_ID,
        "evaluation-engine",
    ) | {
        "taskExecutionId": ACCEPTANCE_TASK_ID,
        "evaluationRef": {
            "kind": "Evaluation",
            "name": "acceptance",
            "version": "1.0.0",
        },
        "status": "SUCCEEDED",
        "outcome": "PASS",
        "target": {"type": "GeneratedArtifact", "id": ARTIFACT_ID},
    }
    git_tool_ref = {"kind": "Tool", "name": "git", "version": "1.0.0"}
    push = base("ToolInvocation", PUSH_ID, TASK_ID, "tool-runtime") | {
        "taskExecutionId": TASK_ID,
        "toolRef": {"kind": "Tool", "name": "git", "version": "1.0.0"},
        "status": "SUCCEEDED",
        "input": {
            "operation": "push",
            "repository": "acme/widgets",
            "branch": "aep/issue-42",
            "revision": REVISION,
        },
        "output": {
            "operation": "push",
            "repository": "acme/widgets",
            "branch": "aep/issue-42",
            "revision": REVISION,
        },
        "policyDecisionId": PUSH_POLICY_ID,
    }
    push_policy = base(
        "PolicyDecision", PUSH_POLICY_ID, TASK_ID, "capability-policy"
    ) | {
        "taskExecutionId": TASK_ID,
        "gate": "PRE_EXECUTION_CAPABILITY",
        "policyRefs": [
            {
                "kind": "Policy",
                "name": "git-push",
                "version": "1.0.0",
            }
        ],
        "action": "git.push",
        "decision": "ALLOW",
        "reason": "The approved branch may be pushed.",
        "approvalRequired": False,
        "evaluatedAt": "2026-07-30T12:00:00Z",
        "subject": f"TaskExecution:{TASK_ID}",
        "resourceScope": {
            "repository": "acme/widgets",
            "branch": "aep/issue-42",
            "repositoryRevision": REVISION,
            "toolRef": git_tool_ref,
        },
        "evaluatedRule": {
            "scope": "Tool",
            "policyRef": {
                "kind": "Policy",
                "name": "git-push",
                "version": "1.0.0",
            },
            "ruleIndex": 0,
            "effect": "allow",
        },
        "matchedRules": [
            {
                "scope": "Tool",
                "policyRef": {
                    "kind": "Policy",
                    "name": "git-push",
                    "version": "1.0.0",
                },
                "ruleIndex": 0,
                "effect": "allow",
            }
        ],
    }
    policy = base(
        "PolicyDecision", POLICY_ID, TASK_ID, "publication-policy"
    ) | {
        "kind": "PolicyDecision",
        "taskExecutionId": TASK_ID,
        "gate": "PUBLICATION",
        "action": CREATE_PR_CAPABILITY,
        "decision": "ALLOW",
        "reason": "Evaluated output may be published.",
        "policyRefs": [
            {
                "kind": "Policy",
                "name": "publication",
                "version": "1.0.0",
            }
        ],
        "repositoryRevision": REVISION,
        "evaluationResultIds": [EVALUATION_ID, ACCEPTANCE_EVALUATION_ID],
        "generatedArtifactIds": [ARTIFACT_ID],
        "resourceScope": {"repository": "acme/widgets"},
        "publicationTarget": {
            "repository": "acme/widgets",
            "head": "aep/issue-42",
            "base": "main",
            "repositoryRevision": REVISION,
            "pushToolInvocationId": PUSH_ID,
        },
    }
    return {
        ARTIFACT_ID: artifact,
        EVALUATION_ID: evaluation,
        ACCEPTANCE_EVALUATION_ID: acceptance_evaluation,
        PUSH_ID: push,
        PUSH_POLICY_ID: push_policy,
        POLICY_ID: policy,
    }


def trusted_verifier(
    evidence: dict[str, dict[str, Any]] | None = None,
) -> PersistedPublicationPolicyVerifier:
    records = evidence or persisted_evidence()
    return PersistedPublicationPolicyVerifier(
        lambda runtime_id: deepcopy(records.get(runtime_id))
    )


def issue_response(request_id: str = "github-request-read-1") -> dict[str, Any]:
    return {
        "number": 42,
        "title": "Add widgets",
        "body": "Please add widgets.",
        "state": "open",
        "url": "https://github.com/acme/widgets/issues/42",
        "author": "octocat",
        "labels": ["feature"],
        "requestId": request_id,
    }


def pull_request_response() -> dict[str, Any]:
    return {
        "number": 84,
        "url": "https://github.com/acme/widgets/pull/84",
        "requestId": "github-request-create-1",
    }


def test_read_issue_returns_structured_data_and_attempt_metadata() -> None:
    client = FakeGitHubClient(issue_outcomes=[issue_response()])

    result = invoke_github_tool(
        request(read_input(), capabilities=(READ_CAPABILITY,)),
        pre_execute_authorize=lambda _: True,
        adapter=GitHubToolAdapter(client),
    )

    assert result.status is ToolResultStatus.SUCCEEDED
    assert result.output["issue"]["number"] == 42
    assert result.output["issue"]["url"].endswith("/issues/42")
    assert result.output["metadata"]["traceId"] == TRACE_ID
    assert result.output["metadata"]["attempts"] == (
        {
            "attempt": 1,
            "providerRequestId": "github-request-read-1",
            "classification": "SUCCESS",
            "retryable": False,
            "retryAfterMs": None,
            "outcome": "SUCCEEDED",
        },
    )
    assert client.operations[0].cleaned_up is True


def test_create_uses_trusted_verifier_then_capability_authorization() -> None:
    order: list[str] = []
    client = FakeGitHubClient(pull_request_outcomes=[pull_request_response()])

    class OrderedVerifier(FakePublicationVerifier):
        def verify(self, request: ToolRequest) -> PublicationVerification:
            order.append("publication")
            return super().verify(request)

    verifier = OrderedVerifier(PublicationVerification(True, "allowed"))

    def authorize(_: ToolRequest) -> bool:
        order.append("pre-execution")
        return True

    result = invoke_github_tool(
        request(create_input(), capabilities=(CREATE_PR_CAPABILITY,)),
        pre_execute_authorize=authorize,
        publication_verifier=verifier,
        adapter=GitHubToolAdapter(client),
    )

    assert result.status is ToolResultStatus.SUCCEEDED
    assert order == ["publication", "pre-execution"]
    assert len(verifier.requests) == 1
    assert result.output["pullRequest"]["number"] == 84
    assert len(client.pull_request_calls) == 1


def test_caller_supplied_policy_decision_fields_are_rejected_by_schema() -> None:
    value = create_input()
    value["publicationEvidence"]["decision"] = "ALLOW"
    client = FakeGitHubClient(pull_request_outcomes=[pull_request_response()])

    result = invoke_github_tool(
        request(value, capabilities=(CREATE_PR_CAPABILITY,)),
        pre_execute_authorize=lambda _: True,
        publication_verifier=trusted_verifier(),
        adapter=GitHubToolAdapter(client),
    )

    assert result.failure_class is ToolFailureClass.VALIDATION
    assert client.pull_request_calls == []


@pytest.mark.parametrize(
    "mutation",
    [
        lambda records: records[POLICY_ID].update(gate="PRE_EXECUTION_CAPABILITY"),
        lambda records: records[POLICY_ID].update(action="git.push"),
        lambda records: records[POLICY_ID].update(decision="DENY"),
        lambda records: records[POLICY_ID].update(traceId="trace-other"),
        lambda records: records[POLICY_ID]["resourceScope"].update(
            repository="other/repo"
        ),
        lambda records: records[POLICY_ID].update(repositoryRevision="deadbee"),
        lambda records: records[EVALUATION_ID].update(outcome="FAIL"),
        lambda records: records[EVALUATION_ID]["target"].update(
            type="GeneratedArtifact", id="generatedartifact-999999999999"
        ),
        lambda records: records[ARTIFACT_ID].update(repositoryRevision="deadbee"),
        lambda records: records[ARTIFACT_ID]["provenance"].update(
            workflowExecutionId="workflowexecution-other123456"
        ),
    ],
)
def test_persisted_verifier_binds_complete_publication_evidence(mutation) -> None:
    records = persisted_evidence()
    mutation(records)
    client = FakeGitHubClient(pull_request_outcomes=[pull_request_response()])
    authorization_calls: list[ToolRequest] = []

    result = invoke_github_tool(
        request(create_input(), capabilities=(CREATE_PR_CAPABILITY,)),
        pre_execute_authorize=lambda value: authorization_calls.append(value) or True,
        publication_verifier=trusted_verifier(records),
        adapter=GitHubToolAdapter(client),
    )

    assert result.status is ToolResultStatus.DENIED
    assert result.output["failure"]["gate"] == "publication-policy"
    assert authorization_calls == []
    assert client.pull_request_calls == []


def test_fake_verifier_denial_blocks_capability_and_provider() -> None:
    verifier = FakePublicationVerifier(PublicationVerification(False, "denied"))
    client = FakeGitHubClient(pull_request_outcomes=[pull_request_response()])
    authorization_calls: list[ToolRequest] = []

    result = invoke_github_tool(
        request(create_input(), capabilities=(CREATE_PR_CAPABILITY,)),
        pre_execute_authorize=lambda value: authorization_calls.append(value) or True,
        publication_verifier=verifier,
        adapter=GitHubToolAdapter(client),
    )

    assert result.status is ToolResultStatus.DENIED
    assert result.failure_message == "denied"
    assert len(verifier.requests) == 1
    assert authorization_calls == []
    assert client.pull_request_calls == []


def test_persisted_verifier_binds_requesting_task_identity() -> None:
    value = create_input()
    value["publicationEvidence"]["taskExecutionId"] = (
        "taskexecution-other123456"
    )
    client = FakeGitHubClient(pull_request_outcomes=[pull_request_response()])

    result = invoke_github_tool(
        request(value, capabilities=(CREATE_PR_CAPABILITY,)),
        pre_execute_authorize=lambda _: True,
        publication_verifier=trusted_verifier(),
        adapter=GitHubToolAdapter(client),
    )

    assert result.status is ToolResultStatus.DENIED
    assert result.failure_message == "Publication task identity mismatch"
    assert client.pull_request_calls == []


def test_schema_valid_cross_task_publication_graph_is_allowed() -> None:
    records = persisted_evidence()
    registry = _runtime_registry()
    for runtime_id, value in records.items():
        schema_name = {
            ARTIFACT_ID: "generatedartifact",
            EVALUATION_ID: "evaluationresult",
            ACCEPTANCE_EVALUATION_ID: "evaluationresult",
            PUSH_ID: "toolinvocation",
            PUSH_POLICY_ID: "policydecision",
            POLICY_ID: "policydecision",
        }[runtime_id]
        schema = json.loads(
            (
                ROOT / "schemas/runtime/v1" / f"{schema_name}.schema.json"
            ).read_text(encoding="utf-8")
        )
        Draft202012Validator(schema, registry=registry).validate(value)

    client = FakeGitHubClient(pull_request_outcomes=[pull_request_response()])
    result = invoke_github_tool(
        request(create_input(), capabilities=(CREATE_PR_CAPABILITY,)),
        pre_execute_authorize=lambda _: True,
        publication_verifier=trusted_verifier(records),
        adapter=GitHubToolAdapter(client),
    )

    assert result.status is ToolResultStatus.SUCCEEDED
    assert records[ARTIFACT_ID]["taskExecutionId"] == PATCH_TASK_ID
    assert records[EVALUATION_ID]["taskExecutionId"] == VALIDATION_TASK_ID
    assert (
        records[ACCEPTANCE_EVALUATION_ID]["taskExecutionId"]
        == ACCEPTANCE_TASK_ID
    )
    assert records[POLICY_ID]["taskExecutionId"] == TASK_ID
    assert records[PUSH_ID]["taskExecutionId"] == TASK_ID


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("head", "changed-head", "Git push input mismatch"),
        ("base", "release", "Publication target mismatch"),
    ],
)
def test_changed_publication_target_is_denied_before_capability(
    field: str, value: str, message: str
) -> None:
    input_value = create_input()
    input_value[field] = value
    client = FakeGitHubClient(pull_request_outcomes=[pull_request_response()])
    authorization_calls: list[ToolRequest] = []

    result = invoke_github_tool(
        request(input_value, capabilities=(CREATE_PR_CAPABILITY,)),
        pre_execute_authorize=lambda item: authorization_calls.append(item) or True,
        publication_verifier=trusted_verifier(),
        adapter=GitHubToolAdapter(client),
    )

    assert result.status is ToolResultStatus.DENIED
    assert result.failure_message == message
    assert authorization_calls == []
    assert client.pull_request_calls == []


def test_push_proof_must_resolve_approved_head_to_revision() -> None:
    records = persisted_evidence()
    records[PUSH_ID]["output"]["revision"] = "deadbee"
    client = FakeGitHubClient(pull_request_outcomes=[pull_request_response()])

    result = invoke_github_tool(
        request(create_input(), capabilities=(CREATE_PR_CAPABILITY,)),
        pre_execute_authorize=lambda _: True,
        publication_verifier=trusted_verifier(records),
        adapter=GitHubToolAdapter(client),
    )

    assert result.status is ToolResultStatus.DENIED
    assert result.failure_message == "Git push target mismatch"
    assert client.pull_request_calls == []


@pytest.mark.parametrize(
    "mutation",
    [
        lambda records: records[PUSH_ID].update(
            toolRef={"kind": "Tool", "name": "github", "version": "1.0.0"}
        ),
        lambda records: records[PUSH_ID].update(
            toolRef={"kind": "Tool", "name": "git", "version": "latest"}
        ),
        lambda records: records[PUSH_ID].update(taskExecutionId=PATCH_TASK_ID),
        lambda records: records[PUSH_ID]["provenance"].update(
            taskExecutionId=PATCH_TASK_ID
        ),
        lambda records: records[PUSH_ID]["provenance"].update(
            workflowExecutionId="workflowexecution-777777777777"
        ),
        lambda records: records[PUSH_ID]["provenance"].update(
            repositoryRevision="deadbee"
        ),
        lambda records: records[PUSH_ID]["input"].update(branch="other-head"),
        lambda records: records[PUSH_ID].update(
            policyDecisionId="policydecision-666666666666"
        ),
        lambda records: records[PUSH_POLICY_ID].update(decision="DENY"),
        lambda records: records[PUSH_POLICY_ID].update(action="filesystem.write"),
        lambda records: records[PUSH_POLICY_ID].update(
            taskExecutionId=PATCH_TASK_ID
        ),
        lambda records: records[PUSH_POLICY_ID]["resourceScope"].update(
            branch="other-head"
        ),
        lambda records: records[PUSH_POLICY_ID].update(traceId="trace-other"),
    ],
)
def test_push_proof_and_capability_decision_fail_closed(mutation) -> None:
    records = persisted_evidence()
    mutation(records)
    client = FakeGitHubClient(pull_request_outcomes=[pull_request_response()])
    authorization_calls: list[ToolRequest] = []

    result = invoke_github_tool(
        request(create_input(), capabilities=(CREATE_PR_CAPABILITY,)),
        pre_execute_authorize=lambda item: authorization_calls.append(item) or True,
        publication_verifier=trusted_verifier(records),
        adapter=GitHubToolAdapter(client),
    )

    assert result.status is ToolResultStatus.DENIED
    assert authorization_calls == []
    assert client.pull_request_calls == []


def test_missing_verifier_and_capability_denial_never_publish() -> None:
    client = FakeGitHubClient(
        pull_request_outcomes=[pull_request_response(), pull_request_response()]
    )
    missing = invoke_github_tool(
        request(create_input(), capabilities=(CREATE_PR_CAPABILITY,)),
        pre_execute_authorize=lambda _: True,
        adapter=GitHubToolAdapter(client),
    )
    denied = invoke_github_tool(
        request(create_input(), capabilities=(CREATE_PR_CAPABILITY,)),
        pre_execute_authorize=lambda _: False,
        publication_verifier=trusted_verifier(),
        adapter=GitHubToolAdapter(client),
    )

    assert missing.status is ToolResultStatus.DENIED
    assert denied.status is ToolResultStatus.DENIED
    assert client.pull_request_calls == []


def test_invalid_input_and_capability_mismatch_do_not_start_provider() -> None:
    client = FakeGitHubClient(issue_outcomes=[issue_response()])
    invalid = read_input()
    invalid["issueNumber"] = 0
    invalid_result = invoke_github_tool(
        request(invalid, capabilities=(READ_CAPABILITY,)),
        pre_execute_authorize=lambda _: True,
        adapter=GitHubToolAdapter(client),
    )
    mismatch = invoke_github_tool(
        request(read_input(), capabilities=(CREATE_PR_CAPABILITY,)),
        pre_execute_authorize=lambda _: True,
        adapter=GitHubToolAdapter(client),
    )

    assert invalid_result.failure_class is ToolFailureClass.VALIDATION
    assert mismatch.status is ToolResultStatus.DENIED
    assert client.issue_calls == []


def test_rate_limit_waits_and_preserves_full_attempt_history() -> None:
    clock = FakeClock()
    client = FakeGitHubClient(
        issue_outcomes=[
            GitHubRateLimitError(
                request_id="github-rate-1", retry_after_ms=1000
            ),
            issue_response("github-read-2"),
        ]
    )

    result = invoke_github_tool(
        request(read_input(), capabilities=(READ_CAPABILITY,), timeout_ms=5000),
        pre_execute_authorize=lambda _: True,
        adapter=GitHubToolAdapter(
            client, max_read_attempts=2, clock=clock, wait_retry=clock.wait
        ),
    )

    assert result.status is ToolResultStatus.SUCCEEDED
    assert clock.waits == [1000]
    assert [attempt["providerRequestId"] for attempt in result.output["metadata"]["attempts"]] == [
        "github-rate-1",
        "github-read-2",
    ]
    assert [attempt["classification"] for attempt in result.output["metadata"]["attempts"]] == [
        "RATE_LIMIT",
        "SUCCESS",
    ]


def test_insufficient_deadline_budget_returns_history_without_retry() -> None:
    clock = FakeClock()
    client = FakeGitHubClient(
        issue_outcomes=[
            GitHubRateLimitError(
                request_id="github-rate-1", retry_after_ms=2000
            ),
            issue_response("must-not-run"),
        ]
    )

    result = invoke_github_tool(
        request(read_input(), capabilities=(READ_CAPABILITY,), timeout_ms=1000),
        pre_execute_authorize=lambda _: True,
        adapter=GitHubToolAdapter(
            client, max_read_attempts=2, clock=clock, wait_retry=clock.wait
        ),
    )

    assert result.status is ToolResultStatus.FAILED
    assert clock.waits == []
    assert len(client.issue_calls) == 1
    assert result.output["failure"]["attempts"] == (
        {
            "attempt": 1,
            "providerRequestId": "github-rate-1",
            "classification": "RATE_LIMIT",
            "retryable": True,
            "retryAfterMs": 2000,
            "outcome": "FAILED",
        },
    )
    with pytest.raises(TypeError):
        result.output["failure"]["attempts"][0]["outcome"] = "SUCCEEDED"


def test_hanging_read_is_terminated_killed_and_cleaned_up() -> None:
    client = FakeGitHubClient(
        issue_outcomes=[PendingProviderResponse("github-timeout-read")]
    )

    result = invoke_github_tool(
        request(read_input(), capabilities=(READ_CAPABILITY,), timeout_ms=10),
        pre_execute_authorize=lambda _: True,
        adapter=GitHubToolAdapter(client),
    )

    operation = client.operations[0]
    assert result.status is ToolResultStatus.TIMED_OUT
    assert operation.wait_timeouts[0] <= 10
    assert 1 <= operation.wait_timeouts[1] <= 100
    assert operation.terminated is True
    assert operation.killed is True
    assert operation.cleaned_up is True
    assert len(client.issue_calls) == 1
    assert result.failure_class is ToolFailureClass.TIMEOUT
    assert result.output["failure"]["ambiguousPublication"] is False
    assert result.output["failure"]["retryable"] is True
    assert result.output["failure"]["providerRequestId"] == "github-timeout-read"
    assert result.output["failure"]["traceId"] == TRACE_ID
    assert result.output["failure"]["attempts"][0]["classification"] == "TIMEOUT"


def test_ambiguous_hanging_publication_is_never_replayed() -> None:
    client = FakeGitHubClient(
        pull_request_outcomes=[
            PendingProviderResponse("github-timeout-create"),
            pull_request_response(),
        ]
    )

    result = invoke_github_tool(
        request(create_input(), capabilities=(CREATE_PR_CAPABILITY,), timeout_ms=10),
        pre_execute_authorize=lambda _: True,
        publication_verifier=trusted_verifier(),
        adapter=GitHubToolAdapter(client),
    )

    assert result.status is ToolResultStatus.TIMED_OUT
    assert len(client.pull_request_calls) == 1
    assert client.operations[0].terminated is True
    assert client.operations[0].killed is True
    assert client.operations[0].cleaned_up is True
    assert result.output["failure"]["ambiguousPublication"] is True
    assert result.output["failure"]["retryable"] is False
    assert result.output["failure"]["providerRequestId"] == "github-timeout-create"
    with pytest.raises(TypeError):
        result.output["failure"]["attempts"][0]["outcome"] = "SUCCEEDED"


def test_provider_failure_is_stable_and_not_retried_for_publication() -> None:
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
        publication_verifier=trusted_verifier(),
        adapter=GitHubToolAdapter(client),
    )

    assert result.status is ToolResultStatus.FAILED
    assert result.output["failure"]["retryable"] is True
    assert result.output["failure"]["providerRequestId"] == "github-create-failed"
    assert len(client.pull_request_calls) == 1


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


def _runtime_registry() -> Registry:
    registry = Registry()
    for root in (ROOT / "schemas/resources/v1", ROOT / "schemas/runtime/v1"):
        for path in root.glob("*.schema.json"):
            schema = json.loads(path.read_text(encoding="utf-8"))
            registry = registry.with_resource(
                schema["$id"],
                SchemaResource.from_contents(
                    schema, default_specification=DRAFT202012
                ),
            )
    return registry
