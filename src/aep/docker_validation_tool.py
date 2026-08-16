"""Docker-backed deterministic build and test execution."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess
from time import monotonic, sleep
from typing import Any
from uuid import uuid4

from aep.observability import bind_correlation
from aep.runtime_store import RuntimeObject, RuntimeObjectStore, RuntimeStoreError
from aep.tool_runtime import (
    AuthorizationHook,
    JsonSchemaToolValidator,
    ToolAdapter,
    ToolAdapterError,
    ToolExecution,
    ToolFailureClass,
    ToolMetrics,
    ToolRequest,
    ToolResult,
    ToolResultStatus,
    invoke_tool,
)


DOCKER_RUN_CAPABILITY = "docker.run"
DOCKER_WORKSPACE_DESTINATION = "/workspace"
IMAGE_DIGEST_PATTERN = r"^[A-Za-z0-9._/-]+@sha256:[0-9a-f]{64}$"
INVOCATION_REPLAY_GRACE_MS = 1_000

DOCKER_VALIDATION_INPUT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "image", "requiredExecutables", "commands", "workspaceMount", "resources"
    ],
    "properties": {
        "image": {"type": "string", "pattern": IMAGE_DIGEST_PATTERN},
        "requiredExecutables": {
            "type": "array", "minItems": 1, "uniqueItems": True,
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["argv", "versionPattern"],
                "properties": {
                    "argv": {"type": "array", "minItems": 1,
                             "items": {"type": "string", "minLength": 1}},
                    "versionPattern": {"type": "string", "minLength": 1, "maxLength": 200},
                },
            },
        },
        "commands": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["argv"],
                "properties": {
                    "argv": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string", "minLength": 1},
                    }
                },
            },
        },
        "workspaceMount": {
            "type": "object",
            "additionalProperties": False,
            "required": ["hostPath", "containerPath", "readOnly"],
            "properties": {
                "hostPath": {"type": "string", "minLength": 1},
                "containerPath": {"type": "string", "minLength": 1},
                "readOnly": {"type": "boolean"},
            },
        },
        "resources": {
            "type": "object",
            "additionalProperties": False,
            "required": ["cpuLimit", "memoryBytes"],
            "properties": {
                "cpuLimit": {"type": "number", "exclusiveMinimum": 0},
                "memoryBytes": {"type": "integer", "minimum": 1},
            },
        },
    },
}

DOCKER_VALIDATION_OUTPUT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["image", "workspaceMount", "readiness", "commands"],
    "properties": {
        "image": {"type": "string", "minLength": 1},
        "workspaceMount": {
            "type": "object",
            "additionalProperties": False,
            "required": ["hostPath", "containerPath", "readOnly"],
            "properties": {
                "hostPath": {"type": "string", "minLength": 1},
                "containerPath": {"type": "string", "minLength": 1},
                "readOnly": {"type": "boolean"},
            },
        },
        "readiness": {
            "type": "object", "additionalProperties": False,
            "required": ["status", "executables"],
            "properties": {
                "status": {"enum": ["PASS"]},
                "executables": {"type": "array", "minItems": 1,
                    "items": {"type": "object", "additionalProperties": False,
                        "required": ["argv", "versionPattern", "output", "logsRef"],
                        "properties": {
                            "argv": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
                            "versionPattern": {"type": "string", "minLength": 1},
                            "output": {"type": "string", "maxLength": 1024},
                            "logsRef": {"type": "string", "minLength": 1},
                        }}},
            },
        },
        "commands": {
            "type": "array",
            "minItems": 0,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "argv",
                    "stdout",
                    "stderr",
                    "exitCode",
                    "durationMs",
                    "logsRef",
                ],
                "properties": {
                    "argv": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string", "minLength": 1},
                    },
                    "stdout": {"type": "string"},
                    "stderr": {"type": "string"},
                    "exitCode": {"type": "integer"},
                    "durationMs": {"type": "integer", "minimum": 0},
                    "logsRef": {"type": "string", "minLength": 1},
                },
            },
        },
    },
}


def docker_validation_validator() -> JsonSchemaToolValidator:
    """Return the public input and output contract validator."""

    return JsonSchemaToolValidator(
        DOCKER_VALIDATION_INPUT_SCHEMA, DOCKER_VALIDATION_OUTPUT_SCHEMA
    )


class DockerInvocationIdentityConflictError(ValueError):
    """Raised when an invocation id is rebound to different immutable inputs."""


class DockerValidationTool:
    """Run Docker validation with retry-safe persisted ToolInvocation evidence."""

    def __init__(
        self,
        adapter: "DockerValidationAdapter",
        store: RuntimeObjectStore,
        *,
        replay_grace_ms: int = INVOCATION_REPLAY_GRACE_MS,
    ) -> None:
        if not isinstance(adapter, DockerValidationAdapter):
            raise TypeError("adapter must be a DockerValidationAdapter")
        if (
            not isinstance(replay_grace_ms, int)
            or isinstance(replay_grace_ms, bool)
            or replay_grace_ms < 0
        ):
            raise ValueError("replay_grace_ms must be a non-negative integer")
        self.adapter = adapter
        self._store = store
        self._replay_grace_ms = replay_grace_ms

    def invoke(
        self,
        *,
        invocation_id: str,
        task_execution_id: str,
        request: ToolRequest,
        authorize: AuthorizationHook,
        policy_decision_id: str | None = None,
    ) -> tuple[ToolResult, RuntimeObject]:
        bind_correlation(request.correlation, task_execution_id=task_execution_id)
        fingerprint = _request_fingerprint(
            task_execution_id, request, policy_decision_id
        )
        owner_token = str(uuid4())
        started_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        pending = _pending_invocation_record(
            invocation_id,
            task_execution_id,
            request,
            fingerprint,
            policy_decision_id,
            started_at,
            owner_token,
        )
        created = self._store.create(
            pending, deterministic_key=f"docker-tool-invocation:{invocation_id}"
        )
        if created.get("requestFingerprint") != fingerprint:
            raise DockerInvocationIdentityConflictError(
                f"invocation id {invocation_id!r} is already bound to different "
                "immutable request inputs"
            )
        if created.get("ownerToken") != owner_token:
            if created.get("status") in {"SUCCEEDED", "FAILED"}:
                return _result_from_invocation(created), created
            return self._await_terminal(
                invocation_id,
                deadline_ms=request.timeout_ms + self._replay_grace_ms,
            )

        try:
            result = invoke_tool(
                request,
                validator=docker_validation_validator(),
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
        try:
            persisted = self._store.update_status(
                invocation_id,
                status,
                expected_status="PENDING",
                updated_at=result.completed_at,
                changes=_terminal_invocation_changes(result),
            )
            return result, persisted
        except RuntimeStoreError:
            prior = self._store.get(invocation_id)
            if prior is not None and prior.get("status") in {"SUCCEEDED", "FAILED"}:
                return _result_from_invocation(prior), prior
            raise

    def _await_terminal(
        self, invocation_id: str, *, deadline_ms: int
    ) -> tuple[ToolResult, RuntimeObject]:
        deadline = monotonic() + (deadline_ms / 1_000)
        while monotonic() < deadline:
            prior = self._store.get(invocation_id)
            if prior is not None and prior.get("status") in {"SUCCEEDED", "FAILED"}:
                return _result_from_invocation(prior), prior
            sleep(0.001)
        prior = self._store.get(invocation_id)
        timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        started_at = (
            prior.get("createdAt")
            if isinstance(prior, Mapping) and isinstance(prior.get("createdAt"), str)
            else timestamp
        )
        abandoned = ToolResult(
            status=ToolResultStatus.TIMED_OUT,
            output=None,
            logs_ref=None,
            metrics=ToolMetrics(duration_ms=deadline_ms),
            started_at=started_at,
            completed_at=timestamp,
            failure_class=ToolFailureClass.TIMEOUT,
            failure_message=(
                "invocation owner did not persist terminal evidence before "
                "the request deadline"
            ),
        )
        try:
            persisted = self._store.update_status(
                invocation_id,
                "FAILED",
                expected_status="PENDING",
                updated_at=timestamp,
                changes=_terminal_invocation_changes(abandoned),
            )
            return abandoned, persisted
        except RuntimeStoreError:
            prior = self._store.get(invocation_id)
            if prior is not None and prior.get("status") in {"SUCCEEDED", "FAILED"}:
                return _result_from_invocation(prior), prior
            raise


def _pending_invocation_record(
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
        "input": _json_copy(request.input),
        "capabilities": list(request.capabilities),
        "requestFingerprint": fingerprint,
        "ownerToken": owner_token,
    }
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
            in {ToolFailureClass.IO, ToolFailureClass.TIMEOUT, ToolFailureClass.STARTUP},
        }
        changes["failureClass"] = result.failure_class.value
    return changes


def _result_from_invocation(invocation: RuntimeObject) -> ToolResult:
    metrics = invocation.get("metrics", {})
    failure = invocation.get("failure")
    failure_class = invocation.get("failureClass")
    return ToolResult(
        status=ToolResultStatus(invocation["resultStatus"]),
        output=invocation.get("output"),
        logs_ref=invocation.get("logsAddress"),
        metrics=ToolMetrics(
            duration_ms=metrics.get("durationMs", 0),
            cpu_ms=metrics.get("cpuMs"),
            memory_bytes=metrics.get("memoryBytes"),
        ),
        started_at=invocation["startedAt"],
        completed_at=invocation["completedAt"],
        failure_class=(
            ToolFailureClass(failure_class) if failure_class is not None else None
        ),
        failure_message=(failure.get("message") if isinstance(failure, Mapping) else None),
    )


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


def _runtime_failure_class(value: ToolFailureClass) -> str:
    return {
        ToolFailureClass.VALIDATION: "CONFIGURATION",
        ToolFailureClass.POLICY: "POLICY",
        ToolFailureClass.TIMEOUT: "RECOVERABLE",
        ToolFailureClass.ADAPTER: "PERMANENT",
        ToolFailureClass.STARTUP: "RECOVERABLE",
        ToolFailureClass.NONZERO_EXIT: "EVALUATION",
        ToolFailureClass.BOUNDARY: "POLICY",
        ToolFailureClass.NOT_FOUND: "PERMANENT",
        ToolFailureClass.IO: "RECOVERABLE",
        ToolFailureClass.CONFIGURATION: "CONFIGURATION",
    }[value]


def _json_copy(value: Mapping[str, Any]) -> dict[str, Any]:
    def thaw(item: Any) -> Any:
        if isinstance(item, Mapping):
            return {key: thaw(child) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [thaw(child) for child in item]
        return item

    return json.loads(json.dumps(thaw(value)))


@dataclass(frozen=True)
class DockerWorkspaceMount:
    host_path: str
    container_path: str
    read_only: bool

    def __post_init__(self) -> None:
        if not self.host_path or not self.container_path:
            raise ValueError("workspace mount paths must not be empty")

    def as_record(self) -> dict[str, Any]:
        return {
            "hostPath": self.host_path,
            "containerPath": self.container_path,
            "readOnly": self.read_only,
        }


@dataclass(frozen=True)
class DockerResourceSettings:
    cpu_limit: float
    memory_bytes: int

    def __post_init__(self) -> None:
        if self.cpu_limit <= 0 or self.memory_bytes < 1:
            raise ValueError("Docker resource settings must be positive")


@dataclass(frozen=True)
class DockerRunConfiguration:
    image: str
    required_executables: tuple[tuple[tuple[str, ...], str], ...]
    commands: tuple[tuple[str, ...], ...]
    workspace_mount: DockerWorkspaceMount
    timeout_ms: int
    resources: DockerResourceSettings

    def __post_init__(self) -> None:
        commands = tuple(tuple(command) for command in self.commands)
        executables = tuple((tuple(argv), pattern) for argv, pattern in self.required_executables)
        if not self.image or not commands or not executables:
            raise ValueError("Docker image, readiness commands, and commands must not be empty")
        if any(not argv or not pattern for argv, pattern in executables):
            raise ValueError("Docker readiness command arguments and patterns must not be empty")
        try:
            for _argv, pattern in executables:
                re.compile(pattern)
        except re.error as error:
            raise DockerImageReadinessError(
                "Docker readiness versionPattern is not a valid regular expression"
            ) from error
        if any(
            not command or any(not value for value in command)
            for command in commands
        ):
            raise ValueError("Docker command arguments must not be empty")
        if self.timeout_ms < 1:
            raise ValueError("Docker timeout must be positive")
        object.__setattr__(self, "commands", commands)
        object.__setattr__(self, "required_executables", executables)


@dataclass(frozen=True)
class DockerCommandResult:
    argv: tuple[str, ...]
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int
    logs_ref: str

    def __post_init__(self) -> None:
        argv = tuple(self.argv)
        if not argv or any(not isinstance(value, str) or not value for value in argv):
            raise ValueError("command argv must contain non-empty values")
        if not isinstance(self.stdout, str) or not isinstance(self.stderr, str):
            raise ValueError("command stdout and stderr must be strings")
        if not isinstance(self.exit_code, int):
            raise ValueError("command exit_code must be an integer")
        if not isinstance(self.duration_ms, int) or self.duration_ms < 0:
            raise ValueError("command duration_ms must not be negative")
        if not isinstance(self.logs_ref, str) or not self.logs_ref:
            raise ValueError("command logs_ref must not be empty")
        object.__setattr__(self, "argv", argv)

    def as_record(self) -> dict[str, Any]:
        return {
            "argv": list(self.argv),
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exitCode": self.exit_code,
            "durationMs": self.duration_ms,
            "logsRef": self.logs_ref,
        }


@dataclass(frozen=True)
class DockerExecutionResult:
    commands: tuple[DockerCommandResult, ...]
    logs_ref: str
    started_at: str
    completed_at: str
    readiness: tuple[Mapping[str, Any], ...]

    def __post_init__(self) -> None:
        commands = tuple(self.commands)
        if not commands or any(
            not isinstance(command, DockerCommandResult) for command in commands
        ):
            raise ValueError("execution result must contain command evidence")
        if not self.logs_ref:
            raise ValueError("execution logs_ref must not be empty")
        if not self.started_at or not self.completed_at:
            raise ValueError("execution result must contain timing evidence")
        readiness = tuple(dict(item) for item in self.readiness)
        if not readiness:
            raise ValueError("execution result must contain readiness evidence")
        object.__setattr__(self, "commands", commands)
        object.__setattr__(self, "readiness", readiness)


@dataclass(frozen=True)
class DockerTimeoutResult:
    """Completed command evidence captured before the shared deadline expired."""

    commands: tuple[DockerCommandResult, ...]
    logs_ref: str
    started_at: str
    completed_at: str

    def __post_init__(self) -> None:
        commands = tuple(self.commands)
        if any(not isinstance(command, DockerCommandResult) for command in commands):
            raise ValueError("timeout evidence must contain command results")
        if not self.logs_ref or not self.started_at or not self.completed_at:
            raise ValueError("timeout result must contain logs and timing evidence")
        object.__setattr__(self, "commands", commands)


class DockerStartupError(ToolAdapterError):
    """Raised when the Docker sandbox cannot be provisioned."""

    failure_class = ToolFailureClass.STARTUP


class DockerImageReadinessError(ToolAdapterError):
    """The declared immutable image cannot satisfy validation prerequisites."""

    failure_class = ToolFailureClass.CONFIGURATION


class DockerExecution(ABC):
    """Injectable Docker lifecycle controlled by the shared Tool Runtime."""

    @abstractmethod
    def wait(
        self, timeout_ms: int
    ) -> DockerExecutionResult | DockerTimeoutResult | None:
        """Return captured command evidence, or None when the deadline expires."""

    @abstractmethod
    def terminate(self) -> None:
        """Request graceful container termination."""

    @abstractmethod
    def kill(self) -> None:
        """Force container termination."""

    @abstractmethod
    def cleanup(self) -> None:
        """Remove containers and other executor-owned resources."""


class DockerExecutor(ABC):
    """Provider-neutral boundary for a Docker engine implementation."""

    @abstractmethod
    def start(self, configuration: DockerRunConfiguration) -> DockerExecution:
        """Provision the validation container execution."""

    @abstractmethod
    def cleanup_startup(self) -> None:
        """Remove resources left by an unsuccessful provisioning attempt."""


@dataclass(frozen=True)
class DockerProcessResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int


class DockerProcessBoundary(ABC):
    """Injectable boundary around Docker CLI process execution."""

    @abstractmethod
    def run(
        self, argv: Sequence[str], timeout_ms: int
    ) -> DockerProcessResult | None:
        """Run a Docker CLI command, returning None on timeout."""


class SubprocessDockerProcessBoundary(DockerProcessBoundary):
    """Production Docker CLI process boundary."""

    def run(
        self, argv: Sequence[str], timeout_ms: int
    ) -> DockerProcessResult | None:
        started = monotonic()
        try:
            completed = subprocess.run(
                list(argv),
                capture_output=True,
                text=True,
                timeout=timeout_ms / 1000,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return None
        return DockerProcessResult(
            stdout=completed.stdout,
            stderr=completed.stderr,
            exit_code=completed.returncode,
            duration_ms=max(0, round((monotonic() - started) * 1000)),
        )


class DockerLogStore(ABC):
    @abstractmethod
    def write(self, content: str) -> str:
        """Persist logs and return their content address."""


class ContentAddressedDockerLogStore(DockerLogStore):
    """Filesystem-backed content-addressed Docker log storage."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def write(self, content: str) -> str:
        encoded = content.encode("utf-8")
        digest = sha256(encoded).hexdigest()
        path = self._root / digest
        if not path.exists():
            path.write_bytes(encoded)
        return f"sha256:{digest}"


