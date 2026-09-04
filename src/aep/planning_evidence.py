"""Deterministic, revision-bound evidence for implementation-plan paths."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import PurePosixPath
import re
from typing import Any


class PlanningEvidenceError(ValueError):
    """Raised when planning evidence is incomplete, stale, or contradictory."""


class PlanningEvidenceInspectionError(PlanningEvidenceError):
    """Safe, stable failure raised while inspecting an immutable source blob."""

    def __init__(self, reason: str, *, path: str, blob_size: int | None = None,
                 applied_ceiling: int | None = None, predicate_type: str | None = None,
                 strategy: str | None = None, evaluation_complete: bool = False) -> None:
        self.reason = reason
        self.metadata = {
            "reason": reason, "path": path, "blobSize": blob_size,
            "appliedTrustedCeiling": applied_ceiling, "predicateType": predicate_type,
            "inspectionStrategy": strategy, "evaluationComplete": evaluation_complete,
        }
        super().__init__(reason)


_STATUS = re.compile(r"^\*\*Status:\*\*\s*(?P<value>[^\r\n]+?)\s*$", re.MULTILINE)


def _structured_status_fields(content: str) -> list[tuple[str, int]]:
    """Return only the document's leading, structured Status field.

    A later literal mention is narrative, not metadata.  The field must occur
    before the first substantive body line (blank lines and a title are fine).
    """
    fields = []
    for match in _STATUS.finditer(content):
        prefix = content[:match.start()]
        lines = prefix.splitlines()
        substantive = [line for line in lines if line.strip() and not line.startswith("#")
                       and not _STATUS.match(line)]
        if substantive:
            continue
        fields.append((match.group("value"), content.count("\n", 0, match.start()) + 1))
    return fields


def _region_span(content: str, region: Mapping[str, Any]) -> tuple[int, int, str]:
    kind = region.get("kind")
    name = region.get("name")
    if not isinstance(kind, str) or not isinstance(name, str) or not name:
        raise PlanningEvidenceInspectionError("REGION_SELECTOR_MALFORMED", path="", evaluation_complete=False)
    if kind == "MARKDOWN_SECTION":
        pattern = re.compile(rf"(?m)^(?P<heading>#+)\s+{re.escape(name)}\s*$")
        matches = list(pattern.finditer(content))
        if not matches:
            raise PlanningEvidenceInspectionError("REGION_MISSING", path="", evaluation_complete=False)
        if len(matches) != 1:
            raise PlanningEvidenceInspectionError("REGION_AMBIGUOUS", path="", evaluation_complete=False)
        match = matches[0]
        level = len(match.group("heading"))
        end_match = re.search(rf"(?m)^#{{1,{level}}}\s+.+$", content[match.end():])
        end = match.end() + end_match.start() if end_match else len(content)
        return match.start(), end, f"markdown-section:{name}:line-{content.count(chr(10), 0, match.start()) + 1}"
    if kind == "MARKDOWN_FENCE":
        pattern = re.compile(rf"(?ms)^```[^\n]*\b{re.escape(name)}\b[^\n]*\n.*?^```\s*$")
        matches = list(pattern.finditer(content))
        if not matches:
            raise PlanningEvidenceInspectionError("REGION_MISSING", path="", evaluation_complete=False)
        if len(matches) != 1:
            raise PlanningEvidenceInspectionError("REGION_AMBIGUOUS", path="", evaluation_complete=False)
        match = matches[0]
        return match.start(), match.end(), f"markdown-fence:{name}:line-{content.count(chr(10), 0, match.start()) + 1}"
    raise PlanningEvidenceInspectionError("REGION_UNSUPPORTED", path="", evaluation_complete=False)


@dataclass(frozen=True, slots=True)
class PlanningEvidenceInspection:
    """Immutable blob identity plus only the source needed for evaluation."""

    content: str
    blob_size: int
    blob_sha256: str
    inspected_bytes: int
    status_fields: tuple[tuple[str, int], ...] = ()


def evaluate_path_predicates(
    *, path: str, content: str, repository_revision: str,
    predicates: Sequence[Mapping[str, Any]], source_id: str,
    max_bytes: int = 64 * 1024, blob_size: int | None = None,
    blob_sha256: str | None = None, declared_max_bytes: int | None = None,
    inspection_strategy: str = "COMPLETE_BLOB_SCAN",
    status_fields: Sequence[tuple[str, int]] | None = None,
    inspected_bytes: int | None = None,
    status_scan_bytes: int | None = None,
    region: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate bounded syntactic predicates and return body-free evidence."""
    _path(path)
    if not repository_revision or not source_id:
        raise PlanningEvidenceError("planning evidence requires revision and source provenance")
    if not isinstance(content, str) or "\x00" in content:
        raise PlanningEvidenceError(f"planning-evidence target {path!r} is not UTF-8 text")
    encoded = content.encode("utf-8")
    digest = sha256(encoded).hexdigest()
    complete_size = len(encoded) if blob_size is None else blob_size
    complete_digest = digest if blob_sha256 is None else blob_sha256
    if len(encoded) > max_bytes:
        raise PlanningEvidenceError(f"planning-evidence target {path!r} exceeds its byte limit")
    if not predicates:
        raise PlanningEvidenceError(f"planning-evidence target {path!r} has no predicates")
    region_identity = None
    scoped_content = content
    if region is not None:
        try:
            start, end, region_identity = _region_span(content, region)
        except PlanningEvidenceInspectionError as error:
            error.metadata["path"] = path
            raise
        scoped_content = content[start:end]
    results = []
    for predicate in predicates:
        kind, expected = predicate.get("kind"), predicate.get("value")
        if kind == "STATUS_EQUALS":
            fields = list(status_fields) if status_fields is not None else _structured_status_fields(content)
            if not fields:
                raise PlanningEvidenceInspectionError(
                    "STATUS_FIELD_MISSING", path=path, blob_size=complete_size,
                    applied_ceiling=max_bytes, predicate_type=kind,
                    strategy=inspection_strategy, evaluation_complete=True,
                )
            if len(fields) > 1:
                raise PlanningEvidenceInspectionError(
                    "STATUS_FIELD_AMBIGUOUS", path=path, blob_size=complete_size,
                    applied_ceiling=max_bytes, predicate_type=kind,
                    strategy=inspection_strategy, evaluation_complete=True,
                )
            actual, line = fields[0]
            satisfied = actual == expected
            selected = {"kind": "STRUCTURED_FIELD", "field": "Status", "line": line}
        elif kind in {"TEXT_PRESENT", "TEXT_ABSENT"}:
            if not isinstance(expected, str) or not expected:
                raise PlanningEvidenceError("text predicates require a non-empty value")
            positions = [match.start() for match in re.finditer(re.escape(expected), scoped_content)]
            actual = bool(positions)
            satisfied = actual if kind == "TEXT_PRESENT" else not actual
            selected = {"kind": "TEXT_MATCH", "occurrences": len(positions)}
        else:
            results.append({"predicate": dict(predicate), "result": "UNSUPPORTED", "selectedEvidence": None})
            continue
        results.append({"predicate": dict(predicate), "result": "MATCH" if satisfied else "NO_MATCH", "selectedEvidence": selected})
    partial_status_scan = inspection_strategy == "STRUCTURED_STATUS_FIELD_SCAN" and status_fields is not None
    if (not partial_status_scan and complete_size != len(encoded)) or (
        not partial_status_scan and complete_digest != digest
    ):
        raise PlanningEvidenceInspectionError(
            "BLOB_IDENTITY_MISMATCH", path=path, blob_size=complete_size,
            applied_ceiling=max_bytes, strategy=inspection_strategy,
        )
    record = {
        "path": path, "repositoryRevision": repository_revision,
        "preimageSha256": complete_digest, "sourceProvenance": {"sourceId": source_id},
        "predicateResults": results,
        "inspection": {
            "blobSize": complete_size,
            "inspectedBytes": len(encoded) if inspected_bytes is None else inspected_bytes,
            "appliedTrustedCeiling": max_bytes,
            "declaredMaxBytesHint": declared_max_bytes,
            "strategy": inspection_strategy, "evaluationComplete": True,
            "statusFieldScanBytes": status_scan_bytes,
            "region": None if region is None else {
                "kind": region.get("kind"), "name": region.get("name"),
                "identity": region_identity, "matchCount": sum(
                    int(item.get("selectedEvidence", {}).get("occurrences", 0))
                    for item in results if isinstance(item, Mapping)
                ), "evaluationComplete": True,
            },
        },
    }
    record["selectionId"] = "planselection-" + sha256(
        json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:20]
    return record


