"""Workspace-confined Filesystem Tool adapter and invocation evidence."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path
from time import monotonic
from typing import Any

from aep.runtime_store import RuntimeObject, RuntimeObjectStore
from aep.tool_runtime import (
    AuthorizationHook,
    JsonSchemaToolValidator,
    ToolAdapter,
    ToolExecution,
    ToolFailureClass,
    ToolMetrics,
    ToolRequest,
    ToolResult,
    ToolResultStatus,
    invoke_tool,
)


FILESYSTEM_INPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "oneOf": [
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["operation", "path"],
            "properties": {
                "operation": {"const": "read"},
                "path": {"type": "string", "minLength": 1, "maxLength": 4096},
            },
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["operation", "path", "content"],
            "properties": {
                "operation": {"const": "write"},
                "path": {"type": "string", "minLength": 1, "maxLength": 4096},
                "content": {"type": "string"},
            },
        },
    ],
}

FILESYSTEM_OUTPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "oneOf": [
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["operation", "path", "content", "sizeBytes", "sha256"],
            "properties": {
                "operation": {"const": "read"},
                "path": {"type": "string"},
                "content": {"type": "string"},
                "sizeBytes": {"type": "integer", "minimum": 0},
                "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            },
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["operation", "path", "bytesWritten", "sha256"],
            "properties": {
                "operation": {"const": "write"},
                "path": {"type": "string"},
                "bytesWritten": {"type": "integer", "minimum": 0},
                "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            },
        },
    ],
}


class FilesystemBoundaryError(ValueError):
    """Raised when a requested path is not confined to the workspace."""


class _CompletedExecution(ToolExecution):
    def __init__(self, result: ToolResult) -> None:
        self._result = result

    def wait(self, timeout_ms: int) -> ToolResult:
        return self._result

    def terminate(self) -> None:
        return None

    def kill(self) -> None:
        return None

    def cleanup(self) -> None:
        return None


class FilesystemToolAdapter(ToolAdapter):
    """Execute UTF-8 file reads and writes inside one configured workspace."""

    def __init__(self, workspace: Path | str) -> None:
        candidate = Path(workspace)
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as error:
            raise ValueError(f"workspace does not exist: {candidate}") from error
        if not resolved.is_dir():
            raise ValueError(f"workspace is not a directory: {resolved}")
        self._workspace = resolved
        self._logs: dict[str, Mapping[str, Any]] = {}

    @property
    def workspace(self) -> Path:
        return self._workspace

    def get_log(self, logs_ref: str) -> Mapping[str, Any] | None:
        value = self._logs.get(logs_ref)
        return dict(value) if value is not None else None

    def start(self, request: ToolRequest) -> ToolExecution:
        started_at = datetime.now(UTC)
        started_clock = monotonic()
        operation = request.input["operation"]
        relative_path = request.input["path"]

        if operation == "write" and "filesystem.write" not in request.capabilities:
            return _CompletedExecution(
                self._failure(
                    started_at,
                    started_clock,
                    operation,
                    relative_path,
                    ToolFailureClass.POLICY,
                    "write requires the filesystem.write capability",
                )
            )
        if operation == "read" and "filesystem.read" not in request.capabilities:
            return _CompletedExecution(
                self._failure(
                    started_at,
                    started_clock,
                    operation,
                    relative_path,
                    ToolFailureClass.POLICY,
                    "read requires the filesystem.read capability",
                )
            )

        try:
            target, normalized_path = self._resolve(relative_path, operation)
            if operation == "read":
                content = target.read_text(encoding="utf-8")
                encoded = content.encode("utf-8")
                output = {
                    "operation": "read",
                    "path": normalized_path,
                    "content": content,
                    "sizeBytes": len(encoded),
                    "sha256": sha256(encoded).hexdigest(),
                }
            else:
                content = request.input["content"]
                encoded = content.encode("utf-8")
                target.write_text(content, encoding="utf-8", newline="")
                output = {
                    "operation": "write",
                    "path": normalized_path,
                    "bytesWritten": len(encoded),
                    "sha256": sha256(encoded).hexdigest(),
                }
        except FilesystemBoundaryError as error:
            result = self._failure(
                started_at,
                started_clock,
                operation,
                relative_path,
                ToolFailureClass.BOUNDARY,
                str(error),
            )
        except FileNotFoundError:
            result = self._failure(
                started_at,
                started_clock,
                operation,
                relative_path,
                ToolFailureClass.NOT_FOUND,
                f"file was not found: {relative_path}",
            )
        except (OSError, UnicodeError) as error:
            result = self._failure(
                started_at,
                started_clock,
                operation,
                relative_path,
                ToolFailureClass.IO,
                f"I/O failed for {relative_path}: {type(error).__name__}",
            )
        else:
            completed_at = datetime.now(UTC)
            logs_ref = self._record_log(
                {
                    "operation": operation,
                    "path": normalized_path,
                    "status": "SUCCEEDED",
                }
            )
            result = ToolResult(
                status=ToolResultStatus.SUCCEEDED,
                output=output,
                logs_ref=logs_ref,
                metrics=ToolMetrics(
                    duration_ms=max(0, round((monotonic() - started_clock) * 1000))
                ),
                started_at=_timestamp(started_at),
                completed_at=_timestamp(completed_at),
            )
        return _CompletedExecution(result)

    def _resolve(self, raw_path: str, operation: str) -> tuple[Path, str]:
        relative = Path(raw_path)
        if relative.anchor or any(part == ".." for part in relative.parts):
            raise FilesystemBoundaryError(
                f"path must be workspace-relative without traversal: {raw_path}"
            )

        candidate = self._workspace.joinpath(relative)
        try:
            if operation == "read" or candidate.exists() or candidate.is_symlink():
                resolved = candidate.resolve(strict=True)
            else:
                resolved = candidate.parent.resolve(strict=True) / candidate.name
        except FileNotFoundError:
            if candidate.is_symlink():
                raise FilesystemBoundaryError(
                    f"symlink target is outside or unavailable: {raw_path}"
                ) from None
            raise

        if not resolved.is_relative_to(self._workspace):
            raise FilesystemBoundaryError(
                f"path resolves outside configured workspace: {raw_path}"
            )
        return resolved, resolved.relative_to(self._workspace).as_posix()

    def _failure(
        self,
        started_at: datetime,
        started_clock: float,
        operation: str,
        path: str,
        failure_class: ToolFailureClass,
        message: str,
    ) -> ToolResult:
        completed_at = datetime.now(UTC)
        logs_ref = self._record_log(
            {
                "operation": operation,
                "path": path,
                "status": "FAILED",
                "failureClass": failure_class.value,
                "message": message,
            }
        )
        return ToolResult(
            status=ToolResultStatus.FAILED,
            output=None,
            logs_ref=logs_ref,
            metrics=ToolMetrics(
                duration_ms=max(0, round((monotonic() - started_clock) * 1000))
            ),
            started_at=_timestamp(started_at),
            completed_at=_timestamp(completed_at),
            failure_class=failure_class,
            failure_message=message,
        )

    def _record_log(self, value: Mapping[str, Any]) -> str:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        address = f"sha256:{sha256(payload).hexdigest()}"
        self._logs[address] = dict(value)
        return address


class FilesystemTool:
    """Run the adapter through Tool Runtime and persist ToolInvocation evidence."""

    def __init__(self, workspace: Path | str, store: RuntimeObjectStore) -> None:
        self.adapter = FilesystemToolAdapter(workspace)
        self._store = store
        self._validator = JsonSchemaToolValidator(
            FILESYSTEM_INPUT_SCHEMA, FILESYSTEM_OUTPUT_SCHEMA
        )

    def invoke(
        self,
        *,
        invocation_id: str,
        task_execution_id: str,
        request: ToolRequest,
        authorize: AuthorizationHook,
        policy_decision_id: str | None = None,
    ) -> tuple[ToolResult, RuntimeObject]:
        result = invoke_tool(
            request,
            validator=self._validator,
            authorize=authorize,
            adapter=self.adapter,
        )
        record = _invocation_record(
            invocation_id,
            task_execution_id,
            request,
            result,
            policy_decision_id,
        )
        persisted = self._store.create(
            record, deterministic_key=f"filesystem-tool-invocation:{invocation_id}"
        )
        return result, persisted


def _invocation_record(
    invocation_id: str,
    task_execution_id: str,
    request: ToolRequest,
    result: ToolResult,
    policy_decision_id: str | None,
) -> dict[str, Any]:
    status = "SUCCEEDED" if result.status is ToolResultStatus.SUCCEEDED else "FAILED"
    record: dict[str, Any] = {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "ToolInvocation",
        "id": invocation_id,
        "traceId": request.trace_id,
        "createdAt": result.started_at,
        "updatedAt": result.completed_at,
        "provenance": {
            "actor": "tool-runtime",
            "caller": f"{request.caller.kind}:{request.caller.id}",
            "taskExecutionId": task_execution_id,
            "resourceRefs": [dict(request.tool_ref)],
        },
        "taskExecutionId": task_execution_id,
        "toolRef": dict(request.tool_ref),
        "status": status,
        "input": _json_copy(request.input),
        "capabilities": list(request.capabilities),
        "output": result.output_record(),
        "metrics": result.metrics.as_record(),
        "startedAt": result.started_at,
        "completedAt": result.completed_at,
    }
    if request.caller.kind == "AgentInvocation":
        record["agentInvocationId"] = request.caller.id
    if policy_decision_id is not None:
        record["policyDecisionId"] = policy_decision_id
    if result.logs_ref is not None:
        record["logsAddress"] = result.logs_ref
    if result.failure_class is not None:
        record["failure"] = {
            "class": _runtime_failure_class(result.failure_class),
            "message": result.failure_message or result.failure_class.value,
            "retryable": result.failure_class
            in {ToolFailureClass.IO, ToolFailureClass.TIMEOUT},
        }
        record["failureClass"] = result.failure_class.value
    return record


def _runtime_failure_class(value: ToolFailureClass) -> str:
    return {
        ToolFailureClass.VALIDATION: "CONFIGURATION",
        ToolFailureClass.POLICY: "POLICY",
        ToolFailureClass.TIMEOUT: "RECOVERABLE",
        ToolFailureClass.ADAPTER: "PERMANENT",
        ToolFailureClass.BOUNDARY: "POLICY",
        ToolFailureClass.NOT_FOUND: "PERMANENT",
        ToolFailureClass.IO: "RECOVERABLE",
    }[value]


def _json_copy(value: Mapping[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(dict(value)))


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")
