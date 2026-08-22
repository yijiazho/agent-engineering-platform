from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from aep.capability_policy import ApplicablePolicy, PolicyScope
from aep.publication_policy import (
    PUBLICATION_EVIDENCE_BOOLEAN_FIELDS,
    PUBLICATION_EVIDENCE_FIELDS,
    PublicationPolicy,
    PublicationPolicyContractError,
    PublicationPolicyIdentityConflictError,
)
from aep.resource_loader import ResourceLoader, ResourceRef
from aep.runtime_store import InMemoryRuntimeObjectStore


TASK_ID = "taskexecution-aaaaaaaaaaaa"
WORKFLOW_ID = "workflowexecution-bbbbbbbbbbbb"
TRACE_ID = "trace-publication-1"
REVISION = "1" * 40
ARTIFACT_ID = "generatedartifact-cccccccccccc"
EVALUATION_ID = "evaluationresult-dddddddddddd"
ROOT = Path(__file__).parents[1]


def repository_policy() -> dict:
    resources = ResourceLoader(ROOT).load()
    loaded = resources.get(ResourceRef("Policy", "publication-evidence", "1.1.0"))
    assert loaded is not None
    return loaded.data


def policy(effect: str = "allow", *, reason: str = "Evidence permits publication", conditions=None) -> dict:
    rule = {"effect": effect, "reason": reason}
    if conditions is not None:
        rule["conditions"] = conditions
    return {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "Policy",
        "metadata": {"name": f"publication-{effect}", "version": "1.0.0"},
        "spec": {"type": "publication", "rules": [rule]},
    }


def artifact() -> dict:
    return {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "GeneratedArtifact",
        "id": ARTIFACT_ID,
        "traceId": TRACE_ID,
        "createdAt": "2026-08-06T11:00:00Z",
        "updatedAt": "2026-08-06T11:00:00Z",
        "taskExecutionId": "taskexecution-aaaa11111111",
        "artifactType": "PATCH",
        "contentAddress": f"sha256:{'2' * 64}",
        "repositoryRevision": REVISION,
        "provenance": {
            "actor": "artifact-store",
            "workflowExecutionId": WORKFLOW_ID,
            "taskExecutionId": "taskexecution-aaaa11111111",
            "repositoryRevision": REVISION,
            "resourceRefs": [],
        },
    }


def evaluation(*, outcome: str = "PASS", status: str = "SUCCEEDED") -> dict:
    return {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "EvaluationResult",
        "id": EVALUATION_ID,
        "traceId": TRACE_ID,
        "createdAt": "2026-08-06T11:01:00Z",
        "updatedAt": "2026-08-06T11:01:00Z",
        "taskExecutionId": "taskexecution-bbbb11111111",
        "evaluationRef": {"kind": "Evaluation", "name": "tests", "version": "1.0.0"},
        "target": {"type": "GeneratedArtifact", "id": ARTIFACT_ID},
        "status": status,
        "outcome": outcome,
        "provenance": {
            "actor": "evaluation-engine",
            "workflowExecutionId": WORKFLOW_ID,
            "taskExecutionId": "taskexecution-bbbb11111111",
            "repositoryRevision": REVISION,
            "resourceRefs": [],
        },
    }


def evaluate(*, store=None, artifacts=None, evaluations=None, required_artifacts=None,
             required_evaluations=None, prior=None, policies=None,
             decision_id="policydecision-eeeeeeeeeeee", action="github.create_pr"):
    runtime_store = store or InMemoryRuntimeObjectStore()
    artifact_values = [artifact()] if artifacts is None else artifacts
    evaluation_values = [evaluation()] if evaluations is None else evaluations
    prior_values = [] if prior is None else prior
    for value in [*artifact_values, *evaluation_values, *prior_values]:
        if runtime_store.get(value["id"]) is None:
            runtime_store.create(value, deterministic_key=f"test-evidence:{value['id']}")
    return PublicationPolicy(runtime_store).evaluate(
        decision_id=decision_id,
        task_execution_id=TASK_ID,
        candidate_action={
            "action": action,
            "target": {
                "repository": "acme/widgets",
                "head": "agent/change",
                "base": "main",
                "repositoryRevision": REVISION,
                "pushToolInvocationId": "toolinvocation-ffffffffffff",
            },
        },
        required_artifact_ids=[ARTIFACT_ID] if required_artifacts is None else required_artifacts,
        artifacts=artifact_values,
        required_evaluation_ids=[EVALUATION_ID] if required_evaluations is None else required_evaluations,
        evaluation_results=evaluation_values,
        prior_policy_decisions=prior_values,
        applicable_policies=[ApplicablePolicy(PolicyScope.WORKSPACE, policy())] if policies is None else policies,
        actor=f"TaskExecution:{TASK_ID}",
        resource_scope={"repository": "acme/widgets"},
        correlation={"traceId": TRACE_ID, "workflowExecutionId": WORKFLOW_ID, "taskExecutionId": TASK_ID},
        timestamp="2026-08-06T12:00:00Z",
    )


