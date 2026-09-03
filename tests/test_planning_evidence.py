from hashlib import sha256

import pytest

from aep.planning_evidence import (
    PlanningEvidenceError,
    evaluate_path_predicates,
    finalize_planning_evidence,
    reconcile_dispositions,
    validate_plan_path_contract,
)


REVISION = "679a0c6f4eb04483aa917faae018a3037d3e82f9"


def evidence(path: str, status: str, expected: str = "In Progress") -> dict:
    content = f"# Task\n\n**Status:** {status}\n\nManual testing and MTP verification.\n"
    record = evaluate_path_predicates(
        path=path, content=content,
        repository_revision=REVISION,
        predicates=[{"kind": "STATUS_EQUALS", "value": expected}], source_id="snapshot-issue-78",
    )
    post = evaluate_path_predicates(
        path=path, content=content, repository_revision=REVISION,
        predicates=[{"kind": "STATUS_EQUALS", "value": "Completed"}],
        source_id="snapshot-issue-78",
    )
    return finalize_planning_evidence(
        record, postconditions=[{"kind": "STATUS_EQUALS", "value": "Completed"}],
        postcondition_results=post["predicateResults"], selection_reasons=["test"],
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


@pytest.mark.parametrize(("content", "reason"), [
    ("**Status:** In Progress\n**Status:** Completed\n", "STATUS_FIELD_AMBIGUOUS"),
    ("no status", "STATUS_FIELD_MISSING"),
])
def test_ambiguous_or_missing_structured_field_fails_closed(
    content: str, reason: str
) -> None:
    with pytest.raises(PlanningEvidenceError, match=reason):
        evaluate_path_predicates(path="docs/task.md", content=content, repository_revision=REVISION,
            predicates=[{"kind": "STATUS_EQUALS", "value": "In Progress"}], source_id="snapshot")


def test_plan_contract_rejects_overlap_omission_and_stale_evidence() -> None:
    item = evidence("docs/task.md", "In Progress")
    plan = {"authorizedPaths": ["docs/task.md"], "requiredChangePaths": ["docs/task.md"],
        "verifiedNoChangePaths": [], "unsupportedPaths": [], "pathEvidence": [item]}
    validate_plan_path_contract(plan, REVISION, trusted_path_evidence=[item])
    for mutation, message in [
        ({"verifiedNoChangePaths": ["docs/task.md"]}, "conflict"),
        ({"requiredChangePaths": []}, "exactly one"),
        ({"pathEvidence": [{**item, "repositoryRevision": "stale"}]}, "revision-mismatched"),
    ]:
        invalid = {**plan, **mutation}
        with pytest.raises(PlanningEvidenceError, match=message):
            validate_plan_path_contract(invalid, REVISION, trusted_path_evidence=[item])


def test_plan_contract_rejects_model_fabricated_evidence() -> None:
    trusted = evidence("docs/task.md", "In Progress")
    fabricated = {**trusted, "preimageSha256": "f" * 64}
    plan = {"authorizedPaths": ["docs/task.md"], "requiredChangePaths": ["docs/task.md"],
        "verifiedNoChangePaths": [], "unsupportedPaths": [], "pathEvidence": [fabricated]}
    with pytest.raises(PlanningEvidenceError, match="trusted Context Builder evidence"):
        validate_plan_path_contract(plan, REVISION, trusted_path_evidence=[trusted])


def test_plan_dispositions_require_the_expected_predicate_outcome() -> None:
    completed = evidence("docs/task.md", "Completed")
    false_required = {"authorizedPaths": ["docs/task.md"], "requiredChangePaths": ["docs/task.md"],
        "verifiedNoChangePaths": [], "unsupportedPaths": [], "pathEvidence": [completed]}
    with pytest.raises(PlanningEvidenceError, match="does not satisfy"):
        validate_plan_path_contract(false_required, REVISION, trusted_path_evidence=[completed])
    verified_no_change = {**false_required, "requiredChangePaths": [],
        "verifiedNoChangePaths": ["docs/task.md"]}
    validate_plan_path_contract(verified_no_change, REVISION, trusted_path_evidence=[completed])


def test_plan_predicates_use_conjunction_and_mixed_results_mean_no_change() -> None:
    content = "**Status:** Completed\nmanual testing\n"
    item = evaluate_path_predicates(path="docs/task.md", content=content,
        repository_revision=REVISION, predicates=[
            {"kind": "STATUS_EQUALS", "value": "In Progress"},
            {"kind": "TEXT_PRESENT", "value": "manual testing"},
        ], source_id="snapshot")
    post = evaluate_path_predicates(path="docs/task.md", content=content,
        repository_revision=REVISION, predicates=[
            {"kind": "STATUS_EQUALS", "value": "Completed"},
            {"kind": "TEXT_PRESENT", "value": "manual testing"},
        ], source_id="snapshot")
    item = finalize_planning_evidence(item,
        postconditions=[{"kind": "STATUS_EQUALS", "value": "Completed"},
            {"kind": "TEXT_PRESENT", "value": "manual testing"}],
        postcondition_results=post["predicateResults"], selection_reasons=["test"])
    plan = {"authorizedPaths": ["docs/task.md"], "requiredChangePaths": [],
        "verifiedNoChangePaths": ["docs/task.md"], "unsupportedPaths": [], "pathEvidence": [item]}
    validate_plan_path_contract(plan, REVISION, trusted_path_evidence=[item])


def test_reconciliation_accepts_proven_no_change_but_not_bare_assertion() -> None:
    content = "**Status:** Completed\n"
    target = {"path": "docs/task.md", "content": content, "preimageSha256": sha256(content.encode()).hexdigest(),
        "repositoryRevision": REVISION, "provenance": {"taskExecutionId": "taskexecution-1"}}
    criteria = {"docs/task.md": ({"kind": "STATUS_EQUALS", "value": "Completed"},)}
    result = reconcile_dispositions(plan_id="artifact-1", repository_revision=REVISION,
        original_required_paths=["docs/task.md"], targets=[target],
        dispositions=[{"path": "docs/task.md", "disposition": "NO_CHANGE"}],
        postconditions_by_path=criteria, evaluator_ref={"kind": "Evaluation", "name": "reconcile", "version": "1.0.0"})
    assert result["originalRequiredPaths"] == ["docs/task.md"]
    assert result["effectiveRequiredPaths"] == []
    assert result["verifiedNoChangePaths"] == ["docs/task.md"]
    assert result["pathDispositions"][0]["postconditionProof"]["predicateResults"][0]["result"] == "MATCH"
    with pytest.raises(PlanningEvidenceError, match="no predicates"):
        reconcile_dispositions(plan_id="artifact-1", repository_revision=REVISION,
            original_required_paths=["docs/task.md"], targets=[target],
            dispositions=[{"path": "docs/task.md", "disposition": "NO_CHANGE"}], postconditions_by_path={},
            evaluator_ref={"kind": "Evaluation", "name": "reconcile", "version": "1.0.0"})


def test_reconciliation_keeps_required_change_and_rejects_stale_target() -> None:
    content = "**Status:** In Progress\n"
    target = {"path": "docs/task.md", "content": content, "preimageSha256": sha256(content.encode()).hexdigest(),
        "repositoryRevision": REVISION, "provenance": {}}
    result = reconcile_dispositions(plan_id="artifact-1", repository_revision=REVISION,
        original_required_paths=["docs/task.md"], targets=[target],
        dispositions=[{"path": "docs/task.md", "disposition": "CHANGE"}],
        postconditions_by_path={"docs/task.md": ({"kind": "STATUS_EQUALS", "value": "Completed"},)},
        proposed_contents_by_path={"docs/task.md": "**Status:** Completed\n"},
        evaluator_ref={"kind": "Evaluation", "name": "reconcile", "version": "1.0.0"})
    assert result["effectiveRequiredPaths"] == ["docs/task.md"]
    with pytest.raises(PlanningEvidenceError, match="stale content"):
        reconcile_dispositions(plan_id="artifact-1", repository_revision=REVISION,
            original_required_paths=["docs/task.md"], targets=[{**target, "preimageSha256": "0" * 64}],
            dispositions=[{"path": "docs/task.md", "disposition": "CHANGE"}], postconditions_by_path={},
            proposed_contents_by_path={"docs/task.md": "**Status:** Completed\n"},
            evaluator_ref={"kind": "Evaluation", "name": "reconcile", "version": "1.0.0"})


def test_no_change_requires_exact_required_insertions_to_be_present() -> None:
    content = "existing text\n"
    target = {"path": "docs/task.md", "content": content,
        "preimageSha256": sha256(content.encode()).hexdigest(),
        "repositoryRevision": REVISION, "provenance": {}}
    common = dict(plan_id="artifact-1", repository_revision=REVISION,
        original_required_paths=["docs/task.md"], targets=[target],
        dispositions=[{"path": "docs/task.md", "disposition": "NO_CHANGE"}],
        postconditions_by_path={"docs/task.md": ({"kind": "TEXT_PRESENT", "value": "existing text"},)},
        evaluator_ref={"kind": "Evaluation", "name": "reconcile", "version": "1.0.0"})
    with pytest.raises(PlanningEvidenceError, match="lacks a required insertion"):
        reconcile_dispositions(**common,
            required_insertions_by_path={"docs/task.md": ("new required text",)})
    result = reconcile_dispositions(**common,
        required_insertions_by_path={"docs/task.md": ("existing text",)})
    assert result["pathDispositions"][0]["requiredInsertionProof"] == [
        {"value": "existing text", "result": "MATCH"}
    ]


def test_reconciliation_proves_an_authorized_deleted_post_state() -> None:
    content = "obsolete text\n"
    target = {"path": "docs/task.md", "content": content,
        "preimageSha256": sha256(content.encode()).hexdigest(),
        "repositoryRevision": REVISION, "provenance": {}}
    result = reconcile_dispositions(plan_id="artifact-1", repository_revision=REVISION,
        original_required_paths=["docs/task.md"], targets=[target],
        dispositions=[{"path": "docs/task.md", "disposition": "CHANGE"}],
        postconditions_by_path={"docs/task.md": ({"kind": "TEXT_ABSENT", "value": "obsolete text"},)},
        deleted_paths=["docs/task.md"],
        evaluator_ref={"kind": "Evaluation", "name": "reconcile", "version": "1.0.0"})
    disposition = result["pathDispositions"][0]
    assert disposition["postState"] == "ABSENT"
    assert disposition["outputSha256"] is None
    assert disposition["postconditionProof"]["predicateResults"][0]["result"] == "MATCH"


def test_reconciliation_targets_exactly_cover_original_required_paths() -> None:
    content = "**Status:** In Progress\n"
    target = {"path": "docs/task.md", "content": content, "preimageSha256": sha256(content.encode()).hexdigest(),
        "repositoryRevision": REVISION, "provenance": {}}
    with pytest.raises(PlanningEvidenceError, match="exactly cover"):
        reconcile_dispositions(plan_id="artifact-1", repository_revision=REVISION,
            original_required_paths=["docs/task.md", "docs/other.md"], targets=[target],
            dispositions=[{"path": "docs/task.md", "disposition": "CHANGE"}], postconditions_by_path={},
            evaluator_ref={"kind": "Evaluation", "name": "reconcile", "version": "1.0.0"})
    with pytest.raises(PlanningEvidenceError, match="exactly cover"):
        reconcile_dispositions(plan_id="artifact-1", repository_revision=REVISION,
            original_required_paths=[], targets=[target],
            dispositions=[{"path": "docs/task.md", "disposition": "CHANGE"}], postconditions_by_path={},
            evaluator_ref={"kind": "Evaluation", "name": "reconcile", "version": "1.0.0"})


def test_no_change_rejects_satisfied_precondition_when_postcondition_is_missing() -> None:
    content = "**Status:** In Progress\n"
    target = {"path": "docs/task.md", "content": content, "preimageSha256": sha256(content.encode()).hexdigest(),
        "repositoryRevision": REVISION, "provenance": {}}
    with pytest.raises(PlanningEvidenceError, match="unsatisfied or unsupported"):
        reconcile_dispositions(plan_id="artifact-1", repository_revision=REVISION,
            original_required_paths=["docs/task.md"], targets=[target],
            dispositions=[{"path": "docs/task.md", "disposition": "NO_CHANGE"}],
            postconditions_by_path={"docs/task.md": ({"kind": "STATUS_EQUALS", "value": "Completed"},)},
            evaluator_ref={"kind": "Evaluation", "name": "reconcile", "version": "1.0.0"})


def test_reconciliation_identity_binds_the_postcondition_proof() -> None:
    content = "**Status:** Completed\nmanual testing\n"
    target = {"path": "docs/task.md", "content": content, "preimageSha256": sha256(content.encode()).hexdigest(),
        "repositoryRevision": REVISION, "provenance": {}}
    common = {"plan_id": "artifact-1", "repository_revision": REVISION,
        "original_required_paths": ["docs/task.md"], "targets": [target],
        "dispositions": [{"path": "docs/task.md", "disposition": "NO_CHANGE"}],
        "evaluator_ref": {"kind": "Evaluation", "name": "reconcile", "version": "1.0.0"}}
    status = reconcile_dispositions(**common,
        postconditions_by_path={"docs/task.md": ({"kind": "STATUS_EQUALS", "value": "Completed"},)})
    text = reconcile_dispositions(**common,
        postconditions_by_path={"docs/task.md": ({"kind": "TEXT_PRESENT", "value": "manual testing"},)})
    assert status["id"] != text["id"]


@pytest.mark.parametrize("path", [".git/config", ".GIT/HEAD", ".", "docs/task.md/", "docs\\task.md"])
def test_repository_metadata_and_non_normalized_paths_are_rejected(path: str) -> None:
    with pytest.raises(PlanningEvidenceError, match="unsafe"):
        evaluate_path_predicates(path=path, content="text", repository_revision=REVISION,
            predicates=[{"kind": "TEXT_PRESENT", "value": "text"}], source_id="snapshot")