class DockerCliExecutor(DockerExecutor):
    """Production-capable invocation-scoped Docker CLI executor."""

    def __init__(
        self,
        process: DockerProcessBoundary,
        logs: DockerLogStore,
        *,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._process = process
        self._logs = logs
        self._clock = clock

    def start(self, configuration: DockerRunConfiguration) -> DockerExecution:
        container_name = f"aep-validation-{uuid4().hex}"
        deadline = self._clock() + configuration.timeout_ms / 1000

        def remaining_ms() -> int:
            remaining = round((deadline - self._clock()) * 1000)
            if remaining < 1:
                raise DockerStartupError("Docker startup exceeded invocation deadline")
            return remaining

        mount = configuration.workspace_mount
        mount_spec = (
            f"type=bind,src={mount.host_path},dst={mount.container_path}"
            + (",readonly" if mount.read_only else "")
        )
        create = [
            "docker", "create", "--name", container_name,
            "--network", "none",
            "--cpus", str(configuration.resources.cpu_limit),
            "--memory", str(configuration.resources.memory_bytes),
            "--mount", mount_spec, configuration.image, "sleep", "infinity",
        ]
        try:
            created = self._process.run(create, remaining_ms())
            if created is None:
                raise DockerStartupError("Docker create timed out")
            if created.exit_code != 0:
                detail = created.stderr.strip() or created.stdout.strip() or "unknown error"
                raise DockerStartupError(
                    f"Docker create failed (exit {created.exit_code}): {detail}"
                )
            started = self._process.run(
                ["docker", "start", container_name], remaining_ms()
            )
            if started is None:
                raise DockerStartupError("Docker start timed out")
            if started.exit_code != 0:
                detail = started.stderr.strip() or started.stdout.strip() or "unknown error"
                raise DockerStartupError(
                    f"Docker start failed (exit {started.exit_code}): {detail}"
                )
        except Exception:
            self._process.run(
                ["docker", "rm", "-f", container_name],
                min(configuration.timeout_ms, 5_000),
            )
            raise
        return _DockerCliExecution(
            configuration,
            container_name,
            self._process,
            self._logs,
            deadline,
            self._clock,
        )

    def cleanup_startup(self) -> None:
        # Invocation-scoped cleanup is performed inside start while the name is known.
        return None


class _DockerCliExecution(DockerExecution):
    def __init__(
        self,
        configuration: DockerRunConfiguration,
        container_name: str,
        process: DockerProcessBoundary,
        logs: DockerLogStore,
        deadline: float,
        clock: Callable[[], float],
    ) -> None:
        self._configuration = configuration
        self._name = container_name
        self._process = process
        self._logs = logs
        self._deadline = deadline
        self._clock = clock
        self._timed_out = False

    def wait(
        self, timeout_ms: int
    ) -> DockerExecutionResult | DockerTimeoutResult | None:
        if self._timed_out:
            return None
        started_at = datetime.now(UTC)
        deadline = min(self._deadline, self._clock() + timeout_ms / 1000)
        evidence: list[DockerCommandResult] = []
        combined: list[str] = []
        readiness: list[dict[str, Any]] = []
        for argv, version_pattern in self._configuration.required_executables:
            remaining_ms = max(0, round((deadline - self._clock()) * 1000))
            if remaining_ms < 1:
                self._timed_out = True
                return self._timeout_result(evidence, combined, started_at)
            result = self._process.run(
                ["docker", "exec", self._name, *argv], remaining_ms
            )
            if result is None:
                self._timed_out = True
                return self._timeout_result(evidence, combined, started_at)
            output = (result.stdout + result.stderr).strip()[:1024]
            logs_ref = self._logs.write(f"readiness {list(argv)!r}:\n{output}")
            if result.exit_code != 0 or re.search(version_pattern, output) is None:
                reason = "is unavailable" if result.exit_code != 0 else "reported an incompatible version"
                raise DockerImageReadinessError(
                    f"validation image prerequisite {argv[0]!r} {reason}"
                )
            readiness.append({"argv": list(argv), "versionPattern": version_pattern,
                              "output": output, "logsRef": logs_ref})
        for command in self._configuration.commands:
            remaining_ms = max(0, round((deadline - self._clock()) * 1000))
            if remaining_ms < 1:
                self._timed_out = True
                return self._timeout_result(evidence, combined, started_at)
            result = self._process.run(
                [
                    "docker", "exec", "--workdir", DOCKER_WORKSPACE_DESTINATION,
                    self._name, *command,
                ],
                remaining_ms,
            )
            if result is None:
                self._timed_out = True
                return self._timeout_result(evidence, combined, started_at)
            content = f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            combined.append(content)
            evidence.append(
                DockerCommandResult(
                    argv=command,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    exit_code=result.exit_code,
                    duration_ms=result.duration_ms,
                    logs_ref=self._logs.write(content),
                )
            )
            if result.exit_code != 0:
                break
        return DockerExecutionResult(
            commands=tuple(evidence),
            logs_ref=self._logs.write("\n".join(combined)),
            started_at=started_at.isoformat().replace("+00:00", "Z"),
            completed_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            readiness=tuple(readiness),
        )

    def _timeout_result(
        self,
        evidence: list[DockerCommandResult],
        combined: list[str],
        started_at: datetime,
    ) -> DockerTimeoutResult:
        return DockerTimeoutResult(
            commands=tuple(evidence),
            logs_ref=self._logs.write("\n".join(combined)),
            started_at=started_at.isoformat().replace("+00:00", "Z"),
            completed_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        )

    def terminate(self) -> None:
        self._process.run(["docker", "stop", self._name], 5_000)

    def kill(self) -> None:
        self._process.run(["docker", "kill", self._name], 5_000)

    def cleanup(self) -> None:
        result = self._process.run(["docker", "rm", "-f", self._name], 5_000)
        if result is None:
            raise RuntimeError("Docker container cleanup timed out")
        if result.exit_code != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
            raise RuntimeError(
                f"Docker container cleanup failed (exit {result.exit_code}): {detail}"
            )


class DockerValidationAdapter(ToolAdapter):
    """Translate Docker validation requests into normalized Tool evidence."""

    def __init__(self, executor: DockerExecutor, authorized_workspace_root: Path) -> None:
        self._executor = executor
        self._workspace_root = authorized_workspace_root.resolve(strict=True)
        if not self._workspace_root.is_dir():
            raise ValueError("authorized_workspace_root must be a directory")

    def start(self, request: ToolRequest) -> ToolExecution:
        if DOCKER_RUN_CAPABILITY not in request.capabilities:
            raise DockerStartupError(
                "Docker validation requires the docker.run capability"
            )
        value = request.input
        mount = value["workspaceMount"]
        if mount["containerPath"] != DOCKER_WORKSPACE_DESTINATION:
            raise DockerStartupError(
                f"containerPath must be {DOCKER_WORKSPACE_DESTINATION}"
            )
        try:
            host_path = Path(mount["hostPath"]).resolve(strict=True)
            host_path.relative_to(self._workspace_root)
        except (OSError, ValueError) as error:
            raise DockerStartupError(
                "workspace mount must resolve within the authorized workspace root"
            ) from error
        if not host_path.is_dir():
            raise DockerStartupError("workspace mount hostPath must be a directory")
        resources = value["resources"]
        configuration = DockerRunConfiguration(
            image=value["image"],
            required_executables=tuple(
                (tuple(item["argv"]), item["versionPattern"])
                for item in value["requiredExecutables"]
            ),
            commands=tuple(tuple(command["argv"]) for command in value["commands"]),
            workspace_mount=DockerWorkspaceMount(
                host_path=str(host_path),
                container_path=mount["containerPath"],
                read_only=mount["readOnly"],
            ),
            timeout_ms=request.timeout_ms,
            resources=DockerResourceSettings(
                cpu_limit=resources["cpuLimit"],
                memory_bytes=resources["memoryBytes"],
            ),
        )
        try:
            execution = self._executor.start(configuration)
        except Exception as error:
            message = (
                str(error)
                if isinstance(error, DockerStartupError)
                else f"Docker startup failed: {str(error) or type(error).__name__}"
            )
            try:
                self._executor.cleanup_startup()
            except Exception as cleanup_error:
                message += (
                    "; startup cleanup failed: "
                    f"{str(cleanup_error) or type(cleanup_error).__name__}"
                )
            raise DockerStartupError(message) from error
        return _DockerToolExecution(configuration, execution)


class _DockerToolExecution(ToolExecution):
    def __init__(
        self, configuration: DockerRunConfiguration, execution: DockerExecution
    ) -> None:
        self._configuration = configuration
        self._execution = execution

    def wait(self, timeout_ms: int) -> ToolResult | None:
        try:
            outcome = self._execution.wait(timeout_ms)
        except DockerImageReadinessError as error:
            timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            return ToolResult(
                status=ToolResultStatus.FAILED,
                output=None,
                logs_ref=None,
                metrics=ToolMetrics(duration_ms=0),
                started_at=timestamp,
                completed_at=timestamp,
                failure_class=ToolFailureClass.CONFIGURATION,
                failure_message=str(error),
            )
        if outcome is None:
            return None
        timed_out = isinstance(outcome, DockerTimeoutResult)
        expected = self._configuration.commands
        actual = tuple(command.argv for command in outcome.commands)
        failed = next(
            (command for command in outcome.commands if command.exit_code != 0), None
        )
        if actual != expected[: len(actual)] or (
            not timed_out
            and failed is None
            and len(actual) != len(expected)
        ):
            raise ValueError(
                "Docker executor command evidence does not match requested commands"
            )
        readiness = tuple(getattr(outcome, "readiness", ()))
        if not timed_out:
            try:
                _validate_readiness_evidence(
                    readiness, self._configuration.required_executables
                )
            except DockerImageReadinessError as error:
                return ToolResult(
                    status=ToolResultStatus.FAILED,
                    output=None,
                    logs_ref=outcome.logs_ref,
                    metrics=ToolMetrics(
                        duration_ms=sum(item.duration_ms for item in outcome.commands)
                    ),
                    started_at=outcome.started_at,
                    completed_at=outcome.completed_at,
                    failure_class=ToolFailureClass.CONFIGURATION,
                    failure_message=str(error),
                )
        command_records = [command.as_record() for command in outcome.commands]
        duration_ms = sum(command.duration_ms for command in outcome.commands)
        return ToolResult(
            status=(
                ToolResultStatus.TIMED_OUT
                if timed_out
                else ToolResultStatus.FAILED
                if failed is not None
                else ToolResultStatus.SUCCEEDED
            ),
            output={
                "image": self._configuration.image,
                "workspaceMount": self._configuration.workspace_mount.as_record(),
                "readiness": {"status": "PASS", "executables": list(readiness)},
                "commands": command_records,
            },
            logs_ref=outcome.logs_ref,
            metrics=ToolMetrics(duration_ms=duration_ms),
            started_at=outcome.started_at,
            completed_at=outcome.completed_at,
            failure_class=(
                ToolFailureClass.TIMEOUT
                if timed_out
                else ToolFailureClass.NONZERO_EXIT
                if failed is not None
                else None
            ),
            failure_message=(
                f"adapter exceeded timeout of {timeout_ms}ms"
                if timed_out
                else f"command exited with code {failed.exit_code}: "
                f"{' '.join(failed.argv)}"
                if failed is not None
                else None
            ),
        )

    def terminate(self) -> None:
        self._execution.terminate()

    def kill(self) -> None:
        self._execution.kill()

    def cleanup(self) -> None:
        self._execution.cleanup()


def _validate_readiness_evidence(
    evidence: Sequence[Mapping[str, Any]],
    expected: Sequence[tuple[tuple[str, ...], str]],
) -> None:
    """Fail closed when an executor cannot prove the configured image checks."""

    if len(evidence) != len(expected):
        raise DockerImageReadinessError(
            "Docker executor readiness evidence does not match configured prerequisites"
        )
    for item, (argv, pattern) in zip(evidence, expected, strict=True):
        if not isinstance(item, Mapping):
            raise DockerImageReadinessError("Docker executor readiness evidence is invalid")
        output = item.get("output")
        logs_ref = item.get("logsRef")
        if (
            tuple(item.get("argv", ())) != argv
            or item.get("versionPattern") != pattern
            or not isinstance(output, str)
            or re.search(pattern, output) is None
            or not isinstance(logs_ref, str)
            or not logs_ref
        ):
            raise DockerImageReadinessError(
                "Docker executor readiness evidence does not satisfy configured prerequisites"
            )
