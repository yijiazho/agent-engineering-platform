"""Deterministic, evidence-bound application of localized text edits.

The model supplies intent (anchors and replacement text), never a reconstructed
copy of an editable target.  This module is deliberately filesystem-free: all
validation happens against the immutable ContextPackage preimage before the
caller performs its single compare-and-write mutation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import PurePosixPath
from typing import Any


class LocalizedPatchError(ValueError):
    """A stable fail-closed diagnostic for an unsafe localized operation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class LocalizedPatch:
    content: str
    operations: tuple[dict[str, Any], ...]
    preservation: dict[str, Any]


def apply_localized_operations(
    *, path: str, preimage: str, preimage_sha256: str,
    repository_revision: str, operations: Sequence[Mapping[str, Any]],
    region_id: str | None = None, region: Mapping[str, Any] | None = None,
    allow_rewrite: bool = False, target_exists: bool = True,
) -> LocalizedPatch:
    """Apply ordered operations to *preimage* without mutating a workspace.

    Anchors are evaluated in the original preimage.  Every non-rewrite edit
    names an exact anchor and expected match count (normally one).  Spans must
    be strictly ordered and non-overlapping, which makes the output independent
    of implementation details such as incremental string offsets.
    """
    _safe_path(path)
    if not isinstance(preimage, str) or "\x00" in preimage or not _utf8(preimage):
        raise LocalizedPatchError("NON_UTF8_TARGET", "target must be UTF-8 text without NUL")
    if sha256(preimage.encode("utf-8")).hexdigest() != preimage_sha256:
        raise LocalizedPatchError("STALE_PREIMAGE", "preimage digest does not bind target content")
    if not isinstance(repository_revision, str) or len(repository_revision) != 40:
        raise LocalizedPatchError("STALE_REVISION", "repository revision must be immutable")
    if isinstance(operations, (str, bytes)) or not isinstance(operations, Sequence) or not operations:
        raise LocalizedPatchError("MISSING_OPERATION", "localized operations are required")

    # ``region_id`` was accepted by the original operation shape.  It is a
    # model declaration, not an authorization input: only the selector from
    # immutable planning evidence below may determine mutable scope.
    trusted_region: dict[str, Any] | None = None
    region_start, region_end = 0, len(preimage)
    if region is not None:
        region_start, region_end = _region_bounds(preimage, region)
        trusted_region = {
            "kind": region.get("kind"), "name": region.get("name"),
            "selectionId": region.get("selectionId"),
            "planArtifactId": region.get("planArtifactId"),
            "span": [region_start, region_end],
        }

    edits: list[tuple[int, int, str, dict[str, Any]]] = []
    rewrite_seen = False
    for ordinal, raw in enumerate(operations):
        if not isinstance(raw, Mapping):
            raise LocalizedPatchError("INVALID_OPERATION", "operation must be an object")
        operation = raw.get("operation")
        if operation not in {"insert", "replace", "delete", "rewrite"}:
            raise LocalizedPatchError("INVALID_OPERATION", "operation must be insert, replace, delete, or rewrite")
        if raw.get("path") != path:
            raise LocalizedPatchError("PATH_MISMATCH", "operation path differs from target")
        if raw.get("repositoryRevision") != repository_revision:
            raise LocalizedPatchError("STALE_REVISION", "operation revision differs from target")
        if raw.get("preimageSha256") != preimage_sha256:
            raise LocalizedPatchError("STALE_PREIMAGE", "operation digest differs from target")
        declared_region = raw.get("regionId")
        if declared_region is not None and not isinstance(declared_region, str):
            raise LocalizedPatchError("INVALID_OPERATION", "regionId must be a diagnostic string when present")
        content = raw.get("content", "")
        if not isinstance(content, str) or "\x00" in content or not _utf8(content):
            raise LocalizedPatchError("UNSAFE_CONTENT", "operation content must be UTF-8 text without NUL")
        if operation == "rewrite":
            # An empty immutable preimage is the distinct, bounded file-create
            # case.  Existing-file rewrite remains unavailable until AEP-059.
            if (not allow_rewrite and target_exists) or rewrite_seen or len(operations) != 1:
                raise LocalizedPatchError("REWRITE_NOT_AUTHORIZED", "full-file rewrite requires one explicit authorized operation")
            rewrite_seen = True
            edits.append((0, len(preimage), content, {"ordinal": ordinal, "operation": operation, "span": [0, len(preimage)]}))
            continue
        anchor = raw.get("anchor")
        expected = raw.get("expectedMatchCount")
        if not isinstance(anchor, str) or not anchor or not _utf8(anchor):
            raise LocalizedPatchError("MISSING_ANCHOR", "localized operation requires a non-empty anchor")
        if not isinstance(expected, int) or isinstance(expected, bool) or expected < 1:
            raise LocalizedPatchError("INVALID_MATCH_COUNT", "expectedMatchCount must be a positive integer")
        positions = _positions(preimage, anchor)
        if expected != 1 or len(positions) != 1:
            code = "ANCHOR_MISSING" if not positions else "ANCHOR_AMBIGUOUS"
            raise LocalizedPatchError(code, f"anchor matched {len(positions)} times; expected {expected}")
        start = positions[0]
        end = start + len(anchor)
        if operation == "insert":
            placement = raw.get("placement", "after")
            if placement not in {"before", "after"}:
                raise LocalizedPatchError("INVALID_OPERATION", "insert placement must be before or after")
            start = end if placement == "after" else start
            end = start
        elif operation == "delete":
            if content:
                raise LocalizedPatchError("INVALID_OPERATION", "delete must not include content")
            if anchor != preimage or len(operations) != 1:
                raise LocalizedPatchError(
                    "DELETE_NOT_AUTHORIZED",
                    "localized deletion requires plan-operation evidence; only an exact whole-file delete is available",
                )
        if start < region_start or end > region_end:
            raise LocalizedPatchError("OUT_OF_REGION", "operation anchor is outside the trusted region")
        record = {"ordinal": ordinal, "operation": operation, "span": [start, end],
                  "anchorSha256": sha256(anchor.encode()).hexdigest(),
                  "modelDeclaredRegionId": declared_region}
        if trusted_region is not None:
            record["trustedRegion"] = trusted_region
        edits.append((start, end, content, record))

    prior_start = prior_end = -1
    for start, end, _content, _record in edits:
        if start < prior_start:
            raise LocalizedPatchError("OUT_OF_ORDER_EDITS", "operation spans are not source ordered")
        if start < prior_end:
            raise LocalizedPatchError("OVERLAPPING_EDITS", "operation spans overlap")
        prior_start = start
        prior_end = max(prior_end, end)
    output: list[str] = []
    cursor = 0
    unchanged: list[str] = []
    records: list[dict[str, Any]] = []
    for start, end, replacement, record in edits:
        unchanged.append(preimage[cursor:start])
        output.extend((preimage[cursor:start], replacement))
        cursor = end
        records.append(record)
    unchanged.append(preimage[cursor:])
    output.append(preimage[cursor:])
    postimage = "".join(output)
    unchanged_bytes = "".join(unchanged).encode("utf-8")
    return LocalizedPatch(
        content=postimage,
        operations=tuple(records),
        preservation={
            "preimageSha256": preimage_sha256,
            "postimageSha256": sha256(postimage.encode()).hexdigest(),
            "unchangedRegionCount": len(unchanged),
            "unchangedBytes": len(unchanged_bytes),
            "unchangedSha256": sha256(unchanged_bytes).hexdigest(),
            "authorizedSpans": [record["span"] for record in records],
        },
    )


def _positions(content: str, needle: str) -> list[int]:
    positions: list[int] = []
    offset = 0
    while True:
        index = content.find(needle, offset)
        if index < 0:
            return positions
        positions.append(index)
        offset = index + 1


def _safe_path(value: str) -> None:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise LocalizedPatchError("UNSAFE_PATH", "operation path is unsafe")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) != value or path.parts[0].casefold() == ".git":
        raise LocalizedPatchError("UNSAFE_PATH", "operation path is unsafe")


def _utf8(value: str) -> bool:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _region_bounds(content: str, region: Mapping[str, Any]) -> tuple[int, int]:
    """Use the exact planning selector resolver for section and fence spans."""
    from aep.planning_evidence import PlanningEvidenceError, _region_span
    try:
        start, end, _identity, _matches = _region_span(content, region)
    except PlanningEvidenceError as error:
        raise LocalizedPatchError("TRUSTED_REGION_UNRESOLVED", "trusted region does not resolve uniquely") from error
    return start, end
