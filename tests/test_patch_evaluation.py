from __future__ import annotations

from collections.abc import Mapping, Sequence
from hashlib import sha256
import os
from pathlib import Path
import subprocess

import pytest

from aep.git_tool import (
    _decode_patch_path,
    GitSandboxCommandResult,
    GitSandboxTimeout,
    GitToolAdapter,
    GitToolContractError,
    InMemoryGitCommandLogStore,
    git_tool_validator,
)
from aep.patch_evaluation import PatchEvaluationContractError, evaluate_patch
from aep.runtime_store import (
    InMemoryRuntimeObjectStore,
    RuntimeObjectAlreadyExistsError,
)
from aep.tool_runtime import ToolCaller, ToolRequest, ToolResultStatus, invoke_tool


FIXTURES = Path(__file__).parents[1] / "fixtures" / "patch-evaluation"


def git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", "-c", "safe.bareRepository=all", *arguments),
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return completed.stdout.decode().strip()


class LocalGitSandbox:
    disabled_hooks_path = os.devnull
    null_device_path = os.devnull

    def run(
        self,
        *,
        repository: Path,
        arguments: Sequence[str],
        environment: Mapping[str, str],
        timeout_ms: int,
        stdin: bytes | None = None,
    ) -> GitSandboxCommandResult:
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


@pytest.fixture
def repository(tmp_path: Path) -> tuple[Path, str, GitToolAdapter]:
    root = tmp_path / "repository"
    root.mkdir()
    git(root, "init")
    git(root, "config", "user.name", "AEP Test")
    git(root, "config", "user.email", "aep@example.test")
    git(root, "config", "core.autocrlf", "false")
    (root / "tracked.txt").write_bytes(b"original\n")
    (root / "caf\N{LATIN SMALL LETTER E WITH ACUTE}.txt").write_bytes(b"unicode\n")
    git(root, "add", "tracked.txt", "caf\N{LATIN SMALL LETTER E WITH ACUTE}.txt")
    git(root, "commit", "-m", "fixture")
    revision = git(root, "rev-parse", "HEAD")
    adapter = GitToolAdapter(
        repository=root,
        repository_id="example/repository",
        expected_revision=revision,
        working_branch="agent/work",
        log_store=InMemoryGitCommandLogStore(),
        sandbox=LocalGitSandbox(),
    )
    branch_result = invoke_tool(
        ToolRequest(
            tool_ref={"kind": "Tool", "name": "git", "version": "1.0.0"},
            input={
                "operation": "create_branch",
                "expectedRevision": revision,
                "branch": "agent/work",
            },
            caller=ToolCaller(kind="TaskExecution", id="taskexecution-123456789abc"),
            capabilities=("git.read",),
            timeout_ms=5_000,
            trace_id="trace-patch-evaluation",
        ),
        validator=git_tool_validator(),
        authorize=lambda _request: True,
        adapter=adapter,
    )
    assert branch_result.status is ToolResultStatus.SUCCEEDED
    return root, revision, adapter


def artifact(content: bytes, revision: str) -> dict[str, str]:
    return {
        "kind": "GeneratedArtifact",
        "id": "generatedartifact-123456789abc",
        "artifactType": "PATCH",
        "contentAddress": f"sha256:{sha256(content).hexdigest()}",
        "repositoryRevision": revision,
    }


def evaluate(
    repository: tuple[Path, str, GitToolAdapter],
    fixture: str | None,
    *,
    allowed_paths: tuple[str, ...] = ("tracked.txt",),
    artifact_revision: str | None = None,
    store: InMemoryRuntimeObjectStore | None = None,
):
    _root, revision, adapter = repository
    content = b"" if fixture is None else (FIXTURES / fixture).read_bytes()
    metadata = artifact(content, artifact_revision or revision)
    runtime_store = store or InMemoryRuntimeObjectStore()
    result = evaluate_patch(
        store=runtime_store,
        git_adapter=adapter,
        authorize_git=lambda _request: True,
        result_id="evaluationresult-123456789abc",
        task_execution_id="taskexecution-123456789abc",
        evaluation_ref={
            "kind": "Evaluation",
            "name": "patch-safety",
            "version": "1.0.0",
        },
        patch_artifact=metadata,
        patch_content=content,
        expected_revision=revision,
        allowed_paths=allowed_paths,
        working_branch="agent/work",
        trace_id="trace-patch-evaluation",
        timestamp="2026-08-04T00:00:00Z",
        provenance={
            "actor": "patch-evaluator",
            "workflowExecutionId": "workflowexecution-123456789abc",
            "taskExecutionId": "taskexecution-123456789abc",
            "resourceRefs": [],
        },
    )
    return runtime_store, result


def error_codes(result) -> set[str]:
    return {error["code"] for error in result["evidence"]["errors"]}


