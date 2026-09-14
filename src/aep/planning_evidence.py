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


class CandidateReconciliationError(PlanningEvidenceError):
    """Raised when model output fails a deterministic postimage requirement."""


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


_STATUS = re.compile(
    r"^\*\*Status:\*\*[^\S\r\n]*(?P<value>\S(?:[^\r\n]*\S)?)[^\S\r\n]*$",
    re.MULTILINE,
)
_TITLE = re.compile(r"^ {0,3}#(?:[ \t]+|$)")
_STATUS_PREFIX = re.compile(r"^\*\*Status:\*\*")


def _is_markdown_blank(value: str) -> bool:
    return not value or all(character in " \t" for character in value)


def _is_nonempty_document_title(value: str) -> bool:
    if not _TITLE.match(value):
        return False
    title = value.lstrip(" ")[1:].strip(" \t")
    closing = re.match(r"^(?P<title>.*?)[ \t]+#+$", title)
    if closing:
        title = closing.group("title").rstrip(" \t")
    elif title and set(title) == {"#"}:
        title = ""
    return bool(title)


def _markdown_lines(content: str) -> Sequence[str]:
    start = 0
    lines = []
    for match in re.finditer(r"\r\n|[\r\n]", content):
        lines.append(content[start:match.end()])
        start = match.end()
    if start < len(content):
        lines.append(content[start:])
    return lines


def _structured_status_fields(content: str) -> list[tuple[str, int]]:
    """Return only the document's leading, structured Status field.

    A later literal mention is narrative, not metadata.  The field must occur
    before the first substantive body line (blank lines and a title are fine).
    """
    fields = []
    title_seen = False
    status_seen = False
    for line_number, line in enumerate(re.split(r"\r\n|[\r\n]", content), start=1):
        if _is_markdown_blank(line):
            continue
        match = _STATUS.fullmatch(line)
        if match:
            fields.append((match.group("value"), line_number))
            status_seen = True
            continue
        if _STATUS_PREFIX.match(line):
            raise PlanningEvidenceInspectionError(
                "STATUS_FIELD_MALFORMED", path="", evaluation_complete=True
            )
        if not title_seen and not status_seen and _is_nonempty_document_title(line):
            title_seen = True
            continue
        break
    return fields


def _region_span(content: str, region: Mapping[str, Any]) -> tuple[int, int, str, int]:
    kind = region.get("kind")
    name = region.get("name")
    if not isinstance(kind, str) or not isinstance(name, str) or not name.strip():
        raise PlanningEvidenceInspectionError("REGION_SELECTOR_MALFORMED", path="", evaluation_complete=False)
    if kind == "WHOLE_FILE":
        if name != "WHOLE_FILE":
            raise PlanningEvidenceInspectionError("REGION_SELECTOR_MALFORMED", path="", evaluation_complete=False)
        return 0, len(content), "whole-file", 1
    headings, fences = _markdown_structure(content)
    if kind == "MARKDOWN_SECTION":
        matches = [item for item in headings if item[1] == name]
        if not matches:
            raise PlanningEvidenceInspectionError("REGION_MISSING", path="", evaluation_complete=False)
        if len(matches) != 1:
            raise PlanningEvidenceInspectionError("REGION_AMBIGUOUS", path="", evaluation_complete=False)
        start, _heading_name, level, line = matches[0]
        end = next(
            (offset for offset, _title, candidate_level, _line in headings
             if offset > start and candidate_level <= level),
            len(content),
        )
        return (start, end,
                f"markdown-section:{name}:line-{line}",
                len(matches))
    if kind == "MARKDOWN_FENCE":
        matches = [item for item in fences if name in item[2].split()]
        if not matches:
            raise PlanningEvidenceInspectionError("REGION_MISSING", path="", evaluation_complete=False)
        if len(matches) != 1:
            raise PlanningEvidenceInspectionError("REGION_AMBIGUOUS", path="", evaluation_complete=False)
        start, end, _info, line = matches[0]
        return (start, end,
                f"markdown-fence:{name}:line-{line}",
                len(matches))
    raise PlanningEvidenceInspectionError("REGION_UNSUPPORTED", path="", evaluation_complete=False)


