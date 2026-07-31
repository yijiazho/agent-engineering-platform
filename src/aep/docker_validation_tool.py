"""Docker-backed deterministic build and test execution."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
import subprocess
from time import monotonic
from typing import Any
from uuid import uuid4

from aep.tool_runtime import (
    JsonSchemaToolValidator,
    ToolAdapter,
    ToolAdapterError,
    ToolExecution,
    ToolFailureClass,
    ToolMetrics,
    ToolRequest,
    ToolResult,
    ToolResultStatus,
)


DOCKER_RUN_CAPABILITY = "docker.run"
DOCKER_WORKSPACE_DESTINATION = "/workspace"
IMAGE_DIGEST_PATTERN = r"^[A-Za-z0-9._/-]+@sha256:[0-9a-f]{64}$"

DOCKER_VALIDATION_INPUT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["image", "commands", "workspaceMount", "resources"],
    "properties": {
        "image": {"type": "string", "pattern": IMAGE_DIGEST_PATTERN},
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
    "required": ["image", "workspaceMount", "commands"],
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
        "commands": {
            "type": "array",
            "minItems": 1,
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
    commands: tuple[tuple[str, ...], ...]
    workspace_mount: DockerWorkspaceMount
    timeout_ms: int
    resources: DockerResourceSettings

    def __post_init__(self) -> None:
        commands = tuple(tuple(command) for command in self.commands)
        if not self.image or not commands:
            raise ValueError("Docker image and commands must not be empty")
        if any(
            not command or any(not value for value in command)
            for command in commands
        ):
            raise ValueError("Docker command arguments must not be empty")
        if self.timeout_ms < 1:
            raise ValueError("Docker timeout must be positive")
        object.__setattr__(self, "commands", commands)


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
        object.__setattr__(self, "commands", commands)


class DockerStartupError(ToolAdapterError):
    """Raised when the Docker sandbox cannot be provisioned."""

    failure_class = ToolFailureClass.STARTUP


class DockerExecution(ABC):
    """Injectable Docker lifecycle controlled by the shared Tool Runtime."""

    @abstractmethod
    def wait(self, timeout_ms: int) -> DockerExecutionResult | None:
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
        self, process: DockerProcessBoundary, logs: DockerLogStore
    ) -> None:
        self._process = process
        self._logs = logs

    def start(self, configuration: DockerRunConfiguration) -> DockerExecution:
        container_name = f"aep-validation-{uuid4().hex}"
        deadline = monotonic() + configuration.timeout_ms / 1000

        def remaining_ms() -> int:
            return max(1, round((deadline - monotonic()) * 1000))

        mount = configuration.workspace_mount
        mount_spec = (
            f"type=bind,src={mount.host_path},dst={mount.container_path}"
            + (",readonly" if mount.read_only else "")
        )
        create = [
            "docker", "create", "--name", container_name,
            "--cpus", str(configuration.resources.cpu_limit),
            "--memory", str(configuration.resources.memory_bytes),
            "--mount", mount_spec, configuration.image, "sleep", "infinity",
        ]
        try:
            created = self._process.run(create, remaining_ms())
            if created is None:
                raise DockerStartupError("Docker create timed out")
            if created.exit_code != 0:
                raise DockerStartupError(
                    f"Docker create failed: {created.stderr.strip()}"
                )
            started = self._process.run(
                ["docker", "start", container_name], remaining_ms()
            )
            if started is None:
                raise DockerStartupError("Docker start timed out")
            if started.exit_code != 0:
                raise DockerStartupError(
                    f"Docker start failed: {started.stderr.strip()}"
                )
        except Exception:
            self._process.run(
                ["docker", "rm", "-f", container_name],
                min(configuration.timeout_ms, 5_000),
            )
            raise
        return _DockerCliExecution(
            configuration, container_name, self._process, self._logs
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
    ) -> None:
        self._configuration = configuration
        self._name = container_name
        self._process = process
        self._logs = logs
        self._timed_out = False

    def wait(self, timeout_ms: int) -> DockerExecutionResult | None:
        if self._timed_out:
            return None
        started_at = datetime.now(UTC)
        deadline = monotonic() + timeout_ms / 1000
        evidence: list[DockerCommandResult] = []
        combined: list[str] = []
        for command in self._configuration.commands:
            remaining_ms = max(0, round((deadline - monotonic()) * 1000))
            if remaining_ms < 1:
                self._timed_out = True
                return None
            result = self._process.run(
                ["docker", "exec", self._name, *command], remaining_ms
            )
            if result is None:
                self._timed_out = True
                return None
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
        )

    def terminate(self) -> None:
        self._process.run(["docker", "stop", self._name], 5_000)

    def kill(self) -> None:
        self._process.run(["docker", "kill", self._name], 5_000)

    def cleanup(self) -> None:
        result = self._process.run(["docker", "rm", "-f", self._name], 5_000)
        if result is None or result.exit_code != 0:
            raise RuntimeError("Docker container cleanup failed")


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
        outcome = self._execution.wait(timeout_ms)
        if outcome is None:
            return None
        expected = self._configuration.commands
        actual = tuple(command.argv for command in outcome.commands)
        failed = next(
            (command for command in outcome.commands if command.exit_code != 0), None
        )
        if actual != expected[: len(actual)] or (
            failed is None and len(actual) != len(expected)
        ):
            raise ValueError(
                "Docker executor command evidence does not match requested commands"
            )
        command_records = [command.as_record() for command in outcome.commands]
        duration_ms = sum(command.duration_ms for command in outcome.commands)
        return ToolResult(
            status=(
                ToolResultStatus.FAILED
                if failed is not None
                else ToolResultStatus.SUCCEEDED
            ),
            output={
                "image": self._configuration.image,
                "workspaceMount": self._configuration.workspace_mount.as_record(),
                "commands": command_records,
            },
            logs_ref=outcome.logs_ref,
            metrics=ToolMetrics(duration_ms=duration_ms),
            started_at=outcome.started_at,
            completed_at=outcome.completed_at,
            failure_class=(
                ToolFailureClass.NONZERO_EXIT if failed is not None else None
            ),
            failure_message=(
                f"command exited with code {failed.exit_code}: "
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
