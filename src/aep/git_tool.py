"""Controlled Git Tool adapter for one execution-specific working branch."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from hashlib import sha256
import json
from pathlib import Path
import re
from time import monotonic, sleep
from typing import Any, Protocol
from uuid import uuid4

from aep.runtime_store import RuntimeObject, RuntimeObjectStore
from aep.observability import bind_correlation
from aep.tool_runtime import (
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


GIT_INPUT_SCHEMA: Mapping[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["operation", "expectedRevision", "branch"],
    "properties": {
        "operation": {
            "type": "string",
            "enum": ["create_branch", "status", "diff", "check_patch", "push_branch"],
        },
        "expectedRevision": {"type": "string", "pattern": "^[0-9a-fA-F]{40}$"},
        "branch": {"type": "string", "minLength": 1},
        "patch": {"type": "string"},
    },
    "allOf": [
        {
            "if": {
                "properties": {"operation": {"const": "check_patch"}},
                "required": ["operation"],
            },
            "then": {"required": ["patch"]},
            "else": {"not": {"required": ["patch"]}},
        }
    ],
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
        "remoteMutationState",
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
        "remoteMutationState": {
            "type": "string",
            "enum": ["NOT_ATTEMPTED", "CONFIRMED", "UNKNOWN"],
        },
        "commandResults": {
            "type": "array",
            "items": _COMMAND_RESULT_SCHEMA,
        },
        "applicable": {"type": ["boolean", "null"]},
        "diagnostics": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": False,
}

_REVISION_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")
_SAFE_REMOTE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_TOKEN_PATTERN = re.compile(
    r"(?i)(?:gh[opsu]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|"
    r"(?:token|password|passwd|authorization)\s*[=:]\s*(?:bearer\s+)?)(?:[^\s\"']+|\"[^\"]+\"|'[^']+')"
)
_CREDENTIAL_URL_PATTERN = re.compile(
    r"(?i)\b(https?://)(?:[^/@\s]+)@"
)


class GitToolContractError(ValueError):
    """Raised when adapter configuration cannot enforce repository boundaries."""


class GitInvocationIdentityConflictError(ValueError):
    """Raised when an invocation id is reused for different immutable inputs."""


class GitInvocationInProgressError(RuntimeError):
    """Raised when an identical invocation remains owned by another worker."""


class RemoteMutationState(str, Enum):
    """Observed state of the configured remote push side effect."""

    NOT_ATTEMPTED = "NOT_ATTEMPTED"
    CONFIRMED = "CONFIRMED"
    UNKNOWN = "UNKNOWN"


class GitCommandLogStore(Protocol):
    """Persistence boundary for redacted Git command evidence."""

    def put(self, *, key: str, content: str) -> str:
        """Persist content and return an immutable reference."""


@dataclass(frozen=True)
class GitSandboxCommandResult:
    """One command result returned by the isolated Git sandbox."""

    exit_code: int
    stdout: bytes = b""
    stderr: bytes = b""


class GitSandboxTimeout(TimeoutError):
    """Raised after the sandbox has terminated a command at its deadline."""

    def __init__(self, *, stdout: bytes = b"", stderr: bytes = b"") -> None:
        super().__init__("isolated Git command exceeded its deadline")
        self.stdout = stdout
        self.stderr = stderr


class GitSandbox(Protocol):
    """Isolation boundary supplied by the Tool Runtime.

    Implementations mount only ``repository``, execute Git without inheriting
    the Tool Runtime host environment, and terminate the command before raising
    ``GitSandboxTimeout``. Both special paths must be outside the repository
    mount and controlled by the sandbox.
    """

    @property
    def disabled_hooks_path(self) -> str:
        """Return a sandbox path that cannot contain repository hooks."""

    @property
    def null_device_path(self) -> str:
        """Return the sandbox null device path."""

    def run(
        self,
        *,
        repository: Path,
        arguments: Sequence[str],
        environment: Mapping[str, str],
        timeout_ms: int,
        stdin: bytes | None = None,
    ) -> GitSandboxCommandResult:
        """Run Git in isolation with exactly the supplied scoped environment."""


class GitCredentialLease(Protocol):
    """Short-lived credential material scoped to one push attempt."""

    @property
    def environment(self) -> Mapping[str, str]:
        """Return environment entries visible only to the isolated push."""

    def close(self) -> None:
        """Revoke and remove temporary credential material."""


class GitCredentialProvider(Protocol):
    """Issue temporary credentials for one configured remote and branch."""

    def acquire(self, *, remote: str, branch: str) -> GitCredentialLease:
        """Acquire a lease that the adapter closes after the push attempt."""


class _EmptyCredentialLease:
    @property
    def environment(self) -> Mapping[str, str]:
        return {}

    def close(self) -> None:
        return None


class NoGitCredentials:
    """Credential provider for local remotes and credential-free sandboxes."""

    def acquire(self, *, remote: str, branch: str) -> GitCredentialLease:
        return _EmptyCredentialLease()


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
    def __init__(self, result: _CommandResult) -> None:
        self.result = result
        super().__init__("isolated Git command exceeded its deadline")


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
        sandbox: GitSandbox,
        credential_provider: GitCredentialProvider | None = None,
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
        self._sandbox = sandbox
        self._credential_provider = credential_provider or NoGitCredentials()

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
            patch=(
                str(value.get("patch", "")).encode("utf-8")
                if operation == "check_patch"
                else None
            ),
            trace_id=request.trace_id,
            log_store=self._log_store,
            sandbox=self._sandbox,
            credential_provider=self._credential_provider,
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
        patch: bytes | None,
        trace_id: str,
        log_store: GitCommandLogStore,
        sandbox: GitSandbox,
        credential_provider: GitCredentialProvider,
    ) -> None:
        self._repository = repository
        self._repository_id = repository_id
        self._expected_revision = expected_revision
        self._working_branch = working_branch
        self._remote = remote
        self._operation = operation
        self._patch = patch
        self._trace_id = trace_id
        self._log_store = log_store
        self._sandbox = sandbox
        self._credential_provider = credential_provider
        self._commands: list[_CommandResult] = []
        self._result: ToolResult | None = None
        self._cancelled = False
        self._remote_mutation_state = RemoteMutationState.NOT_ATTEMPTED

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
            self._result = self._result_record(
                status=ToolResultStatus.TIMED_OUT,
                output=self._failure_output(),
                started_at=started_at,
                started_clock=started_clock,
                failure_message="isolated Git command exceeded its deadline",
                failure_class=ToolFailureClass.TIMEOUT,
            )
        except Exception as error:
            self._result = self._result_record(
                status=ToolResultStatus.FAILED,
                output=self._failure_output(),
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
            status = self._run(
                ("status", "--porcelain=v1", "-z", "--untracked-files=all"),
                deadline,
            ).stdout
            if status:
                raise GitToolContractError(
                    "create_branch requires a clean index and worktree"
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
            ancestry = self._run(
                (
                    "merge-base",
                    "--is-ancestor",
                    self._expected_revision,
                    "HEAD",
                ),
                deadline,
                accepted_exit_codes=(0, 1),
            )
            if ancestry.exit_code != 0:
                raise GitToolContractError(
                    "configured expected revision is not an ancestor of HEAD"
                )
            if self._operation == "push_branch":
                credentials = self._credential_provider.acquire(
                    remote=self._remote, branch=self._working_branch
                )
                try:
                    self._run(
                        (
                            "push",
                            "--set-upstream",
                            self._remote,
                            self._working_branch,
                        ),
                        deadline,
                        environment=credentials.environment,
                        begins_remote_mutation=True,
                    )
                    self._remote_mutation_state = RemoteMutationState.CONFIRMED
                finally:
                    credentials.close()
            elif self._operation not in {"status", "diff", "check_patch"}:
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
        applicable: bool | None = None
        diagnostics: list[str] = []
        if self._operation == "check_patch":
            head = self._run(("rev-parse", "HEAD"), deadline).stdout.decode(
                "ascii"
            ).strip().lower()
            if head != self._expected_revision:
                raise GitToolContractError(
                    "check_patch requires HEAD at the expected revision"
                )
            if status:
                raise GitToolContractError(
                    "check_patch requires a clean index and worktree"
                )
            assert self._patch is not None
            numstat = self._run(
                ("apply", "--numstat", "-z", "--"),
                deadline,
                accepted_exit_codes=(0, 1, 128),
                stdin=self._patch,
            )
            changed_files = (
                _parse_numstat(numstat.stdout) if numstat.exit_code == 0 else []
            )
            changed_files = _merge_patch_source_paths(changed_files, self._patch)
            check = self._run(
                ("apply", "--check", "--cached", "--"),
                deadline,
                accepted_exit_codes=(0, 1, 128),
                stdin=self._patch,
            )
            applicable = check.exit_code == 0
            diagnostics = _diagnostics(numstat.stderr, check.stderr)
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
                            self._sandbox.null_device_path,
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
            "remoteMutationState": self._remote_mutation_state.value,
            "commandResults": [command.metadata() for command in self._commands],
            "applicable": applicable,
            "diagnostics": diagnostics,
        }

    def _failure_output(self) -> dict[str, Any]:
        return {
            "operation": self._operation,
            "repository": self._repository_id,
            "remoteMutationState": self._remote_mutation_state.value,
            "commandResults": [
                command.metadata() for command in self._commands
            ],
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
        environment: Mapping[str, str] | None = None,
        begins_remote_mutation: bool = False,
        stdin: bytes | None = None,
    ) -> _CommandResult:
        remaining = deadline - monotonic()
        if remaining <= 0:
            result = _CommandResult(
                arguments=tuple(arguments),
                exit_code=-1,
                stdout=b"",
                stderr=b"",
            )
            self._commands.append(result)
            raise _CommandTimeout(result)
        controlled_arguments = (
            "-c",
            f"core.hooksPath={self._sandbox.disabled_hooks_path}",
            *arguments,
        )
        scoped_environment = {
            **dict(environment or {}),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
        try:
            if begins_remote_mutation:
                self._remote_mutation_state = RemoteMutationState.UNKNOWN
            completed = self._sandbox.run(
                repository=self._repository,
                arguments=controlled_arguments,
                environment=scoped_environment,
                timeout_ms=max(1, round(remaining * 1000)),
                stdin=stdin,
            )
        except GitSandboxTimeout as error:
            result = _CommandResult(
                arguments=tuple(controlled_arguments),
                exit_code=-1,
                stdout=error.stdout,
                stderr=error.stderr,
            )
            self._commands.append(result)
            raise _CommandTimeout(result) from error
        result = _CommandResult(
            arguments=tuple(controlled_arguments),
            exit_code=completed.exit_code,
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
        failure_class: ToolFailureClass = ToolFailureClass.ADAPTER,
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
                else failure_class
            ),
            failure_message=failure_message,
        )


def git_tool_validator() -> JsonSchemaToolValidator:
    """Return the public Git Tool input/output contract validator."""

    return JsonSchemaToolValidator(GIT_INPUT_SCHEMA, GIT_OUTPUT_SCHEMA)


class GitTool:
    """Run the Git adapter with atomic, retry-safe ToolInvocation evidence."""

    replay_wait_seconds = 1.0

    def __init__(self, adapter: GitToolAdapter, store: RuntimeObjectStore) -> None:
        if not isinstance(adapter, GitToolAdapter):
            raise TypeError("adapter must be a GitToolAdapter")
        self.adapter = adapter
        self._store = store

    def invoke(
        self,
        *,
        invocation_id: str,
        task_execution_id: str,
        request: ToolRequest,
        authorize: Any,
        policy_decision_id: str | None = None,
    ) -> tuple[ToolResult, RuntimeObject]:
        bind_correlation(request.correlation, task_execution_id=task_execution_id)
        fingerprint = _git_request_fingerprint(
            task_execution_id, request, policy_decision_id
        )
        owner_token = str(uuid4())
        started_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        pending = _git_pending_record(
            invocation_id,
            task_execution_id,
            request,
            fingerprint,
            policy_decision_id,
            started_at,
            owner_token,
        )
        created = self._store.create(
            pending, deterministic_key=f"git-tool-invocation:{invocation_id}"
        )
        if created.get("requestFingerprint") != fingerprint:
            raise GitInvocationIdentityConflictError(
                f"invocation id {invocation_id!r} is already bound to different "
                "immutable request inputs"
            )
        if created.get("ownerToken") != owner_token:
            if created.get("status") in {"SUCCEEDED", "FAILED"}:
                return _git_result_from_invocation(created), created
            return self._await_terminal(invocation_id)

        try:
            result = invoke_tool(
                request,
                validator=git_tool_validator(),
                authorize=authorize,
                adapter=self.adapter,
            )
        except Exception as error:
            completed_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            result = ToolResult(
                status=ToolResultStatus.FAILED,
                output=None,
                logs_ref=None,
                metrics=ToolMetrics(duration_ms=0),
                started_at=started_at,
                completed_at=completed_at,
                failure_class=ToolFailureClass.ADAPTER,
                failure_message=str(error) or type(error).__name__,
            )
        status = "SUCCEEDED" if result.status is ToolResultStatus.SUCCEEDED else "FAILED"
        persisted = self._store.update_status(
            invocation_id,
            status,
            expected_status="PENDING",
            updated_at=result.completed_at,
            changes=_git_terminal_changes(result),
        )
        return result, persisted

    def _await_terminal(
        self, invocation_id: str
    ) -> tuple[ToolResult, RuntimeObject]:
        deadline = monotonic() + self.replay_wait_seconds
        while monotonic() < deadline:
            prior = self._store.get(invocation_id)
            if prior is not None and prior.get("status") in {"SUCCEEDED", "FAILED"}:
                return _git_result_from_invocation(prior), prior
            sleep(0.001)
        raise GitInvocationInProgressError(
            f"identical invocation {invocation_id!r} is still in progress"
        )


def _git_pending_record(
    invocation_id: str,
    task_execution_id: str,
    request: ToolRequest,
    fingerprint: str,
    policy_decision_id: str | None,
    timestamp: str,
    owner_token: str,
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
            "workflowExecutionId": request.correlation.workflow_execution_id,
            "taskExecutionId": task_execution_id,
            "resourceRefs": [dict(request.tool_ref)],
        },
        "taskExecutionId": task_execution_id,
        "toolRef": dict(request.tool_ref),
        "status": "PENDING",
        "input": dict(request.input),
        "capabilities": list(request.capabilities),
        "requestFingerprint": fingerprint,
        "ownerToken": owner_token,
    }
    if request.caller.kind == "AgentInvocation":
        record["agentInvocationId"] = request.caller.id
    if policy_decision_id is not None:
        record["policyDecisionId"] = policy_decision_id
    return record


def _git_terminal_changes(result: ToolResult) -> dict[str, Any]:
    changes: dict[str, Any] = {
        "resultStatus": result.status.value,
        "output": result.output_record(),
        "metrics": result.metrics.as_record(),
        "startedAt": result.started_at,
    }
    if _content_address_ref(result.logs_ref):
        changes["logsAddress"] = result.logs_ref
    elif result.logs_ref is not None:
        changes["adapterLogsRef"] = result.logs_ref
    if result.failure_class is not None:
        changes["failureClass"] = result.failure_class.value
        changes["failure"] = {
            "class": _git_runtime_failure_class(result.failure_class),
            "message": result.failure_message or result.failure_class.value,
            "retryable": result.failure_class
            in {ToolFailureClass.IO, ToolFailureClass.TIMEOUT},
        }
    return changes


def _git_request_fingerprint(
    task_execution_id: str,
    request: ToolRequest,
    policy_decision_id: str | None,
) -> str:
    value = {
        "taskExecutionId": task_execution_id,
        "toolRef": dict(request.tool_ref),
        "input": dict(request.input),
        "caller": request.caller.as_record(),
        "capabilities": list(request.capabilities),
        "timeoutMs": request.timeout_ms,
        "correlation": {
            "traceId": request.trace_id,
            "workflowExecutionId": request.correlation.workflow_execution_id,
            "taskExecutionId": request.correlation.task_execution_id,
        },
        "policyDecisionId": policy_decision_id,
    }
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return "sha256:" + sha256(canonical.encode("utf-8")).hexdigest()


def _git_result_from_invocation(invocation: RuntimeObject) -> ToolResult:
    failure_class = invocation.get("failureClass")
    failure = invocation.get("failure")
    metrics = invocation.get("metrics", {})
    return ToolResult(
        status=ToolResultStatus(str(invocation["resultStatus"])),
        output=invocation.get("output"),
        logs_ref=invocation.get("logsAddress") or invocation.get("adapterLogsRef"),
        metrics=ToolMetrics(
            duration_ms=int(metrics.get("durationMs", 0)),
            cpu_ms=metrics.get("cpuMs"),
            memory_bytes=metrics.get("memoryBytes"),
        ),
        started_at=str(invocation["startedAt"]),
        completed_at=str(invocation["completedAt"]),
        failure_class=ToolFailureClass(str(failure_class)) if failure_class else None,
        failure_message=(
            failure.get("message") if isinstance(failure, Mapping) else None
        ),
    )


def _git_runtime_failure_class(value: ToolFailureClass) -> str:
    return {
        ToolFailureClass.VALIDATION: "CONFIGURATION",
        ToolFailureClass.POLICY: "POLICY",
        ToolFailureClass.TIMEOUT: "RECOVERABLE",
        ToolFailureClass.IO: "RECOVERABLE",
        ToolFailureClass.BOUNDARY: "POLICY",
        ToolFailureClass.NOT_FOUND: "PERMANENT",
        ToolFailureClass.STARTUP: "RECOVERABLE",
        ToolFailureClass.NONZERO_EXIT: "PERMANENT",
        ToolFailureClass.ADAPTER: "PERMANENT",
    }[value]


def _content_address_ref(value: object) -> bool:
    if not isinstance(value, str):
        return False
    algorithm, separator, digest = value.partition(":")
    expected_length = {"sha256": 64, "sha512": 128}.get(algorithm)
    return (
        separator == ":"
        and expected_length is not None
        and len(digest) == expected_length
        and all(character in "0123456789abcdef" for character in digest)
    )


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


def _parse_numstat(value: bytes) -> list[dict[str, str]]:
    """Parse ``git apply --numstat -z`` into stable changed-path evidence."""

    entries = value.split(b"\0")
    changed: list[dict[str, str]] = []
    index = 0
    while index < len(entries):
        raw = entries[index]
        index += 1
        if not raw:
            continue
        fields = raw.split(b"\t", 2)
        if len(fields) != 3:
            raise GitToolContractError("git apply returned malformed path evidence")
        path = fields[2]
        previous_path: bytes | None = None
        if not path:
            if index + 1 >= len(entries):
                raise GitToolContractError("git apply returned malformed rename evidence")
            previous_path = entries[index]
            path = entries[index + 1]
            index += 2
        record = {
            "path": path.decode("utf-8", errors="surrogateescape"),
            "status": "PATCH",
        }
        if previous_path is not None:
            record["previousPath"] = previous_path.decode(
                "utf-8", errors="surrogateescape"
            )
        changed.append(record)
    return sorted(changed, key=lambda item: (item["path"].casefold(), item["path"]))


def _diagnostics(*values: bytes) -> list[str]:
    lines = {
        _redact(line.strip())
        for value in values
        for line in value.decode("utf-8", errors="replace").splitlines()
        if line.strip()
    }
    return sorted(lines, key=lambda line: (line.casefold(), line))


def _merge_patch_source_paths(
    changed: list[dict[str, str]], patch: bytes
) -> list[dict[str, str]]:
    """Include rename/copy sources that ``--numstat`` collapses into destinations."""

    records = {record["path"]: record for record in changed}
    for raw_line in patch.splitlines():
        if raw_line.startswith(b"rename from "):
            value = raw_line[len(b"rename from ") :]
        elif raw_line.startswith(b"copy from "):
            value = raw_line[len(b"copy from ") :]
        else:
            continue
        path = _decode_patch_path(value)
        records.setdefault(path, {"path": path, "status": "PATCH_SOURCE"})
    return sorted(records.values(), key=lambda item: (item["path"].casefold(), item["path"]))


def _decode_patch_path(value: bytes) -> str:
    if not value.startswith(b'"'):
        try:
            return value.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise GitToolContractError("patch path is not valid UTF-8") from error
    if len(value) < 2 or not value.endswith(b'"'):
        raise GitToolContractError("patch contains malformed quoted path")

    escapes = {
        ord("a"): 0x07,
        ord("b"): 0x08,
        ord("t"): 0x09,
        ord("n"): 0x0A,
        ord("v"): 0x0B,
        ord("f"): 0x0C,
        ord("r"): 0x0D,
        ord("\\"): ord("\\"),
        ord('"'): ord('"'),
    }
    encoded = bytearray()
    quoted = value[1:-1]
    index = 0
    while index < len(quoted):
        current = quoted[index]
        index += 1
        if current != ord("\\"):
            encoded.append(current)
            continue
        if index >= len(quoted):
            raise GitToolContractError("patch contains malformed quoted path")
        escaped = quoted[index]
        if escaped in escapes:
            encoded.append(escapes[escaped])
            index += 1
            continue
        if ord("0") <= escaped <= ord("7"):
            digits = bytearray()
            while (
                index < len(quoted)
                and len(digits) < 3
                and ord("0") <= quoted[index] <= ord("7")
            ):
                digits.append(quoted[index])
                index += 1
            decoded_byte = int(digits.decode("ascii"), 8)
            if decoded_byte > 0xFF:
                raise GitToolContractError("patch contains invalid octal path escape")
            encoded.append(decoded_byte)
            continue
        raise GitToolContractError("patch contains unsupported quoted path escape")

    if b"\0" in encoded:
        raise GitToolContractError("patch path must not contain NUL")
    try:
        return bytes(encoded).decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise GitToolContractError("patch path is not valid UTF-8") from error


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
