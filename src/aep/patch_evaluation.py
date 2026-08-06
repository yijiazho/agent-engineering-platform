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

from aep.git_tool import GitToolAdapter, git_tool_validator
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
    working_branch: str,
    trace_id: str,
    timestamp: str,
    provenance: Mapping[str, Any],
    git_tool_ref: Mapping[str, Any] | None = None,
    timeout_ms: int = 5_000,
) -> RuntimeObject:
    """Evaluate a patch without applying it and persist immutable evidence."""

    artifact = deepcopy(dict(patch_artifact))
    normalized_rules = _normalize_rules(allowed_paths)
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

    if not content:
        errors.append({"code": "EMPTY_PATCH", "message": "patch content is empty"})

    if not errors:
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
                trace_id=trace_id,
            )
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

    errors = sorted(errors, key=lambda item: (item["code"], item["message"]))
    logs.extend(diagnostics)
    logs.append("Patch evaluation passed" if not errors else "Patch evaluation failed")
    checks = {
        "artifactIntegrity": not any(
            error["code"] in {"INVALID_ARTIFACT", "CONTENT_MISMATCH"} for error in errors
        ),
        "revision": not any(error["code"] == "REVISION_MISMATCH" for error in errors),
        "applicability": applicable,
        "pathBoundary": bool(changed_files) and all(
            check["allowed"] for check in boundary_checks
        ),
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
        "git": {"status": git_status, "logsRef": git_logs_ref},
        "checks": checks,
        "errors": errors,
    }
    evidence_json = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
    result = {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "EvaluationResult",
        "id": result_id,
        "traceId": trace_id,
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
