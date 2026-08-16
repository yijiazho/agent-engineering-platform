import json
import os
from pathlib import Path
import subprocess

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
    DockerCliExecutor,
    DockerLogStore,
    DockerProcessBoundary,
    DockerProcessResult,
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


def request(
    *,
    capabilities: tuple[str, ...] = ("docker.run",),
    workspace_host: Path | None = None,
    image: str | None = None,
    container_path: str | None = None,
    commands: list[dict] | None = None,
) -> ToolRequest:
    value = json.loads(FIXTURE.read_text(encoding="utf-8"))
    value["input"]["workspaceMount"]["hostPath"] = str(workspace_host or Path.cwd())
    if image is not None:
        value["input"]["image"] = image
    if container_path is not None:
        value["input"]["workspaceMount"]["containerPath"] = container_path
    if commands is not None:
        value["input"]["commands"] = commands
    return ToolRequest(
        tool_ref=value["toolRef"],
        input=value["input"],
        caller=ToolCaller(**value["caller"]),
        capabilities=capabilities,
        timeout_ms=value["timeoutMs"],
        correlation={
            "traceId": value["traceId"],
            "workflowExecutionId": "workflowexecution-000000000001",
            "taskExecutionId": value["caller"]["id"],
        },
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
        readiness=(
            {"argv": ["python", "--version"], "versionPattern": "^Python 3\\.12\\.", "output": "Python 3.12.9", "logsRef": "sha256:" + "d" * 64},
            {"argv": ["git", "--version"], "versionPattern": "^git version 2\\.", "output": "git version 2.43.0", "logsRef": "sha256:" + "e" * 64},
        ),
    )


class FakeDockerExecution(DockerExecution):
    def __init__(
        self,
        result: DockerExecutionResult | None,
        cleanup_error: Exception | None = None,
    ) -> None:
        self.result = result
        self.cleanup_error = cleanup_error
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
        if self.cleanup_error is not None:
            raise self.cleanup_error


class FakeDockerExecutor(DockerExecutor):
    def __init__(
        self,
        result: DockerExecutionResult | None = None,
        *,
        startup_error: Exception | None = None,
        cleanup_error: Exception | None = None,
    ) -> None:
        self.execution = FakeDockerExecution(result, cleanup_error)
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


class FakeProcessBoundary(DockerProcessBoundary):
    def __init__(
        self,
        results: list[DockerProcessResult | None],
        *,
        clock=None,
        advances: list[float] | None = None,
        readiness_results: dict[tuple[str, ...], DockerProcessResult | None] | None = None,
    ) -> None:
        self.results = list(results)
        self.calls: list[tuple[tuple[str, ...], int]] = []
        self.clock = clock
        self.advances = list(advances or [])
        self.readiness_results = dict(readiness_results or {})

    def run(self, argv, timeout_ms):
        # Readiness probes are an image contract, not the command sequence
        # asserted by the legacy lifecycle fixtures below.
        readiness_argv = tuple(argv[-2:])
        if readiness_argv in self.readiness_results:
            return self.readiness_results[readiness_argv]
        if readiness_argv == ("python", "--version"):
            return DockerProcessResult("Python 3.12.9\n", "", 0, 1)
        if readiness_argv == ("git", "--version"):
            return DockerProcessResult("git version 2.43.0\n", "", 0, 1)
        self.calls.append((tuple(argv), timeout_ms))
        if self.advances:
            self.clock.value += self.advances.pop(0)
        return self.results.pop(0)


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class FakeLogStore(DockerLogStore):
    def __init__(self) -> None:
        self.contents: list[str] = []

    def write(self, content: str) -> str:
        self.contents.append(content)
        return "sha256:" + f"{len(self.contents):064x}"


