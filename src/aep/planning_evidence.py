"""Deterministic, revision-bound evidence for implementation-plan paths."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from hashlib import sha256
import json
from pathlib import PurePosixPath
import re
from typing import Any


class PlanningEvidenceError(ValueError):
    """Raised when planning evidence is incomplete, stale, or contradictory."""


_STATUS = re.compile(r"^\*\*Status:\*\*\s*(?P<value>[^\r\n]+?)\s*$", re.MULTILINE)


def evaluate_path_predicates(
    *, path: str, content: str, repository_revision: str,
    predicates: Sequence[Mapping[str, Any]], source_id: str,
    max_bytes: int = 64 * 1024,
) -> dict[str, Any]:
    """Evaluate bounded syntactic predicates and return body-free evidence."""
    _path(path)
    if not repository_revision or not source_id:
        raise PlanningEvidenceError("planning evidence requires revision and source provenance")
    if not isinstance(content, str) or "\x00" in content:
        raise PlanningEvidenceError(f"planning-evidence target {path!r} is not UTF-8 text")
    encoded = content.encode("utf-8")
    if len(encoded) > max_bytes:
        raise PlanningEvidenceError(f"planning-evidence target {path!r} exceeds its byte limit")
    if not predicates:
        raise PlanningEvidenceError(f"planning-evidence target {path!r} has no predicates")
    results = []
    for predicate in predicates:
        kind, expected = predicate.get("kind"), predicate.get("value")
        if kind == "STATUS_EQUALS":
            matches = list(_STATUS.finditer(content))
            if len(matches) != 1:
                raise PlanningEvidenceError(f"planning-evidence target {path!r} has an ambiguous status field")
            actual = matches[0].group("value")
            satisfied = actual == expected
            selected = {"kind": "STRUCTURED_FIELD", "field": "Status", "line": content.count("\n", 0, matches[0].start()) + 1}
        elif kind in {"TEXT_PRESENT", "TEXT_ABSENT"}:
            if not isinstance(expected, str) or not expected:
                raise PlanningEvidenceError("text predicates require a non-empty value")
            positions = [match.start() for match in re.finditer(re.escape(expected), content)]
            actual = bool(positions)
            satisfied = actual if kind == "TEXT_PRESENT" else not actual
            selected = {"kind": "TEXT_MATCH", "occurrences": len(positions)}
        else:
            results.append({"predicate": dict(predicate), "result": "UNSUPPORTED", "selectedEvidence": None})
            continue
        results.append({"predicate": dict(predicate), "result": "MATCH" if satisfied else "NO_MATCH", "selectedEvidence": selected})
    digest = sha256(encoded).hexdigest()
    record = {
        "path": path, "repositoryRevision": repository_revision,
        "preimageSha256": digest, "sourceProvenance": {"sourceId": source_id},
        "predicateResults": results,
    }
    record["selectionId"] = "planselection-" + sha256(
        json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:20]
    return record


def validate_plan_path_contract(
    plan: Mapping[str, Any],
    repository_revision: str,
    *,
    trusted_path_evidence: Sequence[Mapping[str, Any]],
) -> None:
    """Require model decisions to cite independently materialized evidence."""
    authorized = _paths(plan.get("authorizedPaths"), "authorizedPaths", required=True)
    required = _paths(plan.get("requiredChangePaths"), "requiredChangePaths")
    no_change = _paths(plan.get("verifiedNoChangePaths"), "verifiedNoChangePaths")
    unsupported = _paths(plan.get("unsupportedPaths"), "unsupportedPaths")
    groups = (set(required), set(no_change), set(unsupported))
    if any(groups[i] & groups[j] for i in range(3) for j in range(i + 1, 3)):
        raise PlanningEvidenceError("implementation-plan path dispositions conflict")
    if set().union(*groups) != set(authorized):
        raise PlanningEvidenceError("every authorized path requires exactly one disposition")
    evidence = plan.get("pathEvidence")
    if isinstance(evidence, (str, bytes)) or not isinstance(evidence, Sequence):
        raise PlanningEvidenceError("implementation plan requires pathEvidence")
    by_path: dict[str, Mapping[str, Any]] = {}
    for item in evidence:
        if not isinstance(item, Mapping) or item.get("path") in by_path:
            raise PlanningEvidenceError("pathEvidence must contain one unique record per path")
        path = item.get("path")
        _path(path)
        by_path[path] = item
    if set(by_path) != set(authorized):
        raise PlanningEvidenceError("pathEvidence must cover every authorized path")
    trusted_by_id: dict[str, Mapping[str, Any]] = {}
    for item in trusted_path_evidence:
        selection_id = item.get("selectionId") if isinstance(item, Mapping) else None
        if not isinstance(selection_id, str) or not selection_id or selection_id in trusted_by_id:
            raise PlanningEvidenceError("trusted planning evidence has invalid or duplicate identities")
        trusted_by_id[selection_id] = item
    for path, item in by_path.items():
        if item.get("repositoryRevision") != repository_revision:
            raise PlanningEvidenceError(f"planning evidence for {path!r} is revision-mismatched")
        if not re.fullmatch(r"[0-9a-f]{64}", str(item.get("preimageSha256", ""))):
            raise PlanningEvidenceError(f"planning evidence for {path!r} lacks a content digest")
        trusted = trusted_by_id.get(str(item.get("selectionId", "")))
        if trusted is None or _canonical(item) != _canonical(trusted):
            raise PlanningEvidenceError(
                f"planning evidence for {path!r} does not match trusted Context Builder evidence"
            )
        results = item.get("predicateResults")
        if not isinstance(results, Sequence) or not results:
            raise PlanningEvidenceError(f"planning evidence for {path!r} lacks predicate results")
        states = {result.get("result") for result in results if isinstance(result, Mapping)}
        if path in required and ("UNSUPPORTED" in states or not states.intersection({"MATCH", "NO_MATCH"})):
            raise PlanningEvidenceError(f"required-change path {path!r} lacks deterministic predicate evidence")
        if path in no_change and states != {"MATCH"}:
            raise PlanningEvidenceError(f"no-change path {path!r} is not fully proven")
        if path in unsupported and "UNSUPPORTED" not in states:
            raise PlanningEvidenceError(f"unsupported path {path!r} lacks unsupported evidence")


def reconcile_dispositions(
    *, plan_id: str, repository_revision: str, original_required_paths: Sequence[str],
    targets: Sequence[Mapping[str, Any]], dispositions: Sequence[Mapping[str, Any]],
    postconditions_by_path: Mapping[str, Sequence[Mapping[str, Any]]], evaluator_ref: Mapping[str, str],
) -> dict[str, Any]:
    """Create immutable reconciliation evidence from freshly verified targets."""
    target_map = {item.get("path"): item for item in targets}
    if len(target_map) != len(targets):
        raise PlanningEvidenceError("editable targets contain duplicate paths")
    disposition_map = {item.get("path"): item for item in dispositions}
    if len(disposition_map) != len(dispositions) or set(disposition_map) != set(target_map):
        raise PlanningEvidenceError("every editable target requires one terminal disposition")
    effective, no_change, records = [], [], []
    for path in sorted(target_map, key=lambda value: (str(value).casefold(), str(value))):
        target, disposition = target_map[path], disposition_map[path]
        if target.get("repositoryRevision") != repository_revision:
            raise PlanningEvidenceError(f"editable target {path!r} is revision-mismatched")
        content = target.get("content")
        digest = sha256(content.encode()).hexdigest() if isinstance(content, str) else ""
        if digest != target.get("preimageSha256"):
            raise PlanningEvidenceError(f"editable target {path!r} has stale content evidence")
        state = disposition.get("disposition")
        if state == "CHANGE":
            effective.append(path)
        elif state == "NO_CHANGE":
            proof = evaluate_path_predicates(path=path, content=content, repository_revision=repository_revision,
                predicates=postconditions_by_path.get(path, ()), source_id=str(target.get("provenance", {}).get("taskExecutionId", "editable-target")))
            if any(item["result"] != "MATCH" for item in proof["predicateResults"]):
                raise PlanningEvidenceError(f"NO_CHANGE for {path!r} has an unsatisfied or unsupported criterion")
            no_change.append(path)
        else:
            raise PlanningEvidenceError(f"disposition for {path!r} must be CHANGE or NO_CHANGE")
        records.append({"path": path, "disposition": state, "targetSha256": digest})
    record = {"planArtifactId": plan_id, "repositoryRevision": repository_revision,
        "originalRequiredPaths": sorted(original_required_paths), "effectiveRequiredPaths": effective,
        "verifiedNoChangePaths": no_change, "pathDispositions": records,
        "reason": "EXACT_EDITABLE_TARGET_RECONCILIATION", "evaluatorRef": dict(evaluator_ref)}
    record["id"] = "planreconciliation-" + sha256(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:20]
    return record


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _paths(value: Any, field: str, required: bool = False) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or (required and not value):
        raise PlanningEvidenceError(f"{field} must be an array" + (" with paths" if required else ""))
    result = tuple(value)
    if len(set(result)) != len(result):
        raise PlanningEvidenceError(f"{field} contains duplicates")
    for path in result:
        _path(path)
    return result


def _path(value: Any) -> None:
    if not isinstance(value, str) or not value or "\\" in value:
        raise PlanningEvidenceError("planning evidence path is unsafe")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) != value:
        raise PlanningEvidenceError(f"planning evidence path {value!r} is unsafe")
