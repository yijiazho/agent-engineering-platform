from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
import subprocess

import pytest

from aep.repository_knowledge import (
    InvalidRepositoryRevisionError,
    MvpRepositoryScanner,
    NotGitRepositoryError,
)


SCANNED_AT = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)


def _write(repository, relative_path: str, content: str = "") -> None:
    path = repository / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _git(repository, *arguments: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(repository), *arguments),
        capture_output=True,
        check=True,
        text=True,
    )
    return result.stdout.strip()


def _commit(repository, message: str = "fixture") -> str:
    _git(repository, "add", ".")
    _git(
        repository,
        "-c",
        "user.name=AEP Test",
        "-c",
        "user.email=aep@example.invalid",
        "commit",
        "--allow-empty",
        "-q",
        "-m",
        message,
    )
    return _git(repository, "rev-parse", "HEAD")


def _initialize_repository(repository) -> None:
    _git(repository, "init", "-q")


def test_scans_common_layout_with_languages_docs_manifests_and_test_hints(tmp_path):
    _initialize_repository(tmp_path)
    _write(tmp_path, "README.md", "# Example")
    _write(tmp_path, "docs/design.rst", "Design")
    _write(tmp_path, "src/example.py", "VALUE = 1")
    _write(tmp_path, "web/app.ts", "export const value = 1")
    _write(tmp_path, "pyproject.toml", "[project]")
    _write(tmp_path, "web/package.json", "{}")

    revision = _commit(tmp_path)
    snapshot = MvpRepositoryScanner().scan(tmp_path, revision="HEAD", scanned_at=SCANNED_AT)

    assert snapshot.api_version == "aep.dev/repository-knowledge/v1"
    assert snapshot.repository_revision == revision
    assert snapshot.created_at == "2026-07-12T12:00:00Z"
    assert snapshot.snapshot_version.startswith("sha256:")
    assert [(file.path, file.language) for file in snapshot.files] == [
        ("docs/design.rst", "reStructuredText"),
        ("pyproject.toml", "TOML"),
        ("README.md", "Markdown"),
        ("src/example.py", "Python"),
        ("web/app.ts", "TypeScript"),
        ("web/package.json", "JSON"),
    ]
    assert [file.path for file in snapshot.documentation] == [
        "docs/design.rst",
        "README.md",
    ]
    assert [manifest.path for manifest in snapshot.dependency_manifests] == [
        "pyproject.toml",
        "web/package.json",
    ]
    assert [(hint.source_path, hint.command) for hint in snapshot.test_command_hints] == [
        ("pyproject.toml", "python -m pytest"),
        ("web/package.json", "npm test"),
    ]
    assert all(
        file.provenance.repository_revision == revision for file in snapshot.files
    )
    assert all(
        file.provenance.source_path == file.path for file in snapshot.files
    )


def test_ignores_vendor_generated_and_tool_output_directories(tmp_path):
    _initialize_repository(tmp_path)
    _write(tmp_path, "src/main.go")
    _write(tmp_path, "vendor/dependency.go")
    _write(tmp_path, "generated/client.py")
    _write(tmp_path, "node_modules/package/index.js")
    _write(tmp_path, "dist/app.js")
    _write(tmp_path, ".venv/lib/site.py")

    revision = _commit(tmp_path)
    snapshot = MvpRepositoryScanner().scan(
        tmp_path, revision=revision, scanned_at=SCANNED_AT
    )

    assert [file.path for file in snapshot.files] == ["src/main.go"]


def test_detects_common_dependency_manifests_in_stable_order(tmp_path):
    _initialize_repository(tmp_path)
    for path in (
        "service/pom.xml",
        "go.mod",
        "Cargo.toml",
        "requirements-dev.txt",
        "dotnet/App.csproj",
        "frontend/package.json",
    ):
        _write(tmp_path, path)

    revision = _commit(tmp_path)
    snapshot = MvpRepositoryScanner().scan(
        tmp_path, revision=revision, scanned_at=SCANNED_AT
    )

    assert [manifest.path for manifest in snapshot.dependency_manifests] == [
        "Cargo.toml",
        "dotnet/App.csproj",
        "frontend/package.json",
        "go.mod",
        "requirements-dev.txt",
        "service/pom.xml",
    ]
    assert [hint.command for hint in snapshot.test_command_hints] == [
        "cargo test",
        "dotnet test",
        "npm test",
        "go test ./...",
        "mvn test",
    ]


def test_repeatable_output_and_revision_bound_version(tmp_path):
    _initialize_repository(tmp_path)
    _write(tmp_path, "B.py")
    _write(tmp_path, "a.py")
    first_revision = _commit(tmp_path, "first")
    scanner = MvpRepositoryScanner()

    first = scanner.scan(tmp_path, revision=first_revision, scanned_at=SCANNED_AT)
    second = scanner.scan(tmp_path, revision=first_revision, scanned_at=SCANNED_AT)
    _write(tmp_path, "a.py", "changed")
    second_revision = _commit(tmp_path, "content-only change")
    other_revision = scanner.scan(
        tmp_path, revision=second_revision, scanned_at=SCANNED_AT
    )

    assert first == second
    assert [file.path for file in first.files] == ["a.py", "B.py"]
    assert first.snapshot_version != other_revision.snapshot_version
    with pytest.raises(FrozenInstanceError):
        first.repository_revision = "changed"


@pytest.mark.parametrize("revision", ["", "   ", None])
def test_rejects_missing_revision(tmp_path, revision):
    with pytest.raises(ValueError, match="revision must be a non-empty string"):
        MvpRepositoryScanner().scan(tmp_path, revision=revision, scanned_at=SCANNED_AT)


def test_normalizes_string_timestamp_and_rejects_naive_datetime(tmp_path):
    _initialize_repository(tmp_path)
    revision = _commit(tmp_path)
    snapshot = MvpRepositoryScanner().scan(
        tmp_path, revision=revision, scanned_at="2026-07-12T05:00:00-07:00"
    )

    assert snapshot.created_at == "2026-07-12T12:00:00Z"
    with pytest.raises(ValueError, match="must include a timezone"):
        MvpRepositoryScanner().scan(
            tmp_path, revision=revision, scanned_at=datetime(2026, 7, 12, 12, 0)
        )


def test_reads_requested_commit_instead_of_dirty_working_tree(tmp_path):
    _initialize_repository(tmp_path)
    _write(tmp_path, "tracked.py", "committed")
    revision = _commit(tmp_path)
    scanner = MvpRepositoryScanner()
    committed = scanner.scan(tmp_path, revision=revision, scanned_at=SCANNED_AT)

    _write(tmp_path, "tracked.py", "dirty")
    _write(tmp_path, "untracked.py", "not committed")
    dirty = scanner.scan(tmp_path, revision=revision, scanned_at=SCANNED_AT)

    assert dirty == committed
    assert [file.path for file in dirty.files] == ["tracked.py"]


def test_rejects_revision_that_does_not_resolve_to_a_commit(tmp_path):
    _initialize_repository(tmp_path)
    _commit(tmp_path)

    with pytest.raises(InvalidRepositoryRevisionError, match="does not resolve"):
        MvpRepositoryScanner().scan(
            tmp_path, revision="not-a-real-revision", scanned_at=SCANNED_AT
        )


def test_rejects_non_git_repository(tmp_path):
    with pytest.raises(NotGitRepositoryError, match="not a Git repository"):
        MvpRepositoryScanner().scan(
            tmp_path, revision="HEAD", scanned_at=SCANNED_AT
        )