def invoke(executor: DockerExecutor, *, authorize=lambda _: True, tool_request=None):
    return invoke_tool(
        tool_request or request(),
        validator=docker_validation_validator(),
        authorize=authorize,
        adapter=DockerValidationAdapter(executor, Path.cwd()),
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
    assert "@sha256:" in configuration.image
    assert configuration.timeout_ms == 30_000
    assert configuration.workspace_mount.container_path == "/workspace"
    assert configuration.workspace_mount.read_only is False
    assert configuration.resources.cpu_limit == 2
    assert configuration.resources.memory_bytes == 536_870_912
    assert executor.execution.cleaned_up is True


def test_cleanup_failure_after_success_preserves_docker_evidence() -> None:
    executor = FakeDockerExecutor(
        outcome(), cleanup_error=RuntimeError("remove failed")
    )

    result = invoke(executor)

    assert result.status is ToolResultStatus.FAILED
    assert result.failure_class is ToolFailureClass.ADAPTER
    assert result.failure_message == "execution cleanup failed: remove failed"
    assert result.output["commands"][0]["stdout"] == "1 passed\n"
    assert result.logs_ref == "sha256:" + "c" * 64
    assert result.metrics.duration_ms == 25
    assert result.started_at == "2026-07-30T00:00:00Z"
    assert result.completed_at == "2026-07-30T00:00:00.025Z"


def test_concrete_cli_executor_builds_scoped_container_and_evidence() -> None:
    ok = DockerProcessResult("", "", 0, 2)
    process = FakeProcessBoundary(
        [
            ok,
            ok,
            DockerProcessResult("passed\n", "", 0, 18),
            ok,
        ]
    )
    logs = FakeLogStore()
    result = invoke(DockerCliExecutor(process, logs))

    assert result.status is ToolResultStatus.SUCCEEDED
    create = process.calls[0][0]
    assert create[:3] == ("docker", "create", "--name")
    assert create[3].startswith("aep-validation-")
    assert create[4:6] == ("--network", "none")
    assert "--cpus" in create and "--memory" in create and "--mount" in create
    assert str(Path.cwd().resolve()) in create[create.index("--mount") + 1]
    assert process.calls[1][0] == ("docker", "start", create[3])
    assert process.calls[2][0] == (
        "docker", "exec", "--workdir", "/workspace",
        create[3], "python", "-m", "pytest"
    )
    assert process.calls[3][0] == ("docker", "rm", "-f", create[3])
    assert result.output["commands"][0]["stdout"] == "passed\n"
    assert len(logs.contents) == 4


def test_concrete_cli_executor_timeout_is_stopped_killed_and_removed() -> None:
    ok = DockerProcessResult("", "", 0, 1)
    process = FakeProcessBoundary([ok, ok, None, ok, ok, ok])

    result = invoke(DockerCliExecutor(process, FakeLogStore()))

    assert result.status is ToolResultStatus.TIMED_OUT
    name = process.calls[0][0][3]
    assert process.calls[3][0] == ("docker", "stop", name)
    assert process.calls[4][0] == ("docker", "kill", name)
    assert process.calls[5][0] == ("docker", "rm", "-f", name)


def test_create_start_and_commands_share_one_absolute_deadline() -> None:
    clock = FakeClock()
    ok = DockerProcessResult("", "", 0, 1)
    process = FakeProcessBoundary(
        [ok, ok, ok, ok],
        clock=clock,
        advances=[10.0, 10.0, 0.0, 0.0],
    )

    result = invoke(DockerCliExecutor(process, FakeLogStore(), clock=clock))

    assert result.status is ToolResultStatus.SUCCEEDED
    assert [call[1] for call in process.calls[:3]] == [30_000, 20_000, 10_000]


def test_later_command_timeout_preserves_completed_evidence_and_logs() -> None:
    ok = DockerProcessResult("", "", 0, 1)
    first = DockerProcessResult("built\n", "", 0, 8)
    process = FakeProcessBoundary([ok, ok, first, None, ok, ok, ok])
    logs = FakeLogStore()
    tool_request = request(
        commands=[
            {"argv": ["python", "-m", "build"]},
            {"argv": ["python", "-m", "pytest"]},
        ]
    )

    result = invoke(
        DockerCliExecutor(process, logs),
        tool_request=tool_request,
    )

    assert result.status is ToolResultStatus.TIMED_OUT
    assert result.failure_class is ToolFailureClass.TIMEOUT
    assert len(result.output["commands"]) == 1
    assert result.output["commands"][0]["argv"] == ("python", "-m", "build")
    assert result.output["commands"][0]["stdout"] == "built\n"
    assert result.logs_ref == "sha256:" + f"{4:064x}"
    with pytest.raises(TypeError):
        result.output["commands"][0]["stdout"] = "changed"
    name = process.calls[0][0][3]
    assert ("docker", "stop", name) in [call[0] for call in process.calls]
    assert ("docker", "kill", name) in [call[0] for call in process.calls]
    assert process.calls[-1][0] == ("docker", "rm", "-f", name)


def test_cleanup_failure_after_partial_timeout_preserves_evidence() -> None:
    ok = DockerProcessResult("", "", 0, 1)
    first = DockerProcessResult("built\n", "", 0, 8)
    remove_failed = DockerProcessResult("", "remove failed", 1, 1)
    process = FakeProcessBoundary(
        [ok, ok, first, None, ok, ok, remove_failed]
    )

    result = invoke(
        DockerCliExecutor(process, FakeLogStore()),
        tool_request=request(
            commands=[
                {"argv": ["python", "-m", "build"]},
                {"argv": ["python", "-m", "pytest"]},
            ]
        ),
    )

    assert result.status is ToolResultStatus.FAILED
    assert result.failure_class is ToolFailureClass.ADAPTER
    assert result.failure_message == (
        "adapter exceeded timeout of 30000ms; "
        "execution cleanup failed: Docker container cleanup failed (exit 1): remove failed"
    )
    assert len(result.output["commands"]) == 1
    assert result.output["commands"][0]["stdout"] == "built\n"
    assert result.logs_ref == "sha256:" + f"{4:064x}"
    assert result.metrics.duration_ms == 8


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


def test_missing_declared_image_executable_is_configuration_failure() -> None:
    ok = DockerProcessResult("", "", 0, 1)
    process = FakeProcessBoundary(
        [ok, ok, ok],
        readiness_results={
            ("git", "--version"): DockerProcessResult("", "git: not found", 127, 1)
        },
    )

    result = invoke(DockerCliExecutor(process, FakeLogStore()))

    assert result.status is ToolResultStatus.FAILED
    assert result.failure_class is ToolFailureClass.CONFIGURATION
    assert "prerequisite 'git' is unavailable" in result.failure_message
    assert [call[0][0:2] for call in process.calls] == [
        ("docker", "create"), ("docker", "start"), ("docker", "rm")
    ]


def test_invalid_readiness_pattern_is_configuration_failure() -> None:
    executor = FakeDockerExecutor(outcome())
    tool_request = request()
    malformed = dict(tool_request.input)
    malformed["requiredExecutables"] = [
        {"argv": ["python", "--version"], "versionPattern": "["}
    ]
    tool_request = ToolRequest(
        tool_ref=tool_request.tool_ref,
        input=malformed,
        caller=tool_request.caller,
        capabilities=tool_request.capabilities,
        timeout_ms=tool_request.timeout_ms,
        correlation=tool_request.correlation,
    )

    result = invoke(executor, tool_request=tool_request)

    assert result.failure_class is ToolFailureClass.CONFIGURATION
    assert executor.configurations == []


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
    "image",
    ["python:3.12", "python", "sha256:" + "a" * 64, "python@sha256:ABC"],
)
def test_image_must_be_digest_pinned(image: str) -> None:
    executor = FakeDockerExecutor(outcome())
    result = invoke(executor, tool_request=request(image=image))

    assert result.failure_class is ToolFailureClass.VALIDATION
    assert executor.configurations == []


