"""Workspace-confined Filesystem Tool adapter and invocation evidence."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
import errno
from hashlib import sha256
import json
import os
from pathlib import Path
from time import monotonic, sleep
from typing import Any, BinaryIO, Callable

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

TRUSTED_REPOSITORY_READ_CALLERS = frozenset(
    {"ContextBuilder", "TaskExecution", "WorkflowRuntime"}
)
INVOCATION_REPLAY_WAIT_SECONDS = 10.0


class FilesystemBoundaryError(ValueError):
    """Raised when a requested path is not confined to the workspace."""


class FilesystemInvocationIdentityConflictError(ValueError):
    """Raised when an invocation id is reused for different immutable inputs."""


class FilesystemInvocationInProgressError(RuntimeError):
    """Raised when an identical invocation does not reach terminal state in time."""


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

    def __init__(
        self,
        workspace: Path | str,
        *,
        handle_path_resolver: Callable[[int], Path] | None = None,
        before_open: Callable[[Path, str], None] | None = None,
    ) -> None:
        candidate = Path(workspace)
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as error:
            raise ValueError(f"workspace does not exist: {candidate}") from error
        if not resolved.is_dir():
            raise ValueError(f"workspace is not a directory: {resolved}")
        self._workspace = resolved
        self._logs: dict[str, Mapping[str, Any]] = {}
        self._handle_path_resolver = handle_path_resolver or _open_handle_path
        self._before_open = before_open

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
        if (
            operation == "read"
            and request.caller.kind not in TRUSTED_REPOSITORY_READ_CALLERS
        ):
            return _CompletedExecution(
                self._failure(
                    started_at,
                    started_clock,
                    operation,
                    relative_path,
                    ToolFailureClass.POLICY,
                    "repository reads are restricted to trusted Context Builder "
                    "or control-plane callers",
                )
            )

        try:
            relative, normalized_path = self._validate_path(relative_path)
            if self._before_open is not None:
                self._before_open(self._workspace / relative, operation)
            if operation == "read":
                with self._open_confined(relative, operation) as stream:
                    content = stream.read().decode("utf-8")
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
                with self._open_confined(relative, operation) as stream:
                    stream.seek(0)
                    stream.truncate(0)
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
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

    def _validate_path(self, raw_path: str) -> tuple[Path, str]:
        relative = Path(raw_path)
        if (
            relative.anchor
            or not relative.parts
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise FilesystemBoundaryError(
                f"path must be workspace-relative without traversal: {raw_path}"
            )
        return relative, relative.as_posix()

    def _open_confined(self, relative: Path, operation: str) -> BinaryIO:
        try:
            if os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"):
                descriptor = self._open_from_pinned_workspace(relative, operation)
            else:
                descriptor = self._open_and_verify_handle(relative, operation)
        except OSError as error:
            if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise FilesystemBoundaryError(
                    f"path contains a symlink or non-directory component: {relative}"
                ) from None
            raise
        return os.fdopen(descriptor, "r+b" if operation == "write" else "rb")

    def _open_from_pinned_workspace(self, relative: Path, operation: str) -> int:
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        root_descriptor = os.open(self._workspace, directory_flags)
        directory_descriptor = root_descriptor
        try:
            for part in relative.parts[:-1]:
                child = os.open(
                    part,
                    directory_flags,
                    dir_fd=directory_descriptor,
                )
                if directory_descriptor != root_descriptor:
                    os.close(directory_descriptor)
                directory_descriptor = child
            flags = os.O_NOFOLLOW
            if operation == "read":
                flags |= os.O_RDONLY
            else:
                flags |= os.O_RDWR | os.O_CREAT
            descriptor = os.open(
                relative.name,
                flags,
                0o600,
                dir_fd=directory_descriptor,
            )
            self._verify_open_handle(descriptor)
            return descriptor
        finally:
            if directory_descriptor != root_descriptor:
                os.close(directory_descriptor)
            os.close(root_descriptor)

    def _open_and_verify_handle(self, relative: Path, operation: str) -> int:
        candidate = self._workspace / relative
        flags = os.O_RDONLY if operation == "read" else os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_BINARY", 0)
        descriptor = os.open(candidate, flags, 0o600)
        try:
            self._verify_open_handle(descriptor)
        except Exception:
            os.close(descriptor)
            raise
        return descriptor

    def _verify_open_handle(self, descriptor: int) -> None:
        opened_path = self._handle_path_resolver(descriptor)
        if not opened_path.is_relative_to(self._workspace):
            raise FilesystemBoundaryError(
                "opened file handle resolves outside configured workspace"
            )

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
        fingerprint = _request_fingerprint(
            task_execution_id, request, policy_decision_id
        )
        claimed, claim = self._store.claim(
            f"filesystem-tool-invocation-claim:{invocation_id}",
            {
                "invocationId": invocation_id,
                "requestFingerprint": fingerprint,
            },
        )
        if claim["requestFingerprint"] != fingerprint:
            raise FilesystemInvocationIdentityConflictError(
                f"invocation id {invocation_id!r} is already bound to different "
                "immutable request inputs"
            )
        if not claimed:
            prior = self._await_terminal_invocation(invocation_id)
            return _result_from_invocation(prior), prior

        started_at = _timestamp(datetime.now(UTC))
        pending = _pending_invocation_record(
            invocation_id,
            task_execution_id,
            request,
            fingerprint,
            policy_decision_id,
            started_at,
        )
        created = self._store.create(
            pending,
            deterministic_key=f"filesystem-tool-invocation:{invocation_id}",
        )
        if created["requestFingerprint"] != fingerprint:
            raise FilesystemInvocationIdentityConflictError(
                f"invocation id {invocation_id!r} conflicts with existing evidence"
            )
        try:
            result = invoke_tool(
                request,
                validator=self._validator,
                authorize=authorize,
                adapter=self.adapter,
            )
        except Exception as error:
            result = _unexpected_failure(started_at, error)
        status = (
            "SUCCEEDED"
            if result.status is ToolResultStatus.SUCCEEDED
            else "FAILED"
        )
        persisted = self._store.update_status(
            invocation_id,
            status,
            expected_status="PENDING",
            updated_at=result.completed_at,
            changes=_terminal_invocation_changes(result),
        )
        return result, persisted

    def _await_terminal_invocation(self, invocation_id: str) -> RuntimeObject:
        deadline = monotonic() + INVOCATION_REPLAY_WAIT_SECONDS
        while monotonic() < deadline:
            prior = self._store.get(invocation_id)
            if prior is not None and prior.get("status") in {"SUCCEEDED", "FAILED"}:
                return prior
            sleep(0.001)
        raise FilesystemInvocationInProgressError(
            f"identical invocation {invocation_id!r} is still in progress"
        )


def _pending_invocation_record(
    invocation_id: str,
    task_execution_id: str,
    request: ToolRequest,
    request_fingerprint: str,
    policy_decision_id: str | None,
    timestamp: str,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "ToolInvocation",
        "id": invocation_id,
        "traceId": request.trace_id,
        "createdAt": timestamp,
        "updatedAt": timestamp,
        "provenance": {
            "actor": "tool-runtime",
            "caller": f"{request.caller.kind}:{request.caller.id}",
            "taskExecutionId": task_execution_id,
            "resourceRefs": [dict(request.tool_ref)],
        },
        "taskExecutionId": task_execution_id,
        "toolRef": dict(request.tool_ref),
        "status": "PENDING",
        "input": _json_copy(request.input),
        "capabilities": list(request.capabilities),
        "requestFingerprint": request_fingerprint,
    }
    if request.caller.kind == "AgentInvocation":
        record["agentInvocationId"] = request.caller.id
    if policy_decision_id is not None:
        record["policyDecisionId"] = policy_decision_id
    return record


def _terminal_invocation_changes(result: ToolResult) -> dict[str, Any]:
    changes: dict[str, Any] = {
        "resultStatus": result.status.value,
        "output": result.output_record(),
        "metrics": result.metrics.as_record(),
        "startedAt": result.started_at,
    }
    if result.logs_ref is not None:
        changes["logsAddress"] = result.logs_ref
    if result.failure_class is not None:
        changes["failure"] = {
            "class": _runtime_failure_class(result.failure_class),
            "message": result.failure_message or result.failure_class.value,
            "retryable": result.failure_class
            in {ToolFailureClass.IO, ToolFailureClass.TIMEOUT},
        }
        changes["failureClass"] = result.failure_class.value
    return changes


def _result_from_invocation(invocation: RuntimeObject) -> ToolResult:
    failure_class = invocation.get("failureClass")
    failure = invocation.get("failure")
    return ToolResult(
        status=ToolResultStatus(invocation["resultStatus"]),
        output=invocation.get("output"),
        logs_ref=invocation.get("logsAddress"),
        metrics=ToolMetrics(**_metric_arguments(invocation.get("metrics", {}))),
        started_at=invocation["startedAt"],
        completed_at=invocation["completedAt"],
        failure_class=(
            ToolFailureClass(failure_class) if failure_class is not None else None
        ),
        failure_message=(
            failure.get("message") if isinstance(failure, Mapping) else None
        ),
    )


def _metric_arguments(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "duration_ms": value.get("durationMs", 0),
        "cpu_ms": value.get("cpuMs"),
        "memory_bytes": value.get("memoryBytes"),
    }


def _request_fingerprint(
    task_execution_id: str,
    request: ToolRequest,
    policy_decision_id: str | None,
) -> str:
    value = {
        "taskExecutionId": task_execution_id,
        "toolRef": _json_copy(request.tool_ref),
        "input": _json_copy(request.input),
        "caller": request.caller.as_record(),
        "capabilities": list(request.capabilities),
        "timeoutMs": request.timeout_ms,
        "traceId": request.trace_id,
        "policyDecisionId": policy_decision_id,
    }
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return f"sha256:{sha256(canonical.encode()).hexdigest()}"


def _unexpected_failure(started_at: str, error: Exception) -> ToolResult:
    completed_at = _timestamp(datetime.now(UTC))
    return ToolResult(
        status=ToolResultStatus.FAILED,
        output=None,
        logs_ref=None,
        metrics=ToolMetrics(duration_ms=0),
        started_at=started_at,
        completed_at=completed_at,
        failure_class=ToolFailureClass.ADAPTER,
        failure_message=str(error) or type(error).__name__,
    )


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


def _open_handle_path(descriptor: int) -> Path:
    if os.name == "nt":
        import ctypes
        import msvcrt

        handle = msvcrt.get_osfhandle(descriptor)
        buffer = ctypes.create_unicode_buffer(32768)
        length = ctypes.windll.kernel32.GetFinalPathNameByHandleW(
            handle, buffer, len(buffer), 0
        )
        if length == 0 or length >= len(buffer):
            raise OSError("could not resolve opened filesystem handle")
        value = buffer.value
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
        return Path(value).resolve(strict=True)

    proc_path = Path(f"/proc/self/fd/{descriptor}")
    if proc_path.exists():
        return proc_path.resolve(strict=True)

    import fcntl

    path_buffer = bytearray(4096)
    fcntl.fcntl(descriptor, 50, path_buffer)
    value = bytes(path_buffer).split(b"\0", 1)[0]
    return Path(os.fsdecode(value)).resolve(strict=True)


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")
