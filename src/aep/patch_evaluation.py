"""Deterministic applicability and path-boundary evaluation for patch artifacts."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from functools import cache
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource as SchemaResource
from referencing.jsonschema import DRAFT202012

from aep.git_tool import GitTool, GitToolAdapter, _decode_patch_path, git_tool_validator
from aep.observability import CorrelationContext, bind_correlation
from aep.runtime_store import RuntimeObject, RuntimeObjectStore
from aep.tool_runtime import ToolCaller, ToolRequest, ToolResultStatus, invoke_tool


_REVISION_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")


class PatchEvaluationContractError(ValueError):
    """Raised when evaluator inputs cannot form a valid EvaluationResult."""


def evaluate_patch(
    *,
    store: RuntimeObjectStore,
    git_adapter: GitToolAdapter,
    authorize_git: Callable[[ToolRequest], bool],
    result_id: str,
    task_execution_id: str,
    evaluation_ref: Mapping[str, Any],
    patch_artifact: Mapping[str, Any],
    patch_content: str | bytes,
    expected_revision: str,
    allowed_paths: Sequence[str],
    required_paths: Sequence[str] | None = None,
    no_change_paths: Sequence[str] = (),
    deletion_authorized_paths: Sequence[str] = (),
    required_insertions: Sequence[Mapping[str, str]] = (),
    unsupported_acceptance_criteria: Sequence[str] = (),
    working_branch: str,
    correlation: CorrelationContext | Mapping[str, Any],
    timestamp: str,
    provenance: Mapping[str, Any],
    git_tool_ref: Mapping[str, Any] | None = None,
    git_tool: GitTool | None = None,
    tool_invocation_id: str | None = None,
    timeout_ms: int = 5_000,
) -> RuntimeObject:
    """Evaluate a patch without applying it and persist immutable evidence."""

    if tool_invocation_id is not None and (
        not isinstance(tool_invocation_id, str) or not tool_invocation_id
    ):
        raise PatchEvaluationContractError(
            "tool_invocation_id must be a non-empty string"
        )

    context = bind_correlation(
        correlation,
        task_execution_id=task_execution_id,
        provenance=provenance,
    )
    artifact = deepcopy(dict(patch_artifact))
    normalized_rules = _normalize_rules(allowed_paths)
    normalized_required = _normalize_rules(required_paths) if required_paths else ()
    normalized_no_change = _normalize_rules(no_change_paths) if no_change_paths else ()
    normalized_deletion_authorized = _normalize_rules(deletion_authorized_paths) if deletion_authorized_paths else ()
    errors: list[dict[str, str]] = []
    logs: list[str] = []
    changed_files: list[str] = []
    boundary_checks: list[dict[str, Any]] = []
    diagnostics: list[str] = []
    applicable = False
    git_status = "NOT_RUN"
    git_logs_ref: str | None = None

    content = _patch_bytes(patch_content, errors)
    artifact_id = artifact.get("id")
    if artifact.get("kind") != "GeneratedArtifact" or artifact.get("artifactType") != "PATCH":
        errors.append(
            {
                "code": "INVALID_ARTIFACT",
                "message": "target must be a PATCH GeneratedArtifact",
            }
        )
    if not isinstance(artifact_id, str) or not artifact_id:
        raise PatchEvaluationContractError("patch artifact id must be a non-empty string")

    actual_address = f"sha256:{sha256(content).hexdigest()}"
    if artifact.get("contentAddress") != actual_address:
        errors.append(
            {
                "code": "CONTENT_MISMATCH",
                "message": "patch content does not match the artifact content address",
            }
        )

    revision = expected_revision.lower()
    if not _REVISION_PATTERN.fullmatch(expected_revision):
        raise PatchEvaluationContractError(
            "expected_revision must be an immutable 40-character commit id"
        )
    artifact_revision = artifact.get("repositoryRevision")
    if not isinstance(artifact_revision, str) or artifact_revision.lower() != revision:
        errors.append(
            {
                "code": "REVISION_MISMATCH",
                "message": "patch artifact revision does not match the expected repository revision",
            }
        )

    all_no_change = (
        not content
        and not normalized_required
        and bool(normalized_no_change)
        and set(normalized_no_change) == set(normalized_rules)
    )
    if not content and not all_no_change:
        errors.append({"code": "EMPTY_PATCH", "message": "patch content is empty"})

    if all_no_change:
        applicable = True
        logs.append("No patch applicability check required for grounded no-change plan")

    if not errors and not all_no_change:
        try:
            patch_text = content.decode("utf-8")
        except UnicodeDecodeError:
            errors.append(
                {
                    "code": "MALFORMED_PATCH",
                    "message": "patch content must be valid UTF-8",
                }
            )
        else:
            request = ToolRequest(
                tool_ref=git_tool_ref
                or {"kind": "Tool", "name": "git", "version": "1.0.0"},
                input={
                    "operation": "check_patch",
                    "expectedRevision": revision,
                    "branch": working_branch,
                    "patch": patch_text,
                },
                caller=ToolCaller(kind="TaskExecution", id=task_execution_id),
                capabilities=("git.read",),
                timeout_ms=timeout_ms,
                correlation=context,
            )
            if tool_invocation_id is not None:
                if git_tool is None:
                    raise PatchEvaluationContractError(
                        "git_tool is required when tool_invocation_id is supplied"
                    )
                git_result, _ = git_tool.invoke(
                    invocation_id=tool_invocation_id,
                    task_execution_id=task_execution_id,
                    request=request,
                    authorize=authorize_git,
                )
            else:
                git_result = invoke_tool(
                    request,
                    validator=git_tool_validator(),
                    authorize=authorize_git,
                    adapter=git_adapter,
                )
            git_status = git_result.status.value
            git_logs_ref = git_result.logs_ref
            output = git_result.output_record() if git_result.output is not None else {}
            if git_result.status is ToolResultStatus.SUCCEEDED:
                applicable = output.get("applicable") is True
                diagnostics = list(output.get("diagnostics", []))
                changed_records = output.get("changedFiles", [])
                changed_files = sorted(
                    {
                        path
                        for record in changed_records
                        for path in (record.get("previousPath"), record.get("path"))
                        if isinstance(path, str)
                    },
                    key=lambda path: (path.casefold(), path),
                )
                if not applicable:
                    errors.append(
                        {
                            "code": "PATCH_NOT_APPLICABLE",
                            "message": "patch does not apply cleanly to the expected revision",
                        }
                    )
                if not changed_files:
                    errors.append(
                        {
                            "code": "MALFORMED_OR_EMPTY_PATCH",
                            "message": "patch contains no changed-file evidence",
                        }
                    )
            else:
                message = git_result.failure_message or "Git applicability check failed"
                diagnostics = [message]
                errors.append({"code": "GIT_CHECK_FAILED", "message": message})

    for path in changed_files:
        matching_rule = _matching_rule(path, normalized_rules)
        allowed = matching_rule is not None
        check: dict[str, Any] = {"path": path, "allowed": allowed}
        if matching_rule is not None:
            check["rule"] = matching_rule
        boundary_checks.append(check)
        if not allowed:
            errors.append(
                {
                    "code": "DISALLOWED_PATH",
                    "message": f"changed path {path!r} is outside the allowed path rules",
                }
            )

    added_blocks_by_path = _added_blocks_by_path(content)
    missing_insertions = sorted(
        (
            {"path": item["path"], "value": item["value"]}
            for item in required_insertions
            if not any(
                item["value"] in block
                for block in added_blocks_by_path.get(item["path"], ())
            )
        ),
        key=lambda item: (item["path"].casefold(), item["path"], item["value"]),
    )
    for insertion in missing_insertions:
        errors.append({
            "code": "REQUIRED_INSERTION_MISSING",
            "message": f"required insertion {insertion['value']!r} is absent from {insertion['path']!r}",
        })
    for criterion in sorted({value for value in unsupported_acceptance_criteria if isinstance(value, str) and value}):
        errors.append({"code": "UNSUPPORTED_ACCEPTANCE_CRITERION", "message": f"unsupported acceptance criterion: {criterion}"})

    dispositions = [
        {"path": path, "disposition": "CHANGED" if path in changed_files else "MISSING"}
        for path in normalized_required
    ]
    for disposition in dispositions:
        if disposition["disposition"] == "MISSING":
            errors.append({
                "code": "REQUIRED_CHANGE_MISSING",
                "message": f"planned target {disposition['path']!r} is absent from the final diff",
            })
    no_change_dispositions = [
        {"path": path, "disposition": "NO_CHANGE" if path not in changed_files else "CHANGED"}
        for path in normalized_no_change
    ]
    for disposition in no_change_dispositions:
        if disposition["disposition"] != "NO_CHANGE":
            errors.append({"code": "NO_CHANGE_TARGET_MODIFIED", "message": f"no-change target {disposition['path']!r} appears in the final diff"})
    added_lines, deleted_lines = _line_change_counts(content)
    total_changes = added_lines + deleted_lines
    replacement_ratio = deleted_lines / total_changes if total_changes else 0.0
    deleted_paths = _deleted_paths(content)
    unauthorized_deleted_paths = sorted(
        path for path in deleted_paths
        if _matching_rule(path, normalized_deletion_authorized) is None
    )
    for path in unauthorized_deleted_paths:
        errors.append({"code": "UNAUTHORIZED_DELETION", "message": f"deleted content in {path!r} is not deletion-authorized"})
    replacement_violations: list[str] = []
    destructive_candidate = deleted_lines >= 20 and replacement_ratio > 0.8
    deletion_authorized = bool(deleted_paths) and not unauthorized_deleted_paths
    destructive = destructive_candidate and not deletion_authorized
    if destructive:
        errors.append({
            "code": "DESTRUCTIVE_REWRITE",
            "message": "patch deletes at least 20 lines and more than 80% of changed lines",
        })
    unpreserved_hunks = _unpreserved_hunks(content)
    unauthorized_unpreserved_hunks = [
        hunk for hunk in unpreserved_hunks
        if _matching_rule(str(hunk["path"]), normalized_deletion_authorized) is None
    ]
    surrounding_content_preserved = not unauthorized_unpreserved_hunks
    if not surrounding_content_preserved:
        errors.append({
            "code": "SURROUNDING_CONTENT_NOT_PRESERVED",
            "message": "patch replaces or removes a multi-line hunk without preserved surrounding context",
        })

    errors = sorted(errors, key=lambda item: (item["code"], item["message"]))
    logs.extend(diagnostics)
    logs.append("Patch evaluation passed" if not errors else "Patch evaluation failed")
    checks = {
        "artifactIntegrity": not any(
            error["code"] in {"INVALID_ARTIFACT", "CONTENT_MISMATCH"} for error in errors
        ),
        "revision": not any(error["code"] == "REVISION_MISMATCH" for error in errors),
        "applicability": applicable,
        "pathBoundary": (
            all_no_change
            or (bool(changed_files) and all(check["allowed"] for check in boundary_checks))
        ),
        "requiredFileDisposition": all(
            item["disposition"] == "CHANGED" for item in dispositions
        ),
        "noChangeDisposition": all(item["disposition"] == "NO_CHANGE" for item in no_change_dispositions),
        "acceptanceCriteria": not unsupported_acceptance_criteria,
        "destructiveChange": not destructive,
        "surroundingContentPreservation": surrounding_content_preserved,
    }
    evidence = {
        "type": "patch-evaluation",
        "artifactId": artifact_id,
        "contentAddress": actual_address,
        "expectedRevision": revision,
        "artifactRevision": artifact_revision,
        "allowedPaths": list(normalized_rules),
        "changedFiles": changed_files,
        "applicable": applicable,
        "diagnostics": diagnostics,
        "boundaryChecks": boundary_checks,
        "requiredFileDispositions": dispositions,
        "noChangeFileDispositions": no_change_dispositions,
        "changeStatistics": {
            "addedLines": added_lines,
            "deletedLines": deleted_lines,
            "replacementRatio": replacement_ratio,
            "destructiveRewrite": destructive,
            "deletionAuthorized": deletion_authorized,
            "deletionAuthorizedPaths": list(normalized_deletion_authorized),
            "deletedPaths": deleted_paths,
            "unauthorizedDeletedPaths": unauthorized_deleted_paths,
            "requiredInsertions": list(required_insertions),
            "missingInsertions": missing_insertions,
            "replacementViolations": replacement_violations,
            "unsupportedAcceptanceCriteria": list(unsupported_acceptance_criteria),
            "unpreservedHunks": unpreserved_hunks,
            "unauthorizedUnpreservedHunks": unauthorized_unpreserved_hunks,
        },
        "git": {
            "status": git_status,
            "logsRef": git_logs_ref,
            "toolInvocationId": tool_invocation_id if git_status != "NOT_RUN" else None,
        },
        "checks": checks,
        "errors": errors,
    }
    evidence_json = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
    result = {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "EvaluationResult",
        "id": result_id,
        "traceId": context.trace_id,
        "createdAt": timestamp,
        "updatedAt": timestamp,
        "provenance": deepcopy(dict(provenance)),
        "taskExecutionId": task_execution_id,
        "evaluationRef": deepcopy(dict(evaluation_ref)),
        "target": {"type": "GeneratedArtifact", "id": artifact_id},
        "status": "SUCCEEDED",
        "outcome": "PASS" if not errors else "FAIL",
        "metrics": {
            "checks": len(checks),
            "passed": sum(checks.values()),
            "errors": len(errors),
            "changedFiles": len(changed_files),
        },
        "logs": logs,
        "evidence": evidence,
        "evidenceAddress": f"sha256:{sha256(evidence_json.encode()).hexdigest()}",
        "startedAt": timestamp,
        "completedAt": timestamp,
    }
    _validate_result(result)
    return store.create(
        result,
        deterministic_key=f"patch-evaluation:{result_id}:{result['evidenceAddress']}",
    )


def _patch_bytes(
    content: str | bytes, errors: list[dict[str, str]]
) -> bytes:
    if isinstance(content, str):
        return content.encode("utf-8")
    if isinstance(content, bytes):
        return content
    errors.append(
        {"code": "INVALID_CONTENT", "message": "patch content must be text or bytes"}
    )
    return b""


def _line_change_counts(content: bytes) -> tuple[int, int]:
    try:
        lines = content.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return 0, 0
    added = sum(line.startswith("+") and not line.startswith("+++") for line in lines)
    deleted = sum(line.startswith("-") and not line.startswith("---") for line in lines)
    return added, deleted


def _unpreserved_hunks(content: bytes) -> list[dict[str, int | str]]:
    """Identify replacements that remove all or nearly all source hunk lines."""

    hunks: list[dict[str, int | str]] = []
    current: dict[str, int | str] | None = None
    path: str | None = None
    for line in content.decode("utf-8", errors="replace").splitlines():
        marker = _patch_marker_path(line, "--- ")
        if marker is not None and marker != "/dev/null":
            path = marker
        elif line.startswith("@@ "):
            if current is not None and _hunk_is_unpreserved(current):
                hunks.append(current)
            match = re.match(r"^@@ -\d+(?:,(\d+))? ", line)
            source_lines = int(match.group(1) or "1") if match else 0
            current = {"path": path or "", "deletedLines": 0, "addedLines": 0, "contextLines": 0, "sourceLines": source_lines}
        elif current is not None:
            if line.startswith("-") and not line.startswith("---"):
                current["deletedLines"] += 1
            elif line.startswith("+") and not line.startswith("+++"):
                current["addedLines"] += 1
            elif line.startswith(" "):
                current["contextLines"] += 1
    if current is not None and _hunk_is_unpreserved(current):
        hunks.append(current)
    return hunks


def _hunk_is_unpreserved(hunk: Mapping[str, int | str]) -> bool:
    deleted = int(hunk["deletedLines"])
    context = int(hunk["contextLines"])
    source = int(hunk["sourceLines"])
    return deleted >= 2 and (
        context == 0 or (deleted >= 5 and source > 0 and deleted / source >= 0.8)
    )


def _deleted_paths(content: bytes) -> list[str]:
    """Return files whose hunks remove more lines than they add."""

    current: str | None = None
    deleted = added = 0
    paths: set[str] = set()

    def complete_hunk() -> None:
        if current is not None and deleted > added:
            paths.add(current)

    for line in content.decode("utf-8", errors="replace").splitlines():
        marker = _patch_marker_path(line, "--- ")
        if marker is not None:
            complete_hunk()
            current = None if marker == "/dev/null" else marker
            deleted = added = 0
        elif line.startswith("@@ "):
            complete_hunk()
            deleted = added = 0
        elif current is not None and line.startswith("-") and not line.startswith("---"):
            deleted += 1
        elif current is not None and line.startswith("+") and not line.startswith("+++"):
            added += 1
    complete_hunk()
    return sorted(paths, key=lambda value: (value.casefold(), value))


def _added_text(content: bytes) -> tuple[str, ...]:
    try:
        lines = content.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return ()
    return tuple(line[1:] for line in lines if line.startswith("+") and not line.startswith("+++"))


def _added_text_by_path(content: bytes) -> dict[str, tuple[str, ...]]:
    current: str | None = None
    values: dict[str, list[str]] = {}
    for line in content.decode("utf-8", errors="replace").splitlines():
        marker = _patch_marker_path(line, "+++ ")
        if marker is not None:
            current = None if marker == "/dev/null" else marker
        elif current is not None and line.startswith("+") and not line.startswith("+++"):
            values.setdefault(current, []).append(line[1:])
    return {path: tuple(lines) for path, lines in values.items()}


def _added_blocks_by_path(content: bytes) -> dict[str, tuple[str, ...]]:
    current: str | None = None
    active: list[str] = []
    values: dict[str, list[str]] = {}

    def finish() -> None:
        if current is not None and active:
            values.setdefault(current, []).append("\n".join(active))
            active.clear()

    for line in content.decode("utf-8", errors="replace").splitlines():
        marker = _patch_marker_path(line, "+++ ")
        if marker is not None:
            finish()
            current = None if marker == "/dev/null" else marker
        elif line.startswith("@@ "):
            finish()
        elif current is not None and line.startswith("+") and not line.startswith("+++"):
            active.append(line[1:])
        else:
            finish()
    finish()
    return {path: tuple(blocks) for path, blocks in values.items()}


def _replaced_paths(content: bytes) -> set[str]:
    current: str | None = None
    deleted = added = 0
    replaced: set[str] = set()
    def finish() -> None:
        if current is not None and deleted and added:
            replaced.add(current)
    for line in content.decode("utf-8", errors="replace").splitlines():
        marker = _patch_marker_path(line, "--- ")
        if marker is not None:
            finish()
            current = None if marker == "/dev/null" else marker
            deleted = added = 0
        elif line.startswith("@@ "):
            finish()
            deleted = added = 0
        elif line.startswith("-") and not line.startswith("---"):
            deleted += 1
        elif line.startswith("+") and not line.startswith("+++"):
            added += 1
    finish()
    return replaced


def _patch_marker_path(line: str, marker: str) -> str | None:
    if not line.startswith(marker):
        return None
    raw = line[len(marker):].encode("utf-8")
    path = _decode_patch_path(raw)
    if path == "/dev/null":
        return path
    if path.startswith(("a/", "b/")):
        return path[2:]
    return None


def _normalize_rules(rules: Sequence[str]) -> tuple[str, ...]:
    if isinstance(rules, (str, bytes)) or not rules:
        raise PatchEvaluationContractError("allowed_paths must contain path rules")
    normalized: set[str] = set()
    for rule in rules:
        candidate = rule.rstrip("/") if isinstance(rule, str) else ""
        if not _safe_relative_path(candidate):
            raise PatchEvaluationContractError(
                "allowed path rules must be normalized repository-relative paths"
            )
        normalized.add(candidate)
    return tuple(sorted(normalized, key=lambda value: (value.casefold(), value)))


def _matching_rule(path: str, rules: Sequence[str]) -> str | None:
    if not _safe_relative_path(path):
        return None
    return next(
        (rule for rule in rules if path == rule or path.startswith(f"{rule}/")),
        None,
    )


def _safe_relative_path(value: str) -> bool:
    if not value or "\\" in value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and all(part not in {"", ".", ".."} for part in path.parts)


def _validate_result(result: Mapping[str, Any]) -> None:
    errors = sorted(
        _evaluation_result_validator().iter_errors(dict(result)),
        key=lambda error: (list(error.absolute_path), error.message),
    )
    if errors:
        error = errors[0]
        path = "$" + "".join(
            f"[{part}]" if isinstance(part, int) else f".{part}"
            for part in error.absolute_path
        )
        raise PatchEvaluationContractError(
            f"invalid EvaluationResult at {path}: {error.message}"
        )


@cache
def _evaluation_result_validator() -> Draft202012Validator:
    schema_root = Path(__file__).parents[2] / "schemas"
    schema_paths = (
        schema_root / "resources" / "v1" / "resource-definitions.schema.json",
        schema_root / "runtime" / "v1" / "runtime-definitions.schema.json",
        schema_root / "runtime" / "v1" / "evaluationresult.schema.json",
    )
    schemas = [json.loads(path.read_text(encoding="utf-8")) for path in schema_paths]
    registry = Registry()
    for schema in schemas:
        registry = registry.with_resource(
            schema["$id"],
            SchemaResource.from_contents(schema, default_specification=DRAFT202012),
        )
    return Draft202012Validator(schemas[-1], registry=registry)
