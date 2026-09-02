from hashlib import sha256

import pytest

from aep.planning_evidence import (
    PlanningEvidenceError,
    evaluate_path_predicates,
    reconcile_dispositions,
    validate_plan_path_contract,
)


REVISION = "679a0c6f4eb04483aa917faae018a3037d3e82f9"


def evidence(path: str, status: str, expected: str = "In Progress") -> dict:
    return evaluate_path_predicates(
        path=path, content=f"# Task\n\n**Status:** {status}\n\nManual testing and MTP verification.\n",
        repository_revision=REVISION,
        predicates=[{"kind": "STATUS_EQUALS", "value": expected}], source_id="snapshot-issue-78",
    )


def test_issue_78_regression_selects_only_genuinely_pending_tasks() -> None:
    completed = [
        "AEP-005-normalize-github-issue-created-event.md",
        "AEP-021-implement-filesystem-tool.md",
        "AEP-029-implement-analyzeissue-task-handler.md",
        "AEP-033-implement-evaluateacceptance-task-handler.md",
        "AEP-044-stabilize-self-hosting-dogfood-startup.md",
    ]
    pending = ["AEP-043-complete-live-pilot.md", "AEP-047-verify-images.md"]
    records = [evidence(f"docs/tasks/{name}", "Completed") for name in completed]
    records += [evidence(f"docs/tasks/{name}", "In Progress") for name in pending]

    required = [item["path"] for item in records if item["predicateResults"][0]["result"] == "MATCH"]

    assert required == [f"docs/tasks/{name}" for name in pending]
    assert not set(required) & {f"docs/tasks/{name}" for name in completed}
    assert all(item["repositoryRevision"] == REVISION and item["preimageSha256"] for item in records)
    assert all("content" not in item for item in records)


def test_predicates_cover_text_absence_and_unsupported_semantics_deterministically() -> None:
    first = evaluate_path_predicates(
        path="docs/execution-plan.md", content="required text", repository_revision=REVISION,
        predicates=[{"kind": "TEXT_PRESENT", "value": "required"}, {"kind": "TEXT_ABSENT", "value": "secret"}, {"kind": "SEMANTIC", "value": "looks correct"}],
        source_id="snapshot",
    )
    second = evaluate_path_predicates(
        path="docs/execution-plan.md", content="required text", repository_revision=REVISION,
        predicates=[{"kind": "TEXT_PRESENT", "value": "required"}, {"kind": "TEXT_ABSENT", "value": "secret"}, {"kind": "SEMANTIC", "value": "looks correct"}],
        source_id="snapshot",
    )
    assert [item["result"] for item in first["predicateResults"]] == ["MATCH", "MATCH", "UNSUPPORTED"]
    assert first["selectionId"] == second["selectionId"]


@pytest.mark.parametrize("content", ["**Status:** In Progress\n**Status:** Completed\n", "no status"])
def test_ambiguous_or_missing_structured_field_fails_closed(content: str) -> None:
    with pytest.raises(PlanningEvidenceError, match="ambiguous status"):
        evaluate_path_predicates(path="docs/task.md", content=content, repository_revision=REVISION,
            predicates=[{"kind": "STATUS_EQUALS", "value": "In Progress"}], source_id="snapshot")


def test_plan_contract_rejects_overlap_omission_and_stale_evidence() -> None:
    item = evidence("docs/task.md", "In Progress")
    plan = {"authorizedPaths": ["docs/task.md"], "requiredChangePaths": ["docs/task.md"],
        "verifiedNoChangePaths": [], "unsupportedPaths": [], "pathEvidence": [item]}
    validate_plan_path_contract(plan, REVISION)
    for mutation, message in [
        ({"verifiedNoChangePaths": ["docs/task.md"]}, "conflict"),
        ({"requiredChangePaths": []}, "exactly one"),
        ({"pathEvidence": [{**item, "repositoryRevision": "stale"}]}, "revision-mismatched"),
    ]:
        invalid = {**plan, **mutation}
        with pytest.raises(PlanningEvidenceError, match=message):
            validate_plan_path_contract(invalid, REVISION)


def test_reconciliation_accepts_proven_no_change_but_not_bare_assertion() -> None:
    content = "**Status:** Completed\n"
    target = {"path": "docs/task.md", "content": content, "preimageSha256": sha256(content.encode()).hexdigest(),
        "repositoryRevision": REVISION, "provenance": {"taskExecutionId": "taskexecution-1"}}
    criteria = {"docs/task.md": ({"kind": "STATUS_EQUALS", "value": "Completed"},)}
    result = reconcile_dispositions(plan_id="artifact-1", repository_revision=REVISION,
        original_required_paths=["docs/task.md"], targets=[target],
        dispositions=[{"path": "docs/task.md", "disposition": "NO_CHANGE"}],
        criteria_by_path=criteria, evaluator_ref={"kind": "Evaluation", "name": "reconcile", "version": "1.0.0"})
    assert result["originalRequiredPaths"] == ["docs/task.md"]
    assert result["effectiveRequiredPaths"] == []
    assert result["verifiedNoChangePaths"] == ["docs/task.md"]
    with pytest.raises(PlanningEvidenceError, match="no predicates"):
        reconcile_dispositions(plan_id="artifact-1", repository_revision=REVISION,
            original_required_paths=["docs/task.md"], targets=[target],
            dispositions=[{"path": "docs/task.md", "disposition": "NO_CHANGE"}], criteria_by_path={},
            evaluator_ref={"kind": "Evaluation", "name": "reconcile", "version": "1.0.0"})


def test_reconciliation_keeps_required_change_and_rejects_stale_target() -> None:
    content = "**Status:** In Progress\n"
    target = {"path": "docs/task.md", "content": content, "preimageSha256": sha256(content.encode()).hexdigest(),
        "repositoryRevision": REVISION, "provenance": {}}
    result = reconcile_dispositions(plan_id="artifact-1", repository_revision=REVISION,
        original_required_paths=["docs/task.md"], targets=[target],
        dispositions=[{"path": "docs/task.md", "disposition": "CHANGE"}], criteria_by_path={},
        evaluator_ref={"kind": "Evaluation", "name": "reconcile", "version": "1.0.0"})
    assert result["effectiveRequiredPaths"] == ["docs/task.md"]
    with pytest.raises(PlanningEvidenceError, match="stale content"):
        reconcile_dispositions(plan_id="artifact-1", repository_revision=REVISION,
            original_required_paths=["docs/task.md"], targets=[{**target, "preimageSha256": "0" * 64}],
            dispositions=[{"path": "docs/task.md", "disposition": "CHANGE"}], criteria_by_path={},
            evaluator_ref={"kind": "Evaluation", "name": "reconcile", "version": "1.0.0"})