def test_clean_patch_passes_without_mutating_repository(repository) -> None:
    root, revision, _adapter = repository

    store, result = evaluate(repository, "clean.patch")

    assert result["outcome"] == "PASS"
    assert result["evidence"]["applicable"] is True
    assert result["evidence"]["changedFiles"] == ["tracked.txt"]
    assert result["evidence"]["boundaryChecks"] == [
        {"path": "tracked.txt", "allowed": True, "rule": "tracked.txt"},
    ]
    assert result["target"]["id"] == "generatedartifact-123456789abc"
    assert result["evidence"]["git"]["logsRef"].startswith("memory://git-logs/")
    assert store.get(result["id"]) == result
    assert git(root, "rev-parse", "HEAD") == revision
    assert git(root, "status", "--porcelain") == ""
    assert (root / "tracked.txt").read_text(encoding="utf-8") == "original\n"
    with pytest.raises(TypeError):
        result["outcome"] = "FAIL"


def test_conflicting_patch_fails_with_git_diagnostics(repository) -> None:
    _, result = evaluate(repository, "conflicting.patch")

    assert result["outcome"] == "FAIL"
    assert "PATCH_NOT_APPLICABLE" in error_codes(result)
    assert result["evidence"]["changedFiles"] == ["tracked.txt"]
    assert any("patch failed" in item.lower() for item in result["evidence"]["diagnostics"])


def test_clean_patch_outside_allowed_paths_fails(repository) -> None:
    _, result = evaluate(
        repository,
        "out-of-scope.patch",
        allowed_paths=("src", "tests"),
    )

    assert result["evidence"]["applicable"] is True
    assert result["evidence"]["changedFiles"] == ["private/secret.txt"]
    assert "DISALLOWED_PATH" in error_codes(result)
    assert result["outcome"] == "FAIL"


@pytest.mark.parametrize("fixture", ["malformed.patch", None])
def test_malformed_and_empty_patches_fail_before_validation(repository, fixture) -> None:
    _, result = evaluate(repository, fixture)

    assert result["outcome"] == "FAIL"
    assert result["evidence"]["changedFiles"] == []
    assert error_codes(result) & {"PATCH_NOT_APPLICABLE", "EMPTY_PATCH"}


def test_revision_mismatch_fails_without_running_git(repository) -> None:
    _, result = evaluate(repository, "clean.patch", artifact_revision="a" * 40)

    assert result["outcome"] == "FAIL"
    assert "REVISION_MISMATCH" in error_codes(result)
    assert result["evidence"]["git"]["status"] == "NOT_RUN"


def test_allowed_directory_rule_includes_descendants(repository) -> None:
    _, result = evaluate(
        repository,
        "out-of-scope.patch",
        allowed_paths=("private",),
    )

    assert result["outcome"] == "PASS"


def test_rename_checks_source_and_destination_paths(repository) -> None:
    _, result = evaluate(
        repository,
        "rename.patch",
        allowed_paths=("private",),
    )

    assert result["evidence"]["applicable"] is True
    assert result["evidence"]["changedFiles"] == [
        "private/tracked.txt",
        "tracked.txt",
    ]
    assert "DISALLOWED_PATH" in error_codes(result)


def test_git_octal_quoted_unicode_rename_checks_both_paths(repository) -> None:
    _, result = evaluate(
        repository,
        "unicode-rename.patch",
        allowed_paths=("caf\N{LATIN SMALL LETTER E WITH ACUTE}.txt", "private"),
    )

    assert result["outcome"] == "PASS"
    assert result["evidence"]["applicable"] is True
    assert result["evidence"]["changedFiles"] == [
        "caf\N{LATIN SMALL LETTER E WITH ACUTE}.txt",
        "private/caf\N{LATIN SMALL LETTER E WITH ACUTE}.txt",
    ]


def test_git_octal_quoted_unicode_rename_rejects_disallowed_source(repository) -> None:
    _, result = evaluate(
        repository,
        "unicode-rename.patch",
        allowed_paths=("private",),
    )

    assert result["evidence"]["applicable"] is True
    assert "DISALLOWED_PATH" in error_codes(result)


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (b'"caf\\303.txt"', "not valid UTF-8"),
        (b'"bad\\q.txt"', "unsupported quoted path escape"),
        (b'"unterminated', "malformed quoted path"),
        (b'"nul\\000.txt"', "must not contain NUL"),
    ],
)
def test_invalid_git_quoted_paths_fail_safely(value: bytes, message: str) -> None:
    with pytest.raises(GitToolContractError, match=message):
        _decode_patch_path(value)


@pytest.mark.parametrize("rule", ["../src", "/src", "src\\outside"])
def test_unsafe_allowed_path_rule_is_rejected(repository, rule: str) -> None:
    with pytest.raises(PatchEvaluationContractError, match="allowed path rules"):
        evaluate(repository, "clean.patch", allowed_paths=(rule,))


def test_result_id_cannot_be_reused_for_different_evidence(repository) -> None:
    store = InMemoryRuntimeObjectStore()
    evaluate(repository, "clean.patch", store=store)

    with pytest.raises(RuntimeObjectAlreadyExistsError):
        evaluate(
            repository,
            "out-of-scope.patch",
            allowed_paths=("private",),
            store=store,
        )
