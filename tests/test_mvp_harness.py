from pathlib import Path

from aep.mvp_harness import PR_URL, run_mvp_harness
from aep.publication_policy import PUBLICATION_EVIDENCE_FIELDS


ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "fixtures" / "e2e-mvp" / "repository"
EXPECTED_TASKS = (
    "analyze-issue",
    "build-implementation-plan",
    "generate-patch",
    "run-validation",
    "evaluate-acceptance",
    "create-pull-request",
)


def test_json_task_fixtures_are_utf8_without_a_byte_order_mark() -> None:
    for name in ("analyze-issue", "build-implementation-plan", "generate-patch"):
        payload = (FIXTURE / ".ai" / "tasks" / f"{name}.yaml").read_bytes()
        assert not payload.startswith(b"\xef\xbb\xbf")


def test_fixture_issue_runs_the_complete_mvp_dag_deterministically() -> None:
    result = run_mvp_harness(FIXTURE)

    assert result.resources.workspace.name == "fixture"
    assert result.normalized_event["type"] == "github.issue.created"
    assert result.duplicate_was_rejected is True
    assert result.task_names == EXPECTED_TASKS
    assert result.workflow_execution["status"] == "SUCCEEDED"
    assert result.model_request_count == 3
    assert result.github_request_count == 1
    assert result.git_operations[-2:] == ("commit_changes", "push_branch")
    assert result.pull_request_url == PR_URL
    assert [item["artifactType"] for item in result.generated_artifacts] == [
        "ISSUE_ANALYSIS",
        "IMPLEMENTATION_PLAN",
        "PATCH",
        "EVALUATION_REPORT",
        "PULL_REQUEST_DESCRIPTION",
    ]
    assert len(result.evaluation_results) == 6
    assert {item["outcome"] for item in result.evaluation_results} == {"PASS"}
    assert [item["decision"] for item in result.policy_decisions] == [
        "ALLOW",
        "ALLOW",
        "ALLOW",
    ]
    publication = next(
        item for item in result.policy_decisions if item["gate"] == "PUBLICATION"
    )
    assert tuple(publication["evidence"]) == PUBLICATION_EVIDENCE_FIELDS
    assert publication["evidence"] == {
        "patchGenerated": True,
        "validationRan": True,
        "requiredArtifactsPresent": True,
        "requiredEvaluationsPresent": True,
        "allRequiredEvaluationsPassed": True,
        "noPriorPolicyViolation": True,
        "failures": [],
    }
    assert {item["traceId"] for item in result.runtime_history} == {
        result.workflow_execution["traceId"]
    }
    assert {
        "ContextPackage",
        "ResolvedAgent",
        "AgentInvocation",
        "ModelInvocation",
        "ToolInvocation",
        "EvaluationResult",
        "PolicyDecision",
        "GeneratedArtifact",
        "ExecutionEvent",
        "TaskExecution",
    }.issubset({item["kind"] for item in result.runtime_history})


def test_publication_denial_persists_evidence_and_never_calls_github() -> None:
    result = run_mvp_harness(FIXTURE, block_publication=True)

    assert result.task_names == EXPECTED_TASKS
    assert result.workflow_execution["status"] == "FAILED"
    assert [item["decision"] for item in result.policy_decisions] == ["DENY"]
    assert result.github_request_count == 0
    assert "push_branch" not in result.git_operations
    assert result.pull_request_url is None
    assert "PULL_REQUEST_DESCRIPTION" not in {
        item["artifactType"] for item in result.generated_artifacts
    }
