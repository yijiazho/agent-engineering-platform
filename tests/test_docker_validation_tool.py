import json
from pathlib import Path

import pytest

from aep.capability_policy import (
    ApplicablePolicy,
    PolicyScope,
    PreExecutionCapabilityPolicy,
)
from aep.docker_validation_tool import (
    DockerCommandResult,
    DockerExecution,
    DockerExecutionResult,
    DockerExecutor,
    DockerRunConfiguration,
    DockerValidationAdapter,
    docker_validation_validator,
)
from aep.runtime_store import InMemoryRuntimeObjectStore
from aep.tool_runtime import (
    ToolCaller,
    ToolFailureClass,
    ToolRequest,
    ToolResultStatus,
    invoke_tool,
)


FIXTURE = (
    Path(__file__).parents[1] / "fixtures" / "docker-validation" / "request.json"
)


def request(*, capabilities: tuple[str, ...] = ("docker.run",)) -> ToolRequest:
    value = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return ToolRequest(
        tool_ref=value["toolRef"],
        input=value["input"],
        caller=ToolCaller(**value["caller"]),
        capabilities=capabilities,
        timeout_ms=value["timeoutMs"],
        trace_id=value["traceId"],
    )


def command(*, exit_code: int = 0) -> DockerCommandResult:
    return DockerCommandResult(
        argv=("python", "-m", "pytest"),
        stdout="1 passed\n" if exit_code == 0 else "",
        stderr="" if exit_code == 0 else "1 failed\n",
        exit_code=exit_code,
        duration_ms=25,
        logs_ref=f"sha256:{'a' if exit_code == 0 else 'b'}" + "0" * 63,
    )


def outcome(*, exit_code: int = 0) -> DockerExecutionResult:
    return DockerExecutionResult(
        commands=(command(exit_code=exit_code),),
        logs_ref="sha256:" + "c" * 64,
        started_at="2026-07-30T00:00:00Z",
        completed_at="2026-07-30T00:00:00.025Z",
    )


class FakeDockerExecution(DockerExecution):
    def __init__(self, result: DockerExecutionResult | None) -> None:
        self.result = result
        self.wait_timeouts: list[int] = []
        self.terminated = False
        self.killed = False
        self.cleaned_up = False

    def wait(self, timeout_ms: int) -> DockerExecutionResult | None:
        self.wait_timeouts.append(timeout_ms)
        return self.result

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def cleanup(self) -> None:
        self.cleaned_up = True


class FakeDockerExecutor(DockerExecutor):
    def __init__(
        self,
        result: DockerExecutionResult | None = None,
        *,
        startup_error: Exception | None = None,
    ) -> None:
        self.execution = FakeDockerExecution(result)
        self.startup_error = startup_error
        self.configurations: list[DockerRunConfiguration] = []
        self.startup_cleaned_up = False

    def start(self, configuration: DockerRunConfiguration) -> DockerExecution:
        self.configurations.append(configuration)
        if self.startup_error is not None:
            raise self.startup_error
        return self.execution

    def cleanup_startup(self) -> None:
        self.startup_cleaned_up = True


def invoke(executor: DockerExecutor, *, authorize=lambda _: True, tool_request=None):
    return invoke_tool(
        tool_request or request(),
        validator=docker_validation_validator(),
        authorize=authorize,
        adapter=DockerValidationAdapter(executor),
    )


def test_pass_captures_configuration_and_command_evidence() -> None:
    executor = FakeDockerExecutor(outcome())

    result = invoke(executor)

    assert result.status is ToolResultStatus.SUCCEEDED
    assert result.failure_class is None
    assert result.logs_ref == "sha256:" + "c" * 64
    assert result.metrics.duration_ms == 25
    assert result.output["commands"][0] == {
        "argv": ("python", "-m", "pytest"),
        "stdout": "1 passed\n",
        "stderr": "",
        "exitCode": 0,
        "durationMs": 25,
        "logsRef": "sha256:" + "a" + "0" * 63,
    }
    configuration = executor.configurations[0]
    assert configuration.image.startswith("python@sha256:")
    assert configuration.timeout_ms == 30_000
    assert configuration.workspace_mount.container_path == "/workspace"
    assert configuration.workspace_mount.read_only is False
    assert configuration.resources.cpu_limit == 2
    assert configuration.resources.memory_bytes == 536_870_912
    assert executor.execution.cleaned_up is True