def _markdown_structure(
    content: str,
) -> tuple[list[tuple[int, str, int, int]], list[tuple[int, int, str, int]]]:
    """Return headings outside fences and complete fenced regions."""
    headings: list[tuple[int, str, int, int]] = []
    fences: list[tuple[int, int, str, int]] = []
    active: tuple[str, int, int, str, int] | None = None
    html_terminator: re.Pattern[str] | None = None
    paragraph_active = False
    offset = 0
    for line_number, line in enumerate(_markdown_lines(content), start=1):
        text = line.rstrip("\r\n")
        fence = re.match(
            r"^ {0,3}(?:(?P<ticks>`{3,})(?P<tick_info>[^`]*)|"
            r"(?P<tildes>~{3,})(?P<tilde_info>.*))$",
            text,
        )
        if active is not None:
            paragraph_active = False
            marker_char, marker_length, start, info, start_line = active
            if re.match(rf"^ {{0,3}}{re.escape(marker_char)}{{{marker_length},}}[ \t]*$", text):
                fences.append((start, offset, info, start_line))
                active = None
        elif html_terminator is not None:
            paragraph_active = False
            if html_terminator.search(text):
                html_terminator = None
        elif html := re.match(
            r"^ {0,3}<(?P<tag>script|pre|style|textarea)(?:\s|>|$)",
            text,
            re.IGNORECASE,
        ):
            paragraph_active = False
            terminator = re.compile(
                rf"</{re.escape(html.group('tag'))}\s*>", re.IGNORECASE
            )
            if not terminator.search(text, html.end()):
                html_terminator = terminator
        elif "<!--" in text and re.match(r"^ {0,3}<!--", text):
            paragraph_active = False
            if "-->" not in text[text.index("<!--") + 4:]:
                html_terminator = re.compile(r"-->")
        elif re.match(r"^ {0,3}<\?", text):
            paragraph_active = False
            if "?>" not in text:
                html_terminator = re.compile(r"\?>")
        elif re.match(r"^ {0,3}<![A-Z]", text):
            paragraph_active = False
            if ">" not in text:
                html_terminator = re.compile(r">")
        elif re.match(r"^ {0,3}<!\[CDATA\[", text, re.IGNORECASE):
            paragraph_active = False
            if "]]>" not in text:
                html_terminator = re.compile(r"\]\]>")
        elif re.match(
            r"^ {0,3}</?(?:address|article|aside|base|basefont|blockquote|body|"
            r"caption|center|col|colgroup|dd|details|dialog|dir|div|dl|dt|"
            r"fieldset|figcaption|figure|footer|form|frame|frameset|h[1-6]|head|"
            r"header|hgroup|hr|html|iframe|legend|li|link|main|menu|menuitem|nav|"
            r"noframes|ol|optgroup|option|p|param|search|section|summary|table|"
            r"tbody|td|tfoot|th|thead|title|tr|track|ul)(?:\s|/?>|$)",
            text,
            re.IGNORECASE,
        ):
            paragraph_active = False
            html_terminator = re.compile(r"^[ \t]*$")
        elif not paragraph_active and re.match(
            r"^ {0,3}</?[A-Za-z][A-Za-z0-9-]*"
            r"(?:\s+[A-Za-z_:][A-Za-z0-9_.:-]*"
            r"(?:\s*=\s*(?:[^ \t\r\n\"'=<>`]+|'[^']*'|\"[^\"]*\"))?)*"
            r"\s*/?>[ \t]*$",
            text,
        ):
            paragraph_active = False
            html_terminator = re.compile(r"^[ \t]*$")
        elif fence:
            paragraph_active = False
            marker = fence.group("ticks") or fence.group("tildes")
            info = fence.group("tick_info") or fence.group("tilde_info") or ""
            active = (
                marker[0], len(marker), offset + len(line),
                info.strip(), line_number,
            )
        else:
            heading = re.match(
                r"^ {0,3}(?P<marks>#{1,6})(?:[ \t]+(?P<body>.*)|[ \t]*)$",
                text,
            )
            if heading:
                paragraph_active = False
                title = (heading.group("body") or "").rstrip(" \t")
                closing = re.match(r"^(?P<title>.*?)[ \t]+#+$", title)
                if closing:
                    title = closing.group("title").rstrip(" \t")
                elif title and set(title) == {"#"}:
                    title = ""
                headings.append((offset, title, len(heading.group("marks")), line_number))
            else:
                paragraph_active = bool(text.strip())
        offset += len(line)
    if active is not None:
        raise PlanningEvidenceInspectionError(
            "REGION_MALFORMED", path="", evaluation_complete=False
        )
    return headings, fences


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
    distinct_text_matches: bool = False,
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
    region_match_count = 0
    scoped_content = content
    if region is not None:
        try:
            start, end, region_identity, region_match_count = _region_span(content, region)
        except PlanningEvidenceInspectionError as error:
            error.metadata["path"] = path
            raise
        scoped_content = content[start:end]
    results = []
    distinct_candidates: list[tuple[int, str, list[int]]] = []
    for predicate in predicates:
        kind, expected = predicate.get("kind"), predicate.get("value")
        if kind == "STATUS_EQUALS":
            try:
                if region is not None:
                    base_line = len(re.findall(r"\r\n|[\r\n]", content[:start]))
                    status_content = scoped_content
                    if region.get("kind") == "MARKDOWN_SECTION":
                        heading_ending = re.search(r"\r\n|[\r\n]", scoped_content)
                        if heading_ending:
                            status_content = scoped_content[heading_ending.end():]
                            base_line += 1
                    fields = [
                        (value, line + base_line)
                        for value, line in _structured_status_fields(status_content)
                    ]
                else:
                    fields = list(status_fields) if status_fields is not None else _structured_status_fields(content)
            except PlanningEvidenceInspectionError as error:
                error.metadata.update({
                    "path": path, "blobSize": complete_size,
                    "appliedTrustedCeiling": max_bytes, "predicateType": kind,
                    "inspectionStrategy": inspection_strategy,
                })
                raise
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
            # The same logical multiline insertion can be represented by a
            # CRLF checkout or by unified-diff added records without the final
            # newline.  Preserve exact characters otherwise, but compare this
            # transport boundary consistently with Patch Evaluation.
            expected_text = _canonical_insertion(expected)
            scoped_text = _canonical_insertion(scoped_content)
            positions = _logical_text_positions(scoped_text, expected_text)
            actual = bool(positions)
            satisfied = actual if kind == "TEXT_PRESENT" else not actual
            selected = {"kind": "TEXT_MATCH", "occurrences": len(positions)}
        else:
            results.append({"predicate": dict(predicate), "result": "UNSUPPORTED", "selectedEvidence": None})
            continue
        result_index = len(results)
        results.append({"predicate": dict(predicate), "result": "MATCH" if satisfied else "NO_MATCH", "selectedEvidence": selected})
        if distinct_text_matches and kind == "TEXT_PRESENT":
            distinct_candidates.append((result_index, expected_text, positions))
    if distinct_candidates:
        selected_positions = _independent_text_positions(distinct_candidates)
        for result_index, _value, positions in distinct_candidates:
            result = results[result_index]
            result["result"] = (
                "MATCH" if result_index in selected_positions else "NO_MATCH"
            )
            result["selectedEvidence"] = {
                "kind": "TEXT_MATCH",
                "occurrences": len(positions),
                "independentOccurrence": result_index in selected_positions,
            }
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
                "identity": region_identity, "matchCount": region_match_count,
                "evaluationComplete": True,
            },
        },
    }
    record["selectionId"] = "planselection-" + sha256(
        json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:20]
    return record


