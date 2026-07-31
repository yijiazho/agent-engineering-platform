"""Docker-backed deterministic build and test execution."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

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

DOCKER_VALIDATION_INPUT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["image", "commands", "workspaceMount", "resources"],
    "properties": {
        "image": {"type": "string", "minLength": 1},
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


class DockerValidationAdapter(ToolAdapter):
    """Translate Docker validation requests into normalized Tool evidence."""

    def __init__(self, executor: DockerExecutor) -> None:
        self._executor = executor

    def start(self, request: ToolRequest) -> ToolExecution:
        if DOCKER_RUN_CAPABILITY not in request.capabilities:
            raise DockerStartupError(
                "Docker validation requires the docker.run capability"
            )
        value = request.input
        mount = value["workspaceMount"]
        resources = value["resources"]
        configuration = DockerRunConfiguration(
            image=value["image"],
            commands=tuple(tuple(command["argv"]) for command in value["commands"]),
            workspace_mount=DockerWorkspaceMount(
                host_path=mount["hostPath"],
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
