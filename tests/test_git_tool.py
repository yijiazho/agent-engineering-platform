from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import subprocess

import pytest

from aep.git_tool import (
    GitToolAdapter,
    GitToolContractError,
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
        ("git", *arguments),
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return result.stdout.decode("utf-8").strip()


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
) -> GitToolAdapter:
    return GitToolAdapter(
        repository=repository,
        repository_id="example/repository",
        expected_revision=revision,
        working_branch="agent/work",
        remote=remote,
        log_store=logs,
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
    assert result.status is ToolResultStatus.SUCCEEDED
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
    assert result.logs_ref is not None
    assert "switch" in logs.get(result.logs_ref)


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
    assert result.output["pushed"] is True
    assert git(remote, "rev-parse", "refs/heads/agent/work") == revision


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
        )