def test_nonzero_exit_is_classified_without_interpreting_acceptance() -> None:
    executor = FakeDockerExecutor(outcome(exit_code=7))

    result = invoke(executor)

    assert result.status is ToolResultStatus.FAILED
    assert result.failure_class is ToolFailureClass.NONZERO_EXIT
    assert result.output["commands"][0]["exitCode"] == 7
    assert result.output["commands"][0]["stderr"] == "1 failed\n"
    assert executor.execution.cleaned_up is True


def test_timeout_terminates_kills_and_cleans_up() -> None:
    executor = FakeDockerExecutor(None)

    result = invoke(executor)

    assert result.status is ToolResultStatus.TIMED_OUT
    assert result.failure_class is ToolFailureClass.TIMEOUT
    assert executor.execution.wait_timeouts == [30_000, 100]
    assert executor.execution.terminated is True
    assert executor.execution.killed is True
    assert executor.execution.cleaned_up is True


def test_policy_denial_prevents_docker_startup() -> None:
    executor = FakeDockerExecutor(outcome())
    store = InMemoryRuntimeObjectStore()
    policy = {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "Policy",
        "metadata": {"name": "deny-docker", "version": "1.0.0"},
        "spec": {
            "type": "pre-execution-capability",
            "rules": [
                {
                    "effect": "deny",
                    "capabilities": ["docker.run"],
                    "reason": "Docker is disabled for this task.",
                }
            ],
        },
    }
    authorize = PreExecutionCapabilityPolicy(store).tool_authorization_boundary(
        task_execution_id="taskexecution-validation123",
        resource_scope={"workspace": "example"},
        execution_context={"purpose": "validation"},
        applicable_policies=[
            ApplicablePolicy(PolicyScope.TOOL, policy),
        ],
        timestamp="2026-07-30T00:00:00Z",
    )

    result = invoke(executor, authorize=authorize)

    assert result.status is ToolResultStatus.DENIED
    assert result.failure_class is ToolFailureClass.POLICY
    assert executor.configurations == []
    decisions = store.list_by_task_execution("taskexecution-validation123")
    assert len(decisions) == 1
    assert decisions[0]["action"] == "docker.run"
    assert decisions[0]["decision"] == "DENY"


def test_startup_failure_is_classified_before_execution() -> None:
    executor = FakeDockerExecutor(startup_error=RuntimeError("daemon unavailable"))

    result = invoke(executor)

    assert result.status is ToolResultStatus.FAILED
    assert result.failure_class is ToolFailureClass.STARTUP
    assert result.failure_message == "Docker startup failed: daemon unavailable"
    assert executor.startup_cleaned_up is True


def test_docker_run_capability_is_required_even_with_an_allowing_hook() -> None:
    executor = FakeDockerExecutor(outcome())

    result = invoke(
        executor,
        authorize=lambda _: True,
        tool_request=request(capabilities=("filesystem.read",)),
    )

    assert result.status is ToolResultStatus.FAILED
    assert result.failure_class is ToolFailureClass.STARTUP
    assert executor.configurations == []


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("image",), ""),
        (("commands",), []),
        (("workspaceMount", "hostPath"), ""),
        (("resources", "cpuLimit"), 0),
        (("resources", "memoryBytes"), 0),
    ],
)
def test_invalid_configuration_is_rejected_before_policy_and_execution(
    path: tuple[str, ...], value
) -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    target = raw["input"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    tool_request = ToolRequest(
        tool_ref=raw["toolRef"],
        input=raw["input"],
        caller=ToolCaller(**raw["caller"]),
        capabilities=raw["capabilities"],
        timeout_ms=raw["timeoutMs"],
        trace_id=raw["traceId"],
    )
    executor = FakeDockerExecutor(outcome())
    authorization_called = False

    def authorize(_: ToolRequest) -> bool:
        nonlocal authorization_called
        authorization_called = True
        return True

    result = invoke(executor, authorize=authorize, tool_request=tool_request)

    assert result.status is ToolResultStatus.FAILED
    assert result.failure_class is ToolFailureClass.VALIDATION
    assert authorization_called is False
    assert executor.configurations == []
