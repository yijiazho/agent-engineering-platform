"""Controlled Git Tool adapter for one execution-specific working branch."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
from time import monotonic
from typing import Any, Protocol

from aep.tool_runtime import (
    JsonSchemaToolValidator,
    ToolAdapter,
    ToolExecution,
    ToolFailureClass,
    ToolMetrics,
    ToolRequest,
    ToolResult,
    ToolResultStatus,
)


GIT_INPUT_SCHEMA: Mapping[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["operation", "expectedRevision", "branch"],
    "properties": {
        "operation": {
            "type": "string",
            "enum": ["create_branch", "status", "diff", "push_branch"],
        },
        "expectedRevision": {"type": "string", "pattern": "^[0-9a-fA-F]{40}$"},
        "branch": {"type": "string", "minLength": 1},
    },
    "additionalProperties": False,
}

_COMMAND_RESULT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "required": ["arguments", "exitCode", "stdoutBytes", "stderrBytes"],
    "properties": {
        "arguments": {"type": "array", "items": {"type": "string"}},
        "exitCode": {"type": "integer"},
        "stdoutBytes": {"type": "integer", "minimum": 0},
        "stderrBytes": {"type": "integer", "minimum": 0},
    },
    "additionalProperties": False,
}

_CHANGED_FILE_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "required": ["path", "status"],
    "properties": {
        "path": {"type": "string"},
        "status": {"type": "string"},
        "previousPath": {"type": "string"},
        "indexStatus": {"type": "string"},
        "worktreeStatus": {"type": "string"},
    },
    "additionalProperties": False,
}

GIT_OUTPUT_SCHEMA: Mapping[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": [
        "operation",
        "repository",
        "branch",
        "revision",
        "baseRevision",
        "changedFiles",
        "diff",
        "pushed",
        "commandResults",
    ],
    "properties": {
        "operation": {"type": "string"},
        "repository": {"type": "string"},
        "branch": {"type": "string"},
        "revision": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
        "baseRevision": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
        "changedFiles": {
            "type": "array",
            "items": _CHANGED_FILE_SCHEMA,
        },
        "diff": {
            "oneOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "required": ["text", "sha256", "byteLength"],
                    "properties": {
                        "text": {"type": "string"},
                        "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                        "byteLength": {"type": "integer", "minimum": 0},
                    },
                    "additionalProperties": False,
                },
            ]
        },
        "pushed": {"type": "boolean"},
        "commandResults": {
            "type": "array",
            "items": _COMMAND_RESULT_SCHEMA,
        },
    },
    "additionalProperties": False,
}

_REVISION_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")
_SAFE_REMOTE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_TOKEN_PATTERN = re.compile(
    r"(?i)(?:gh[opsu]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|"
    r"(?:token|password|passwd|authorization)\s*[=:]\s*)[^\s\"']+"
)
_CREDENTIAL_URL_PATTERN = re.compile(
    r"(?i)\b(https?://)(?:[^/@\s]+)@"
)


class GitToolContractError(ValueError):
    """Raised when adapter configuration cannot enforce repository boundaries."""


class GitCommandLogStore(Protocol):
    """Persistence boundary for redacted Git command evidence."""

    def put(self, *, key: str, content: str) -> str:
        """Persist content and return an immutable reference."""


class InMemoryGitCommandLogStore:
    """Deterministic command log store for local composition and tests."""

    def __init__(self) -> None:
        self._records: dict[str, str] = {}

    def put(self, *, key: str, content: str) -> str:
        digest = sha256(content.encode("utf-8")).hexdigest()
        reference = f"memory://git-logs/{key}/{digest}"
        existing = self._records.get(reference)
        if existing is not None and existing != content:
            raise RuntimeError(f"log reference collision for {reference}")
        self._records[reference] = content
        return reference

    def get(self, reference: str) -> str:
        return self._records[reference]


@dataclass(frozen=True)
class _CommandResult:
    arguments: tuple[str, ...]
    exit_code: int
    stdout: bytes
    stderr: bytes

    def metadata(self) -> dict[str, Any]:
        return {
            "arguments": [_redact(value) for value in self.arguments],
            "exitCode": self.exit_code,
            "stdoutBytes": len(self.stdout),
            "stderrBytes": len(self.stderr),
        }

    def log_record(self) -> dict[str, Any]:
        return {
            **self.metadata(),
            "stdout": _redact(self.stdout.decode("utf-8", errors="replace")),
            "stderr": _redact(self.stderr.decode("utf-8", errors="replace")),
        }


class _CommandFailure(RuntimeError):
    def __init__(self, result: _CommandResult) -> None:
        self.result = result
        message = result.stderr.decode("utf-8", errors="replace").strip()
        super().__init__(_redact(message or "git command failed"))


class _CommandTimeout(TimeoutError):
    pass


class GitToolAdapter(ToolAdapter):
    """Execute bounded Git operations inside one configured repository."""

    def __init__(
        self,
        *,
        repository: Path,
        repository_id: str,
        expected_revision: str,
        working_branch: str,
        log_store: GitCommandLogStore,
        remote: str = "origin",
    ) -> None:
        root = repository.resolve()
        if not root.is_dir():
            raise GitToolContractError("configured repository must be a directory")
        if not (root / ".git").exists():
            raise GitToolContractError("configured repository must be a Git worktree")
        if not repository_id:
            raise GitToolContractError("repository_id must not be empty")
        if not _REVISION_PATTERN.fullmatch(expected_revision):
            raise GitToolContractError(
                "expected_revision must be an immutable 40-character commit id"
            )
        if not _valid_branch(working_branch):
            raise GitToolContractError("working_branch is not a safe Git branch name")
        if not _SAFE_REMOTE_PATTERN.fullmatch(remote):
            raise GitToolContractError("remote must be a configured remote name")
        self._repository = root
        self._repository_id = repository_id
        self._expected_revision = expected_revision.lower()
        self._working_branch = working_branch
        self._remote = remote
        self._log_store = log_store

    def start(self, request: ToolRequest) -> ToolExecution:
        value = request.input
        if request.tool_ref.get("name") != "git":
            raise GitToolContractError("GitToolAdapter accepts only the git Tool")
        if value.get("expectedRevision", "").lower() != self._expected_revision:
            raise GitToolContractError(
                "request expectedRevision does not match the configured revision"
            )
        if value.get("branch") != self._working_branch:
            raise GitToolContractError(
                "request branch does not match the configured working branch"
            )
        operation = value.get("operation")
        if operation == "push_branch" and "git.push" not in request.capabilities:
            raise GitToolContractError("push_branch requires the git.push capability")
        return _GitToolExecution(
            repository=self._repository,
            repository_id=self._repository_id,
            expected_revision=self._expected_revision,
            working_branch=self._working_branch,
            remote=self._remote,
            operation=str(operation),
            trace_id=request.trace_id,
            log_store=self._log_store,
        )


class _GitToolExecution(ToolExecution):
    def __init__(
        self,
        *,
        repository: Path,
        repository_id: str,
        expected_revision: str,
        working_branch: str,
        remote: str,
        operation: str,
        trace_id: str,
        log_store: GitCommandLogStore,
    ) -> None:
        self._repository = repository
        self._repository_id = repository_id
        self._expected_revision = expected_revision
        self._working_branch = working_branch
        self._remote = remote
        self._operation = operation
        self._trace_id = trace_id
        self._log_store = log_store
        self._commands: list[_CommandResult] = []
        self._result: ToolResult | None = None
        self._cancelled = False

    def wait(self, timeout_ms: int) -> ToolResult | None:
        if self._result is not None:
            return self._result
        if self._cancelled:
            return None
        started_at = datetime.now(UTC)
        started_clock = monotonic()
        deadline = started_clock + (timeout_ms / 1000)
        try:
            output = self._execute(deadline)
            self._result = self._result_record(
                status=ToolResultStatus.SUCCEEDED,
                output=output,
                started_at=started_at,
                started_clock=started_clock,
            )
        except _CommandTimeout:
            return None
        except (GitToolContractError, _CommandFailure, OSError) as error:
            self._result = self._result_record(
                status=ToolResultStatus.FAILED,
                output={
                    "operation": self._operation,
                    "repository": self._repository_id,
                    "commandResults": [
                        command.metadata() for command in self._commands
                    ],
                },
                started_at=started_at,
                started_clock=started_clock,
                failure_message=_redact(str(error)),
            )
        return self._result

    def terminate(self) -> None:
        self._cancelled = True

    def kill(self) -> None:
        self._cancelled = True

    def cleanup(self) -> None:
        return None

    def _execute(self, deadline: float) -> dict[str, Any]:
        resolved_base = self._run(
            ("rev-parse", "--verify", f"{self._expected_revision}^{{commit}}"),
            deadline,
        ).stdout.decode("ascii").strip().lower()
        if resolved_base != self._expected_revision:
            raise GitToolContractError(
                "configured expected revision no longer resolves identically"
            )

        if self._operation == "create_branch":
            head = self._run(("rev-parse", "HEAD"), deadline).stdout.decode(
                "ascii"
            ).strip().lower()
            if head != self._expected_revision:
                raise GitToolContractError(
                    "create_branch requires HEAD at the expected revision"
                )
            self._run(
                ("switch", "-c", self._working_branch, self._expected_revision),
                deadline,
            )
        else:
            branch = self._current_branch(deadline)
            if branch != self._working_branch:
                raise GitToolContractError(
                    f"{self._operation} requires branch {self._working_branch!r}"
                )
            if self._operation == "push_branch":
                self._run(
                    (
                        "push",
                        "--set-upstream",
                        self._remote,
                        self._working_branch,
                    ),
                    deadline,
                )
            elif self._operation not in {"status", "diff"}:
                raise GitToolContractError(
                    f"unsupported Git operation {self._operation!r}"
                )

        branch = self._current_branch(deadline)
        revision = self._run(("rev-parse", "HEAD"), deadline).stdout.decode(
            "ascii"
        ).strip().lower()
        status = self._run(
            ("status", "--porcelain=v1", "-z", "--untracked-files=all"), deadline
        ).stdout
        changed_files = _parse_status(status)
        diff_value: dict[str, Any] | None = None
        if self._operation == "diff":
            patch_parts = [
                self._run(
                    (
                        "diff",
                        "--no-ext-diff",
                        "--no-color",
                        "--binary",
                        self._expected_revision,
                        "--",
                    ),
                    deadline,
                ).stdout
            ]
            for changed_file in changed_files:
                if changed_file["status"] != "??":
                    continue
                patch_parts.append(
                    self._run(
                        (
                            "diff",
                            "--no-index",
                            "--no-ext-diff",
                            "--no-color",
                            "--binary",
                            "--",
                            os.devnull,
                            changed_file["path"],
                        ),
                        deadline,
                        accepted_exit_codes=(0, 1),
                    ).stdout
                )
            patch = b"".join(patch_parts)
            diff_value = {
                "text": patch.decode("utf-8", errors="replace"),
                "sha256": sha256(patch).hexdigest(),
                "byteLength": len(patch),
            }
        return {
            "operation": self._operation,
            "repository": self._repository_id,
            "branch": branch,
            "revision": revision,
            "baseRevision": self._expected_revision,
            "changedFiles": changed_files,
            "diff": diff_value,
            "pushed": self._operation == "push_branch",
            "commandResults": [command.metadata() for command in self._commands],
        }

    def _current_branch(self, deadline: float) -> str:
        return self._run(
            ("branch", "--show-current"), deadline
        ).stdout.decode("utf-8", errors="replace").strip()

    def _run(
        self,
        arguments: Sequence[str],
        deadline: float,
        *,
        accepted_exit_codes: tuple[int, ...] = (0,),
    ) -> _CommandResult:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise _CommandTimeout
        command = ("git", *arguments)
        environment = os.environ.copy()
        environment["GIT_TERMINAL_PROMPT"] = "0"
        try:
            completed = subprocess.run(
                command,
                cwd=self._repository,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=remaining,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            self._commands.append(
                _CommandResult(
                    arguments=tuple(arguments),
                    exit_code=-1,
                    stdout=error.stdout or b"",
                    stderr=error.stderr or b"",
                )
            )
            raise _CommandTimeout from error
        result = _CommandResult(
            arguments=tuple(arguments),
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
        self._commands.append(result)
        if result.exit_code not in accepted_exit_codes:
            raise _CommandFailure(result)
        return result

    def _result_record(
        self,
        *,
        status: ToolResultStatus,
        output: Mapping[str, Any],
        started_at: datetime,
        started_clock: float,
        failure_message: str | None = None,
    ) -> ToolResult:
        log_content = json.dumps(
            [command.log_record() for command in self._commands],
            sort_keys=True,
            separators=(",", ":"),
        )
        safe_trace = re.sub(r"[^A-Za-z0-9._-]", "_", self._trace_id)
        logs_ref = self._log_store.put(key=safe_trace, content=log_content)
        completed_at = datetime.now(UTC)
        return ToolResult(
            status=status,
            output=output,
            logs_ref=logs_ref,
            metrics=ToolMetrics(
                duration_ms=max(0, round((monotonic() - started_clock) * 1000))
            ),
            started_at=started_at.isoformat().replace("+00:00", "Z"),
            completed_at=completed_at.isoformat().replace("+00:00", "Z"),
            failure_class=(
                None
                if status is ToolResultStatus.SUCCEEDED
                else ToolFailureClass.ADAPTER
            ),
            failure_message=failure_message,
        )


def git_tool_validator() -> JsonSchemaToolValidator:
    """Return the public Git Tool input/output contract validator."""

    return JsonSchemaToolValidator(GIT_INPUT_SCHEMA, GIT_OUTPUT_SCHEMA)


def _parse_status(value: bytes) -> list[dict[str, str]]:
    entries = value.split(b"\0")
    changed: list[dict[str, str]] = []
    index = 0
    while index < len(entries):
        raw = entries[index]
        index += 1
        if not raw:
            continue
        text = raw.decode("utf-8", errors="surrogateescape")
        if len(text) < 4:
            raise GitToolContractError("git status returned malformed evidence")
        index_status, worktree_status = text[0], text[1]
        path = text[3:]
        record = {
            "path": path,
            "status": f"{index_status}{worktree_status}",
            "indexStatus": index_status,
            "worktreeStatus": worktree_status,
        }
        if index_status in {"R", "C"}:
            if index >= len(entries) or not entries[index]:
                raise GitToolContractError("git status omitted rename source")
            record["previousPath"] = entries[index].decode(
                "utf-8", errors="surrogateescape"
            )
            index += 1
        changed.append(record)
    return changed


def _valid_branch(value: str) -> bool:
    return bool(
        value
        and not value.startswith(("-", ".", "/"))
        and not value.endswith((".", "/", ".lock"))
        and not any(
            token in value
            for token in ("..", "//", "@{", "\\", " ", "~", "^", ":", "?", "*", "[")
        )
        and all(ord(character) >= 32 and ord(character) != 127 for character in value)
    )


def _redact(value: str) -> str:
    redacted = _CREDENTIAL_URL_PATTERN.sub(r"\1[REDACTED]@", value)
    return _TOKEN_PATTERN.sub("[REDACTED]", redacted)