def test_passing_evidence_allows_and_persists_complete_decision() -> None:
    store = InMemoryRuntimeObjectStore()
    result = evaluate(store=store)

    assert result["decision"] == "ALLOW"
    assert result["reason"] == "Evidence permits publication"
    assert result["evaluatedRule"]["ruleIndex"] == 0
    assert result["generatedArtifactIds"] == [ARTIFACT_ID]
    assert result["evaluationResultIds"] == [EVALUATION_ID]
    assert result["evidence"]["patchGenerated"] is True
    assert store.get(result["id"]) == result


@pytest.mark.parametrize(
    "changes, reason",
    [
        ({"artifacts": []}, "Required artifacts are missing"),
        ({"artifacts": [], "required_artifacts": []}, "Patch generation did not produce"),
        ({"evaluations": []}, "Required EvaluationResults are missing"),
        ({"evaluations": [], "required_evaluations": []}, "Validation did not run"),
        ({"evaluations": [evaluation(outcome="FAIL")]}, "failed"),
        ({"evaluations": [evaluation(status="RUNNING", outcome="PENDING")]}, "did not complete"),
    ],
)
def test_incomplete_or_failed_evidence_denies(changes: dict, reason: str) -> None:
    result = evaluate(**changes)

    assert result["decision"] == "DENY"
    assert reason in result["reason"]
    assert result["evaluatedRule"] is None


def test_prior_denial_overrides_an_allow_rule() -> None:
    prior = {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "PolicyDecision",
        "id": "policydecision-a11111111111",
        "traceId": TRACE_ID,
        "createdAt": "2026-08-06T10:00:00Z",
        "updatedAt": "2026-08-06T10:00:00Z",
        "provenance": {"actor": "policy-engine", "resourceRefs": []},
        "taskExecutionId": TASK_ID,
        "gate": "PUBLICATION",
        "policyRefs": [],
        "action": "github.create_pr",
        "decision": "DENY",
        "reason": "An earlier gate denied the action.",
    }

    result = evaluate(prior=[prior])

    assert result["decision"] == "DENY"
    assert "prior" in result["reason"].lower()
    assert result["priorPolicyDecisionIds"] == [prior["id"]]


def test_require_approval_rule_is_returned_and_explained() -> None:
    result = evaluate(
        policies=[ApplicablePolicy(PolicyScope.TASK, policy("require-approval", reason="Owner approval required"))]
    )

    assert result["decision"] == "REQUIRE_APPROVAL"
    assert result["approvalRequired"] is True
    assert result["reason"] == "Owner approval required"


def test_prior_require_approval_remains_approval_required() -> None:
    store = InMemoryRuntimeObjectStore()
    prior = evaluate(
        store=store,
        decision_id="policydecision-a22222222222",
        policies=[ApplicablePolicy(PolicyScope.TASK, policy("require-approval"))],
    )

    result = evaluate(
        store=store,
        decision_id="policydecision-a33333333333",
        prior=[prior],
        policies=[ApplicablePolicy(PolicyScope.TASK, repository_policy())],
    )

    assert result["decision"] == "REQUIRE_APPROVAL"
    assert result["evaluatedRule"] is None
    assert result["priorPolicyDecisionIds"] == [prior["id"]]


def test_most_restrictive_matching_publication_rule_wins() -> None:
    result = evaluate(
        policies=[
            ApplicablePolicy(PolicyScope.PLATFORM, policy("allow")),
            ApplicablePolicy(PolicyScope.WORKFLOW, policy("require-approval")),
            ApplicablePolicy(PolicyScope.TASK, policy("deny", reason="Task blocks publication")),
        ]
    )

    assert result["decision"] == "DENY"
    assert result["reason"] == "Task blocks publication"
    assert [item["scope"] for item in result["matchedRules"]] == ["Platform", "Workflow", "Task"]


def test_rule_conditions_can_match_evidence_summary() -> None:
    matching = policy(
        conditions={
            "properties": {
                "evidence": {
                    "properties": {"allRequiredEvaluationsPassed": {"const": True}},
                    "required": ["allRequiredEvaluationsPassed"],
                }
            },
            "required": ["evidence"],
        }
    )

    assert evaluate(policies=[ApplicablePolicy(PolicyScope.WORKSPACE, matching)])["decision"] == "ALLOW"


def test_repository_policy_matches_the_canonical_runtime_evidence() -> None:
    result = evaluate(
        policies=[ApplicablePolicy(PolicyScope.TASK, repository_policy())]
    )

    assert result["decision"] == "ALLOW"
    assert tuple(result["evidence"]) == PUBLICATION_EVIDENCE_FIELDS
    assert result["policyRefs"] == [
        {"kind": "Policy", "name": "publication-evidence", "version": "1.1.0"}
    ]
    assert result["matchedRules"] == [
        {
            "scope": "Task",
            "policyRef": result["policyRefs"][0],
            "ruleIndex": 0,
            "effect": "allow",
        }
    ]
    assert result["evaluatedRule"] == result["matchedRules"][0]