def _canonical_insertion(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _logical_text_positions(content: str, expected: str) -> list[int]:
    """Match diff's optional terminal newline without accepting a prefix."""
    if not expected.endswith("\n"):
        return [match.start() for match in re.finditer(re.escape(expected), content)]
    candidates = (expected, expected[:-1])
    positions: list[int] = []
    for candidate in candidates:
        start = content.find(candidate)
        while start >= 0:
            end = start + len(candidate)
            if (start == 0 or content[start - 1] == "\n") and (
                candidate.endswith("\n") or end == len(content) or content[end] == "\n"
            ):
                positions.append(start)
            start = content.find(candidate, start + 1)
        if positions:
            return positions
    return positions


def _independent_text_positions(
    candidates: Sequence[tuple[int, str, Sequence[int]]],
) -> set[int]:
    """Find one non-overlapping occurrence for each required text value."""
    ordered = sorted(candidates, key=lambda item: (len(item[2]), item[0]))
    attempts = 0

    def select(
        offset: int, intervals: tuple[tuple[int, int], ...], chosen: dict[int, int],
    ) -> dict[int, int] | None:
        nonlocal attempts
        if offset == len(ordered):
            return chosen
        result_index, value, positions = ordered[offset]
        for position in positions:
            attempts += 1
            if attempts > 256:
                return None
            interval = (position, position + len(value))
            if any(interval[0] < end and start < interval[1] for start, end in intervals):
                continue
            result = select(
                offset + 1, (*intervals, interval), {**chosen, result_index: position})
            if result is not None:
                return result
        return None

    selected = select(0, (), {})
    return set(selected or {})


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


def scope_disposition(record: Mapping[str, Any]) -> str:
    """Classify one trusted scope without conflating sibling scopes."""
    results = record.get("predicateResults", ())
    postconditions = record.get("postconditionResults", ())
    if not isinstance(results, Sequence) or isinstance(results, (str, bytes)) or not results:
        raise PlanningEvidenceError("planning evidence has malformed predicate results")
    states = [item.get("result") for item in results if isinstance(item, Mapping)]
    if len(states) != len(results):
        raise PlanningEvidenceError("planning evidence has malformed predicate results")
    if "UNSUPPORTED" in states:
        return "UNSUPPORTED"
    if all(state == "MATCH" for state in states):
        return "CHANGE"
    if isinstance(postconditions, Sequence) and not isinstance(postconditions, (str, bytes)) and postconditions:
        post_states = [item.get("result") for item in postconditions if isinstance(item, Mapping)]
        if len(post_states) == len(postconditions) and all(state == "MATCH" for state in post_states):
            return "NO_CHANGE"
    return "UNSUPPORTED"


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
    by_path: dict[str, list[Mapping[str, Any]]] = {}
    for item in evidence:
        if not isinstance(item, Mapping):
            raise PlanningEvidenceError("pathEvidence must contain planning evidence records")
        path = item.get("path")
        _path(path)
        by_path.setdefault(path, []).append(item)
    if set(by_path) != set(authorized):
        raise PlanningEvidenceError("pathEvidence must cover every authorized path")
    trusted_by_id: dict[str, Mapping[str, Any]] = {}
    for item in trusted_path_evidence:
        selection_id = item.get("selectionId") if isinstance(item, Mapping) else None
        if not isinstance(selection_id, str) or not selection_id or selection_id in trusted_by_id:
            raise PlanningEvidenceError("trusted planning evidence has invalid or duplicate identities")
        trusted_by_id[selection_id] = item
    for path, records in by_path.items():
        # Multiple scopes for the same immutable path are expected.  Duplicate
        # selection IDs, however, would make the model's evidence citation
        # ambiguous and remain fail-closed.
        if len({str(item.get("selectionId")) for item in records}) != len(records):
            raise PlanningEvidenceError("pathEvidence has duplicate scope identities")
        editable_records = [item for item in records if item.get("authorizationRole") != "EVALUATOR_ONLY"]
        deciding_records = editable_records or records
        for item in records:
            if item.get("repositoryRevision") != repository_revision:
                raise PlanningEvidenceError(f"planning evidence for {path!r} is revision-mismatched")
            if not re.fullmatch(r"[0-9a-f]{64}", str(item.get("preimageSha256", ""))):
                raise PlanningEvidenceError(f"planning evidence for {path!r} lacks a content digest")
            trusted = trusted_by_id.get(str(item.get("selectionId", "")))
            if trusted is None or _canonical(item) != _canonical(trusted):
                raise PlanningEvidenceError(
                    f"planning evidence for {path!r} does not match trusted Context Builder evidence"
                )
        dispositions = [scope_disposition(item) for item in deciding_records]
        if path in required and ("UNSUPPORTED" in dispositions or "CHANGE" not in dispositions):
            raise PlanningEvidenceError(f"required-change path {path!r} does not satisfy its planning predicates")
        if path in no_change and (not dispositions or any(value != "NO_CHANGE" for value in dispositions)):
            raise PlanningEvidenceError(f"no-change path {path!r} lacks satisfied planning-time postconditions")
        if path in unsupported and "UNSUPPORTED" not in dispositions:
            raise PlanningEvidenceError(f"unsupported path {path!r} lacks unsupported evidence")


def reconcile_dispositions(
    *, plan_id: str, repository_revision: str, original_required_paths: Sequence[str],
    targets: Sequence[Mapping[str, Any]], dispositions: Sequence[Mapping[str, Any]],
    postconditions_by_path: Mapping[str, Sequence[Mapping[str, Any]]], evaluator_ref: Mapping[str, str],
    proposed_contents_by_path: Mapping[str, str] | None = None,
    deleted_paths: Sequence[str] = (),
    required_insertions_by_path: Mapping[str, Sequence[str]] | None = None,
    regions_by_path: Mapping[str, Mapping[str, Any]] | None = None,
    max_bytes: int = 64 * 1024,
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
    def scoped_proof(path: str, value: str, source_id: str) -> dict[str, Any]:
        raw_scopes = postconditions_by_path.get(path, ())
        # Legacy callers pass a flat predicate sequence. New planning evidence
        # carries each predicate set with its own immutable region.
        scopes = raw_scopes if raw_scopes and isinstance(raw_scopes[0], Mapping) and "predicates" in raw_scopes[0] else ({"predicates": raw_scopes, "region": (regions_by_path or {}).get(path)},)
        proofs = []
        for scope in scopes:
            predicates = scope.get("predicates", ())
            if not isinstance(predicates, Sequence):
                raise PlanningEvidenceError(f"planning evidence for {path!r} has malformed scope predicates")
            proof = evaluate_path_predicates(
                path=path, content=value, repository_revision=repository_revision,
                predicates=predicates, source_id=source_id,
                region=scope.get("region"), max_bytes=max_bytes,
            )
            proofs.append({"selectionId": scope.get("selectionId"), "region": scope.get("region"),
                           "predicateResults": proof["predicateResults"]})
        return {"path": path, "repositoryRevision": repository_revision,
                "sourceProvenance": {"sourceId": source_id}, "scopeProofs": proofs,
                "predicateResults": [result for proof in proofs for result in proof["predicateResults"]]}

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
        region = (regions_by_path or {}).get(path)
        proof = None
        insertion_proof: list[dict[str, Any]] = []
        if state == "CHANGE":
            proposed = (proposed_contents_by_path or {}).get(path)
            if path in deleted:
                if region is not None and region.get("kind") != "WHOLE_FILE":
                    raise PlanningEvidenceError(
                        f"DELETE for {path!r} cannot rely on region-scoped evidence"
                    )
                raw_postconditions = postconditions_by_path.get(path, ())
                postconditions = (
                    tuple(predicate for scope in raw_postconditions
                          if isinstance(scope, Mapping)
                          for predicate in scope.get("predicates", ())
                          if isinstance(predicate, Mapping))
                    if raw_postconditions and isinstance(raw_postconditions[0], Mapping)
                    and "predicates" in raw_postconditions[0]
                    else raw_postconditions
                )
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
                proof = scoped_proof(path, proposed, "generated-change")
            if any(item["result"] != "MATCH" for item in proof["predicateResults"]):
                raise CandidateReconciliationError(
                    f"CHANGE for {path!r} has an unsatisfied or unsupported postcondition"
                )
            effective.append(path)
        elif state == "NO_CHANGE":
            proof = scoped_proof(path, content, str(target.get("provenance", {}).get("taskExecutionId", "editable-target")))
            if any(item["result"] != "MATCH" for item in proof["predicateResults"]):
                raise CandidateReconciliationError(f"NO_CHANGE for {path!r} has an unsatisfied or unsupported criterion")
            no_change.append(path)
        else:
            raise PlanningEvidenceError(f"disposition for {path!r} must be CHANGE or NO_CHANGE")
        output = None if path in deleted else (proposed_contents_by_path or {}).get(path, content)
        insertion_values = tuple((required_insertions_by_path or {}).get(path, ()))
        if insertion_values:
            if output is None:
                raise CandidateReconciliationError(
                    f"{state} for {path!r} lacks a required insertion"
                )
            raw_scopes = postconditions_by_path.get(path, ())
            scopes = raw_scopes if raw_scopes and isinstance(raw_scopes[0], Mapping) and "predicates" in raw_scopes[0] else ({"region": region},)
            if len(scopes) == 1 and scopes[0].get("selectionId") is None:
                insertion_record = evaluate_path_predicates(
                    path=path, content=output, repository_revision=repository_revision,
                    predicates=[{"kind": "TEXT_PRESENT", "value": value} for value in insertion_values],
                    source_id="generated-insertion-reconciliation", region=scopes[0].get("region"),
                    max_bytes=max_bytes, distinct_text_matches=True,
                )
                insertion_proof = [{"value": value, "result": result["result"]}
                                   for value, result in zip(insertion_values, insertion_record["predicateResults"], strict=True)]
            else:
                insertion_proof = []
                for value in insertion_values:
                    matches = []
                    for scope in scopes:
                        result = evaluate_path_predicates(
                            path=path, content=output, repository_revision=repository_revision,
                            predicates=[{"kind": "TEXT_PRESENT", "value": value}],
                            source_id="generated-insertion-reconciliation",
                            region=scope.get("region"), max_bytes=max_bytes,
                        )
                        if result["predicateResults"][0]["result"] == "MATCH":
                            matches.append(scope.get("selectionId"))
                    insertion_proof.append({"value": value,
                                            "result": "MATCH" if len(matches) == 1 else "NO_MATCH",
                                            "selectionId": matches[0] if len(matches) == 1 else None})
            if any(item["result"] != "MATCH" for item in insertion_proof):
                raise CandidateReconciliationError(
                    f"{state} for {path!r} lacks a required insertion"
                )
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