def test_mount_outside_authorized_workspace_is_denied() -> None:
    executor = FakeDockerExecutor(outcome())
    result = invoke(
        executor,
        tool_request=request(workspace_host=Path.cwd().parent),
    )

    assert result.failure_class is ToolFailureClass.STARTUP
    assert executor.configurations == []


def test_noncanonical_container_destination_is_denied() -> None:
    executor = FakeDockerExecutor(outcome())
    result = invoke(
        executor,
        tool_request=request(container_path="/host"),
    )

    assert result.failure_class is ToolFailureClass.STARTUP
    assert executor.configurations == []


def test_symlink_escape_from_authorized_workspace_is_denied(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        if os.name != "nt":
            pytest.skip(f"directory symlinks unavailable: {error}")
        junction = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            capture_output=True,
            text=True,
            check=False,
        )
        if junction.returncode != 0:
            pytest.skip(f"directory links unavailable: {junction.stderr}")
    executor = FakeDockerExecutor(outcome())

    result = invoke_tool(
        request(workspace_host=link),
        validator=docker_validation_validator(),
        authorize=lambda _: True,
        adapter=DockerValidationAdapter(executor, root),
    )

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
    raw["input"]["workspaceMount"]["hostPath"] = str(Path.cwd())
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
        correlation={
            "traceId": raw["traceId"],
            "workflowExecutionId": "workflowexecution-000000000001",
            "taskExecutionId": raw["caller"]["id"],
        },
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