@pytest.mark.parametrize("field", PUBLICATION_EVIDENCE_BOOLEAN_FIELDS)
@pytest.mark.parametrize("change", ["false", "missing"])
def test_repository_rule_rejects_each_false_or_missing_canonical_field(
    field: str, change: str
) -> None:
    conditions = repository_policy()["spec"]["rules"][0]["conditions"]
    evidence = {
        name: True for name in PUBLICATION_EVIDENCE_BOOLEAN_FIELDS
    } | {"failures": []}
    if change == "false":
        evidence[field] = False
    else:
        evidence.pop(field)

    assert not Draft202012Validator(conditions).is_valid(
        {
            "candidateAction": {"action": "github.create_pr"},
            "evidence": evidence,
        }
    )


def test_repository_rule_rejects_failures_unknown_action_and_vocabulary_drift() -> None:
    conditions = repository_policy()["spec"]["rules"][0]["conditions"]
    evidence = {
        name: True for name in PUBLICATION_EVIDENCE_BOOLEAN_FIELDS
    } | {"failures": ["unsafe"]}
    assert not Draft202012Validator(conditions).is_valid(
        {"candidateAction": {"action": "git.push"}, "evidence": evidence}
    )
    evidence["failures"] = []
    evidence["renamedField"] = True
    assert not Draft202012Validator(conditions).is_valid(
        {"candidateAction": {"action": "github.create_pr"}, "evidence": evidence}
    )


def test_repository_policy_denies_unknown_action_in_production_path() -> None:
    result = evaluate(
        action="git.push",
        policies=[ApplicablePolicy(PolicyScope.TASK, repository_policy())],
    )

    assert result["decision"] == "DENY"
    assert result["matchedRules"] == []
    assert result["evaluatedRule"] is None


def test_malformed_publication_conditions_fail_closed_as_configuration_error() -> None:
    malformed = policy(conditions={"type": "not-a-json-schema-type"})
    with pytest.raises(PublicationPolicyContractError, match="invalid conditions"):
        evaluate(policies=[ApplicablePolicy(PolicyScope.TASK, malformed)])


def test_no_matching_rule_denies_by_default() -> None:
    result = evaluate(policies=[])

    assert result["decision"] == "DENY"
    assert result["matchedRules"] == []


def test_identical_retry_is_idempotent_but_changed_inputs_conflict() -> None:
    store = InMemoryRuntimeObjectStore()
    first = evaluate(store=store)
    assert evaluate(store=store) == first

    with pytest.raises(PublicationPolicyIdentityConflictError):
        evaluate(store=store, evaluations=[evaluation(outcome="FAIL")])


def test_inputs_and_persisted_evidence_do_not_alias_callers() -> None:
    patch = artifact()
    result = evaluate(artifacts=[patch])
    patch["artifactType"] = "REVIEW_REPORT"
    result["evidence"]["patchGenerated"] = False

    assert result["decision"] == "ALLOW"


def test_schema_invalid_runtime_evidence_denies() -> None:
    value = artifact()
    value.pop("contentAddress")

    result = evaluate(artifacts=[value])

    assert result["decision"] == "DENY"
    assert "is invalid" in result["reason"]


def test_unpersisted_or_spoofed_evidence_denies() -> None:
    store = InMemoryRuntimeObjectStore()
    persisted = artifact()
    store.create(persisted, deterministic_key="persisted-artifact")
    spoof = deepcopy(persisted)
    spoof["artifactType"] = "REVIEW_REPORT"
    store.create(evaluation(), deterministic_key="persisted-evaluation")

    result = PublicationPolicy(store).evaluate(
        decision_id="policydecision-abcde1111111",
        task_execution_id=TASK_ID,
        candidate_action={"action": "github.create_pr", "target": {"repositoryRevision": REVISION}},
        required_artifact_ids=[ARTIFACT_ID],
        artifacts=[spoof],
        required_evaluation_ids=[EVALUATION_ID],
        evaluation_results=[evaluation()],
        prior_policy_decisions=[],
        applicable_policies=[ApplicablePolicy(PolicyScope.WORKSPACE, policy())],
        actor=f"TaskExecution:{TASK_ID}",
        resource_scope={"repository": "acme/widgets"},
        correlation={"traceId": TRACE_ID, "workflowExecutionId": WORKFLOW_ID, "taskExecutionId": TASK_ID},
        timestamp="2026-08-06T12:00:00Z",
    )

    assert result["decision"] == "DENY"
    assert "does not match persisted evidence" in result["reason"]


def test_non_publication_policy_is_rejected() -> None:
    value = policy()
    value["spec"]["type"] = "pre-execution-capability"

    with pytest.raises(PublicationPolicyContractError, match="type publication"):
        evaluate(policies=[ApplicablePolicy(PolicyScope.TASK, value)])


def test_mismatched_revision_provenance_denies() -> None:
    value = artifact()
    value["repositoryRevision"] = "2" * 40

    result = evaluate(artifacts=[value])

    assert result["decision"] == "DENY"
    assert "mismatched provenance" in result["reason"]


def test_cross_workflow_provenance_denies() -> None:
    value = artifact()
    value["provenance"]["workflowExecutionId"] = "workflowexecution-cccccccccccc"

    result = evaluate(artifacts=[value])

    assert result["decision"] == "DENY"
    assert "mismatched provenance" in result["reason"]
