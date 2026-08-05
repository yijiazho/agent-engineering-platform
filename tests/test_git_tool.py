from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import os
from pathlib import Path
import stat
import subprocess

import pytest

from aep.git_tool import (
    GitToolAdapter,
    GitToolContractError,
    GitSandboxCommandResult,
    GitSandboxTimeout,
    InMemoryGitCommandLogStore,
    git_tool_validator,
)
from aep.tool_runtime import (
    ToolCaller,
    ToolRequest,
    ToolResultStatus,
    invoke_tool,
)


def git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("git", "-c", "safe.bareRepository=all", *arguments),
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return result.stdout.decode("utf-8").strip()


class LocalGitSandbox:
    """Daemon-independent test double for the production isolation boundary."""

    disabled_hooks_path = os.devnull
    null_device_path = os.devnull

    def __init__(self) -> None:
        self.environments: list[dict[str, str]] = []

    def run(
        self,
        *,
        repository: Path,
        arguments: Sequence[str],
        environment: Mapping[str, str],
        timeout_ms: int,
        stdin: bytes | None = None,
    ) -> GitSandboxCommandResult:
        self.environments.append(dict(environment))
        process_environment = dict(environment)
        if os.name == "nt":
            process_environment["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
        try:
            completed = subprocess.run(
                ("git", *arguments),
                cwd=repository,
                env=process_environment,
                stdin=subprocess.DEVNULL if stdin is None else None,
                input=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout_ms / 1000,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise GitSandboxTimeout(
                stdout=error.stdout or b"", stderr=error.stderr or b""
            ) from error
        return GitSandboxCommandResult(
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


class TimeoutGitSandbox(LocalGitSandbox):
    def __init__(self, command: str) -> None:
        super().__init__()
        self._command = command
        self.timed_out = False

    def run(
        self,
        *,
        repository: Path,
        arguments: Sequence[str],
        environment: Mapping[str, str],
        timeout_ms: int,
        stdin: bytes | None = None,
    ) -> GitSandboxCommandResult:
        if self._command in arguments:
            self.environments.append(dict(environment))
            self.timed_out = True
            raise GitSandboxTimeout(stderr=b"command timed out gho_TIMEOUTSECRET")
        return super().run(
            repository=repository,
            arguments=arguments,
            environment=environment,
            timeout_ms=timeout_ms,
            stdin=stdin,
        )


class StubCredentialLease:
    def __init__(self) -> None:
        self.environment = {"AEP_GIT_CREDENTIAL_FILE": "sandbox:/tmp/credential"}
        self.closed = False

    def close(self) -> None:
        self.closed = True


class StubCredentialProvider:
    def __init__(self) -> None:
        self.lease = StubCredentialLease()
        self.requests: list[tuple[str, str]] = []

    def acquire(self, *, remote: str, branch: str) -> StubCredentialLease:
        self.requests.append((remote, branch))
        return self.lease


@pytest.fixture
def local_repository(tmp_path: Path) -> tuple[Path, Path, str]:
    remote = tmp_path / "remote.git"
    subprocess.run(
        ("git", "init", "--bare", str(remote)),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init")
    git(repository, "config", "user.name", "AEP Test")
    git(repository, "config", "user.email", "aep@example.test")
    git(repository, "config", "core.autocrlf", "false")
    git(repository, "config", "commit.gpgsign", "false")
    git(repository, "remote", "add", "origin", str(remote))
    (repository / "tracked.txt").write_text("original\n", encoding="utf-8")
    git(repository, "add", "tracked.txt")
    git(repository, "commit", "-m", "fixture")
    revision = git(repository, "rev-parse", "HEAD")
    return repository, remote, revision


def request(
    revision: str,
    operation: str,
    *,
    branch: str = "agent/work",
    capabilities: tuple[str, ...] = ("git.read",),
) -> ToolRequest:
    return ToolRequest(
        tool_ref={"kind": "Tool", "name": "git", "version": "1.0.0"},
        input={
            "operation": operation,
            "expectedRevision": revision,
            "branch": branch,
        },
        caller=ToolCaller(kind="TaskExecution", id="taskexecution-git00000001"),
        capabilities=capabilities,
        timeout_ms=5_000,
        trace_id=f"trace-{operation}",
    )


def adapter(
    repository: Path,
    revision: str,
    logs: InMemoryGitCommandLogStore,
    *,
    remote: str = "origin",
    sandbox: LocalGitSandbox | None = None,
    credential_provider: StubCredentialProvider | None = None,
) -> GitToolAdapter:
    return GitToolAdapter(
        repository=repository,
        repository_id="example/repository",
        expected_revision=revision,
        working_branch="agent/work",
        remote=remote,
        log_store=logs,
        sandbox=sandbox or LocalGitSandbox(),
        credential_provider=credential_provider,
    )


def invoke(
    tool_request: ToolRequest,
    tool_adapter: GitToolAdapter,
    authorize: Callable[[ToolRequest], bool] = lambda _request: True,
):
    return invoke_tool(
        tool_request,
        validator=git_tool_validator(),
        authorize=authorize,
        adapter=tool_adapter,
    )


def create_branch(
    repository: Path, revision: str, logs: InMemoryGitCommandLogStore
) -> GitToolAdapter:
    tool_adapter = adapter(repository, revision, logs)
    result = invoke(request(revision, "create_branch"), tool_adapter)
    assert result.status is ToolResultStatus.SUCCEEDED, logs.get(result.logs_ref)
    return tool_adapter


def test_create_branch_is_bound_to_configured_revision_and_branch(
    local_repository: tuple[Path, Path, str],
) -> None:
    repository, _remote, revision = local_repository
    logs = InMemoryGitCommandLogStore()

    result = invoke(
        request(revision, "create_branch"), adapter(repository, revision, logs)
    )

    assert result.status is ToolResultStatus.SUCCEEDED
    assert result.output["branch"] == "agent/work"
    assert result.output["revision"] == revision
    assert result.output["baseRevision"] == revision
    assert result.output["changedFiles"] == ()
    assert result.output["remoteMutationState"] == "NOT_ATTEMPTED"
    assert result.logs_ref is not None
    assert "switch" in logs.get(result.logs_ref)


@pytest.mark.parametrize("dirty_state", ["modified", "staged", "untracked"])
def test_create_branch_rejects_dirty_index_or_worktree(
    local_repository: tuple[Path, Path, str],
    dirty_state: str,
) -> None:
    repository, _remote, revision = local_repository
    logs = InMemoryGitCommandLogStore()
    if dirty_state == "untracked":
        (repository / "untracked.txt").write_text("dirty\n", encoding="utf-8")
    else:
        (repository / "tracked.txt").write_text("dirty\n", encoding="utf-8")
        if dirty_state == "staged":
            git(repository, "add", "tracked.txt")

    result = invoke(
        request(revision, "create_branch"), adapter(repository, revision, logs)
    )

    assert result.status is ToolResultStatus.FAILED
    assert result.failure_message == "create_branch requires a clean index and worktree"
    assert git(repository, "branch", "--show-current") != "agent/work"


def test_repository_hooks_and_ambient_host_secrets_are_not_exposed(
    local_repository: tuple[Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _remote, revision = local_repository
    logs = InMemoryGitCommandLogStore()
    marker = repository / "hook-ran"
    hook = repository / ".git" / "hooks" / "post-checkout"
    hook.write_text(
        f"#!/bin/sh\nprintf compromised > '{marker.as_posix()}'\n",
        encoding="utf-8",
    )
    hook.chmod(hook.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("AEP_HOST_SECRET", "gho_AMBIENTSECRET")
    sandbox = LocalGitSandbox()

    result = invoke(
        request(revision, "create_branch"),
        adapter(repository, revision, logs, sandbox=sandbox),
    )

    assert result.status is ToolResultStatus.SUCCEEDED
    assert not marker.exists()
    assert all("AEP_HOST_SECRET" not in value for value in sandbox.environments)
    assert all(
        value["GIT_TERMINAL_PROMPT"] == "0" for value in sandbox.environments
    )


def test_status_returns_structured_changed_files_from_local_repository(
    local_repository: tuple[Path, Path, str],
) -> None:
    repository, _remote, revision = local_repository
    logs = InMemoryGitCommandLogStore()
    tool_adapter = create_branch(repository, revision, logs)
    (repository / "tracked.txt").write_text("changed\n", encoding="utf-8")
    (repository / "new.txt").write_text("new\n", encoding="utf-8")

    result = invoke(request(revision, "status"), tool_adapter)

    assert result.status is ToolResultStatus.SUCCEEDED
    changed_files = {
        item["path"]: dict(item) for item in result.output["changedFiles"]
    }
    assert changed_files == {
        "new.txt": {
            "path": "new.txt",
            "status": "??",
            "indexStatus": "?",
            "worktreeStatus": "?",
        },
        "tracked.txt": {
            "path": "tracked.txt",
            "status": " M",
            "indexStatus": " ",
            "worktreeStatus": "M",
        },
    }
    assert all(
        set(command) == {"arguments", "exitCode", "stdoutBytes", "stderrBytes"}
        for command in result.output["commandResults"]
    )


def test_diff_returns_patch_and_content_metadata(
    local_repository: tuple[Path, Path, str],
) -> None:
    repository, _remote, revision = local_repository
    logs = InMemoryGitCommandLogStore()
    tool_adapter = create_branch(repository, revision, logs)
    (repository / "tracked.txt").write_text("changed\n", encoding="utf-8")
    (repository / "added.txt").write_text("added\n", encoding="utf-8")

    result = invoke(request(revision, "diff"), tool_adapter)

    assert result.status is ToolResultStatus.SUCCEEDED
    assert "diff --git a/tracked.txt b/tracked.txt" in result.output["diff"]["text"]
    assert "diff --git a/added.txt b/added.txt" in result.output["diff"]["text"]
    assert "--- /dev/null" in result.output["diff"]["text"]
    assert result.output["diff"]["byteLength"] > 0
    assert len(result.output["diff"]["sha256"]) == 64
    assert result.output["changedFiles"][0]["path"] == "tracked.txt"


def test_push_branch_requires_policy_authorization_before_remote_mutation(
    local_repository: tuple[Path, Path, str],
) -> None:
    repository, remote, revision = local_repository
    logs = InMemoryGitCommandLogStore()
    tool_adapter = create_branch(repository, revision, logs)
    push_request = request(
        revision, "push_branch", capabilities=("git.push",)
    )

    result = invoke(push_request, tool_adapter, authorize=lambda _request: False)

    assert result.status is ToolResultStatus.DENIED
    assert not (remote / "refs" / "heads" / "agent" / "work").exists()


def test_push_branch_rejects_request_that_omits_git_push_capability(
    local_repository: tuple[Path, Path, str],
) -> None:
    repository, remote, revision = local_repository
    logs = InMemoryGitCommandLogStore()
    tool_adapter = create_branch(repository, revision, logs)

    result = invoke(request(revision, "push_branch"), tool_adapter)

    assert result.status is ToolResultStatus.FAILED
    assert result.failure_message == "push_branch requires the git.push capability"
    assert not (remote / "refs" / "heads" / "agent" / "work").exists()


def test_authorized_push_updates_only_configured_remote_branch(
    local_repository: tuple[Path, Path, str],
) -> None:
    repository, remote, revision = local_repository
    logs = InMemoryGitCommandLogStore()
    tool_adapter = create_branch(repository, revision, logs)

    result = invoke(
        request(revision, "push_branch", capabilities=("git.push",)),
        tool_adapter,
    )

    assert result.status is ToolResultStatus.SUCCEEDED
    assert result.output["remoteMutationState"] == "CONFIRMED"
    assert git(remote, "rev-parse", "refs/heads/agent/work") == revision


def test_push_credentials_are_scoped_to_push_and_lease_is_closed(
    local_repository: tuple[Path, Path, str],
) -> None:
    repository, _remote, revision = local_repository
    logs = InMemoryGitCommandLogStore()
    sandbox = LocalGitSandbox()
    provider = StubCredentialProvider()
    tool_adapter = adapter(
        repository,
        revision,
        logs,
        sandbox=sandbox,
        credential_provider=provider,
    )
    assert invoke(
        request(revision, "create_branch"), tool_adapter
    ).status is ToolResultStatus.SUCCEEDED

    result = invoke(
        request(revision, "push_branch", capabilities=("git.push",)),
        tool_adapter,
    )

    assert result.status is ToolResultStatus.SUCCEEDED
    assert provider.requests == [("origin", "agent/work")]
    assert provider.lease.closed
    credential_environments = [
        value
        for value in sandbox.environments
        if "AEP_GIT_CREDENTIAL_FILE" in value
    ]
    assert credential_environments == [
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "AEP_GIT_CREDENTIAL_FILE": "sandbox:/tmp/credential",
        }
    ]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("expectedRevision", "0" * 40, "configured revision"),
        ("branch", "another/branch", "configured working branch"),
    ],
)
def test_request_cannot_escape_configured_revision_or_branch(
    local_repository: tuple[Path, Path, str],
    field: str,
    value: str,
    message: str,
) -> None:
    repository, _remote, revision = local_repository
    logs = InMemoryGitCommandLogStore()
    value_input = {
        "operation": "create_branch",
        "expectedRevision": revision,
        "branch": "agent/work",
    }
    value_input[field] = value
    invalid_request = ToolRequest(
        tool_ref={"kind": "Tool", "name": "git", "version": "1.0.0"},
        input=value_input,
        caller=ToolCaller(kind="TaskExecution", id="taskexecution-git00000001"),
        capabilities=("git.read",),
        timeout_ms=5_000,
        trace_id="trace-invalid-state",
    )

    result = invoke(invalid_request, adapter(repository, revision, logs))

    assert result.status is ToolResultStatus.FAILED
    assert message in result.failure_message


def test_status_fails_when_checkout_is_not_configured_working_branch(
    local_repository: tuple[Path, Path, str],
) -> None:
    repository, _remote, revision = local_repository
    logs = InMemoryGitCommandLogStore()

    result = invoke(
        request(revision, "status"), adapter(repository, revision, logs)
    )

    assert result.status is ToolResultStatus.FAILED
    assert "requires branch 'agent/work'" in result.failure_message
    assert result.logs_ref is not None


def test_status_rejects_working_branch_with_unrelated_history(
    local_repository: tuple[Path, Path, str],
) -> None:
    repository, _remote, revision = local_repository
    logs = InMemoryGitCommandLogStore()
    git(repository, "switch", "--orphan", "agent/work")
    (repository / "unrelated.txt").write_text("unrelated\n", encoding="utf-8")
    git(repository, "add", "unrelated.txt")
    git(repository, "commit", "-m", "unrelated history")

    result = invoke(
        request(revision, "status"), adapter(repository, revision, logs)
    )

    assert result.status is ToolResultStatus.FAILED
    assert result.failure_message == (
        "configured expected revision is not an ancestor of HEAD"
    )


def test_read_timeout_persists_redacted_command_evidence(
    local_repository: tuple[Path, Path, str],
) -> None:
    repository, _remote, revision = local_repository
    logs = InMemoryGitCommandLogStore()
    create_branch(repository, revision, logs)
    sandbox = TimeoutGitSandbox("status")

    result = invoke(
        request(revision, "status"),
        adapter(repository, revision, logs, sandbox=sandbox),
    )

    assert result.status is ToolResultStatus.TIMED_OUT
    assert result.failure_class.value == "TIMEOUT"
    assert sandbox.timed_out
    assert result.logs_ref is not None
    assert "TIMEOUTSECRET" not in logs.get(result.logs_ref)
    assert "[REDACTED]" in logs.get(result.logs_ref)


def test_push_timeout_persists_evidence_without_remote_ref(
    local_repository: tuple[Path, Path, str],
) -> None:
    repository, remote, revision = local_repository
    logs = InMemoryGitCommandLogStore()
    create_branch(repository, revision, logs)
    sandbox = TimeoutGitSandbox("push")

    result = invoke(
        request(revision, "push_branch", capabilities=("git.push",)),
        adapter(repository, revision, logs, sandbox=sandbox),
    )

    assert result.status is ToolResultStatus.TIMED_OUT
    assert result.failure_class.value == "TIMEOUT"
    assert sandbox.timed_out
    assert result.logs_ref is not None
    assert result.output["remoteMutationState"] == "UNKNOWN"
    assert result.output["commandResults"][-1]["exitCode"] == -1
    assert not (remote / "refs" / "heads" / "agent" / "work").exists()


def test_confirmed_push_state_survives_post_push_evidence_timeout(
    local_repository: tuple[Path, Path, str],
) -> None:
    repository, remote, revision = local_repository
    logs = InMemoryGitCommandLogStore()
    create_branch(repository, revision, logs)
    sandbox = TimeoutGitSandbox("status")

    result = invoke(
        request(revision, "push_branch", capabilities=("git.push",)),
        adapter(repository, revision, logs, sandbox=sandbox),
    )

    assert result.status is ToolResultStatus.TIMED_OUT
    assert result.failure_class.value == "TIMEOUT"
    assert result.output["remoteMutationState"] == "CONFIRMED"
    assert git(remote, "rev-parse", "refs/heads/agent/work") == revision


def test_command_failure_is_structured_and_command_logs_are_redacted(
    local_repository: tuple[Path, Path, str],
) -> None:
    repository, _remote, revision = local_repository
    logs = InMemoryGitCommandLogStore()
    tool_adapter = create_branch(repository, revision, logs)
    secret_remote = "gho_SUPERSECRET"
    git(repository, "remote", "add", secret_remote, "missing-local-repository")
    failing_adapter = adapter(
        repository, revision, logs, remote=secret_remote
    )

    result = invoke(
        request(revision, "push_branch", capabilities=("git.push",)),
        failing_adapter,
    )

    assert result.status is ToolResultStatus.FAILED
    assert result.failure_class.value == "ADAPTER"
    assert result.output["remoteMutationState"] == "UNKNOWN"
    assert result.output["commandResults"][-1]["exitCode"] != 0
    assert result.logs_ref is not None
    assert "SUPERSECRET" not in result.failure_message
    assert "SUPERSECRET" not in logs.get(result.logs_ref)
    assert "[REDACTED]" in logs.get(result.logs_ref)


def test_adapter_rejects_non_repository_and_unsafe_configuration(
    tmp_path: Path,
) -> None:
    logs = InMemoryGitCommandLogStore()
    with pytest.raises(GitToolContractError, match="Git worktree"):
        GitToolAdapter(
            repository=tmp_path,
            repository_id="example/repository",
            expected_revision="0" * 40,
            working_branch="agent/work",
            log_store=logs,
            sandbox=LocalGitSandbox(),
        )