def finalize_planning_evidence(
    record: Mapping[str, Any], *, postconditions: Sequence[Mapping[str, Any]],
    selection_reasons: Sequence[str], postcondition_results: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Bind downstream criteria and selection reasons into the evidence identity."""
    if not postconditions or not selection_reasons:
        raise PlanningEvidenceError(
            "planning evidence requires postconditions and selection reasons"
        )
    result = _plain(record)
    result.pop("selectionId", None)
    result["postconditions"] = [dict(item) for item in postconditions]
    if postcondition_results is not None:
        result["postconditionResults"] = [dict(item) for item in postcondition_results]
    result["selectionReasons"] = list(selection_reasons)
    result["selectionId"] = "planselection-" + sha256(_canonical(result)).hexdigest()[:20]
    return result


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
        states = [result.get("result") for result in results if isinstance(result, Mapping)]
        if len(states) != len(results):
            raise PlanningEvidenceError(f"planning evidence for {path!r} has malformed predicate results")
        has_unsupported = "UNSUPPORTED" in states
        all_match = bool(states) and all(state == "MATCH" for state in states)
        conjunction_failed = bool(states) and not has_unsupported and not all_match
        postcondition_results = item.get("postconditionResults")
        postcondition_states = [
            result.get("result") for result in postcondition_results
            if isinstance(result, Mapping)
        ] if isinstance(postcondition_results, Sequence) else []
        postconditions_match = bool(postcondition_states) and all(
            state == "MATCH" for state in postcondition_states
        )
        if path in required and not all_match:
            raise PlanningEvidenceError(f"required-change path {path!r} does not satisfy its planning predicates")
        if path in no_change and (not conjunction_failed or not postconditions_match):
            raise PlanningEvidenceError(
                f"no-change path {path!r} lacks satisfied planning-time postconditions"
            )
        if path in unsupported and not (
            has_unsupported or (conjunction_failed and not postconditions_match)
        ):
            raise PlanningEvidenceError(f"unsupported path {path!r} lacks unsupported evidence")


def reconcile_dispositions(
    *, plan_id: str, repository_revision: str, original_required_paths: Sequence[str],
    targets: Sequence[Mapping[str, Any]], dispositions: Sequence[Mapping[str, Any]],
    postconditions_by_path: Mapping[str, Sequence[Mapping[str, Any]]], evaluator_ref: Mapping[str, str],
    proposed_contents_by_path: Mapping[str, str] | None = None,
    deleted_paths: Sequence[str] = (),
    required_insertions_by_path: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """Create immutable reconciliation evidence from freshly verified targets."""
    target_map = {item.get("path"): item for item in targets}
    if len(target_map) != len(targets):
        raise PlanningEvidenceError("editable targets contain duplicate paths")
    original_paths = set(original_required_paths)
    if len(original_paths) != len(original_required_paths) or set(target_map) != original_paths:
        raise PlanningEvidenceError(
            "reconciliation targets must exactly cover the original required paths"
        )
    disposition_map = {item.get("path"): item for item in dispositions}
    if len(disposition_map) != len(dispositions) or set(disposition_map) != set(target_map):
        raise PlanningEvidenceError("every editable target requires one terminal disposition")
    deleted = set(deleted_paths)
    if len(deleted) != len(deleted_paths) or not deleted.issubset(original_paths):
        raise PlanningEvidenceError("deleted paths must be unique original required paths")
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
        proof = None
        insertion_proof: list[dict[str, Any]] = []
        if state == "CHANGE":
            proposed = (proposed_contents_by_path or {}).get(path)
            if path in deleted:
                postconditions = postconditions_by_path.get(path, ())
                if not postconditions or any(
                    item.get("kind") != "TEXT_ABSENT"
                    for item in postconditions
                ):
                    raise PlanningEvidenceError(
                        f"DELETE for {path!r} requires TEXT_ABSENT postconditions"
                    )
                proof = {
                    "path": path,
                    "repositoryRevision": repository_revision,
                    "sourceProvenance": {"sourceId": "generated-delete"},
                    "postState": "ABSENT",
                    "predicateResults": [
                        {"predicate": dict(item), "result": "MATCH",
                         "selectedEvidence": {"kind": "PATH_ABSENCE"}}
                        for item in postconditions
                    ],
                }
            elif not isinstance(proposed, str):
                raise PlanningEvidenceError(
                    f"CHANGE for {path!r} lacks proposed content evidence"
                )
            else:
                proof = evaluate_path_predicates(
                    path=path, content=proposed, repository_revision=repository_revision,
                    predicates=postconditions_by_path.get(path, ()),
                    source_id="generated-change",
                )
            if any(item["result"] != "MATCH" for item in proof["predicateResults"]):
                raise PlanningEvidenceError(
                    f"CHANGE for {path!r} has an unsatisfied or unsupported postcondition"
                )
            effective.append(path)
        elif state == "NO_CHANGE":
            proof = evaluate_path_predicates(path=path, content=content, repository_revision=repository_revision,
                predicates=postconditions_by_path.get(path, ()), source_id=str(target.get("provenance", {}).get("taskExecutionId", "editable-target")))
            if any(item["result"] != "MATCH" for item in proof["predicateResults"]):
                raise PlanningEvidenceError(f"NO_CHANGE for {path!r} has an unsatisfied or unsupported criterion")
            for value in (required_insertions_by_path or {}).get(path, ()):
                matched = isinstance(value, str) and bool(value) and value in content
                insertion_proof.append({"value": value, "result": "MATCH" if matched else "NO_MATCH"})
            if any(item["result"] != "MATCH" for item in insertion_proof):
                raise PlanningEvidenceError(
                    f"NO_CHANGE for {path!r} lacks a required insertion"
                )
            no_change.append(path)
        else:
            raise PlanningEvidenceError(f"disposition for {path!r} must be CHANGE or NO_CHANGE")
        output = None if path in deleted else (proposed_contents_by_path or {}).get(path, content)
        records.append({"path": path, "disposition": state, "targetSha256": digest,
            "outputSha256": None if output is None else sha256(output.encode()).hexdigest(),
            "postState": "ABSENT" if path in deleted else "PRESENT",
            "postconditionProof": proof, "requiredInsertionProof": insertion_proof})
    record = {"planArtifactId": plan_id, "repositoryRevision": repository_revision,
        "originalRequiredPaths": sorted(original_required_paths), "effectiveRequiredPaths": effective,
        "verifiedNoChangePaths": no_change, "pathDispositions": records,
        "reason": "EXACT_EDITABLE_TARGET_RECONCILIATION", "evaluatorRef": dict(evaluator_ref)}
    record["id"] = "planreconciliation-" + sha256(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:20]
    return record


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(_plain(value), sort_keys=True, separators=(",", ":")).encode("utf-8")


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_plain(item) for item in value]
    return value


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
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise PlanningEvidenceError("planning evidence path is unsafe")
    path = PurePosixPath(value)
    if (path.is_absolute() or ".." in path.parts or str(path) != value
            or value == "." or path.parts[0].casefold() == ".git"):
        raise PlanningEvidenceError(f"planning evidence path {value!r} is unsafe")
