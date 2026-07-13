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


from aep.repository_knowledge import (
    CandidateFileQuery,
    DependencyManifest,
    DependencyManifestQuery,
    DocumentationQuery,
    FileQuery,
    InMemoryRepositoryKnowledgeProvider,
    KnowledgeKind,
    KnowledgeResult,
    QueryProvenance,
    RepositoryFile,
    RepositoryKnowledgeProvider,
    RepositoryKnowledgeSnapshot,
    SnapshotRecord,
    SourceLocation,
    SourceProvenance,
    TestCommandHint as ScannerTestCommandHint,
    TestHintQuery as KnowledgeTestHintQuery,
)


REVISION = "7f3d2c1"


def record(
    id: str,
    kind: KnowledgeKind,
    path: str,
    *,
    attributes: dict | None = None,
    keywords: tuple[str, ...] = (),
    start_line: int | None = None,
    end_line: int | None = None,
) -> SnapshotRecord:
    return SnapshotRecord(
        id=id,
        kind=kind,
        location=SourceLocation(path, start_line, end_line),
        attributes=attributes or {},
        keywords=keywords,
    )


def snapshot(records: list[SnapshotRecord]) -> RepositoryKnowledgeSnapshot:
    def source_provenance(path: str) -> SourceProvenance:
        return SourceProvenance(
            source_path=path,
            repository_revision=REVISION,
            scanned_at="2026-07-12T10:00:00Z",
            scanner_version="mvp-scanner/1.0.0",
        )

    language_by_path = {
        item.location.path: item.attributes["language"]
        for item in records
        if item.attributes.get("language") is not None
    }
    files: list[RepositoryFile] = []
    documentation: list[RepositoryFile] = []
    manifests: list[DependencyManifest] = []
    hints: list[ScannerTestCommandHint] = []
    for item in records:
        path = item.location.path
        provenance = source_provenance(path)
        if item.kind is KnowledgeKind.FILE:
            files.append(
                RepositoryFile(
                    path=path,
                    language=item.attributes.get("language"),
                    is_documentation=bool(item.attributes.get("is_documentation")),
                    provenance=provenance,
                )
            )
        elif item.kind is KnowledgeKind.DOCUMENTATION:
            document = RepositoryFile(
                path=path,
                language=item.attributes.get("language", "Markdown"),
                is_documentation=True,
                provenance=provenance,
            )
            files.append(document)
            documentation.append(document)
        elif item.kind is KnowledgeKind.DEPENDENCY_MANIFEST:
            manifests.append(
                DependencyManifest(
                    path=path,
                    ecosystem=item.attributes["ecosystem"],
                    manifest_type=item.attributes.get("manifest_type", "dependencies"),
                    provenance=provenance,
                )
            )
        elif item.kind is KnowledgeKind.TEST_HINT:
            hints.append(
                ScannerTestCommandHint(
                    command=item.attributes["command"],
                    source_path=path,
                    provenance=provenance,
                )
            )

    known_file_paths = {file.path for file in files}
    for item in records:
        path = item.location.path
        if path in known_file_paths:
            continue
        files.append(
            RepositoryFile(
                path=path,
                language=language_by_path.get(path),
                is_documentation=False,
                provenance=source_provenance(path),
            )
        )
        known_file_paths.add(path)

    return RepositoryKnowledgeSnapshot(
        api_version="aep.dev/repository-knowledge/v1",
        snapshot_version="snapshot-v12",
        repository_revision=REVISION,
        created_at="2026-07-12T10:00:00Z",
        scanner_version="mvp-scanner/1.0.0",
        files=tuple(files),
        documentation=tuple(documentation),
        dependency_manifests=tuple(manifests),
        test_command_hints=tuple(hints),
    )


def provider(records: list[SnapshotRecord]) -> InMemoryRepositoryKnowledgeProvider:
    return InMemoryRepositoryKnowledgeProvider(snapshot(records))


def provenance(**changes) -> QueryProvenance:
    values = {
        "repository_revision": REVISION,
        "snapshot_version": "snapshot-v12",
        "snapshot_created_at": "2026-07-12T10:00:00Z",
        "snapshot_producer": "mvp-scanner/1.0.0",
        "source": SourceLocation("src/auth.py"),
        "traversal_path": ("snapshot:snapshot-v12", "file:file-auth"),
    }
    values.update(changes)
    return QueryProvenance(**values)


def all_records() -> list[SnapshotRecord]:
    # Deliberately not path-sorted so tests exercise provider ordering.
    return [
        record(
            "file-tests",
            KnowledgeKind.FILE,
            "tests/test_auth.py",
            attributes={"language": "Python", "is_test": True, "content": "test login"},
        ),
        record(
            "doc-adr",
            KnowledgeKind.DOCUMENTATION,
            "docs/adr/ADR-001-login.md",
            attributes={"title": "Authentication decision", "content": "login tokens"},
            start_line=1,
            end_line=12,
        ),
        record(
            "manifest-web",
            KnowledgeKind.DEPENDENCY_MANIFEST,
            "web/package.json",
            attributes={"ecosystem": "npm", "dependencies": ["react"]},
        ),
        record(
            "file-auth",
            KnowledgeKind.FILE,
            "src/auth.py",
            attributes={"language": "Python", "content": "def login(): pass"},
            keywords=("authentication",),
        ),
        record(
            "hint-web",
            KnowledgeKind.TEST_HINT,
            "web/package.json",
            attributes={"language": "TypeScript", "command": "npm test"},
        ),
        record(
            "doc-readme",
            KnowledgeKind.DOCUMENTATION,
            "docs/login-overview.md",
            attributes={"title": "Project authentication", "content": "login overview"},
        ),
        record(
            "manifest-python",
            KnowledgeKind.DEPENDENCY_MANIFEST,
            "pyproject.toml",
            attributes={"ecosystem": "python", "dependencies": ["pytest"]},
        ),
        record(
            "file-service",
            KnowledgeKind.FILE,
            "src/auth/service.py",
            attributes={"language": "Python", "content": "authentication service"},
        ),
        record(
            "hint-python",
            KnowledgeKind.TEST_HINT,
            "pyproject.toml",
            attributes={"language": "Python", "command": "python -m pytest"},
        ),
    ]


def test_implements_provider_neutral_query_contract() -> None:
    knowledge: RepositoryKnowledgeProvider = provider(all_records())

    assert [result.id for result in knowledge.lookup_file(FileQuery("src\\auth.py"))] == [
        "file:src/auth.py"
    ]
    assert knowledge.lookup_file(FileQuery("src/missing.py")) == ()


def test_supports_document_manifest_and_test_hint_queries() -> None:
    knowledge = provider(all_records())

    docs = knowledge.lookup_documentation(DocumentationQuery(terms=("login",)))
    manifests = knowledge.lookup_dependency_manifests(
        DependencyManifestQuery(ecosystems=("PYTHON",))
    )
    hints = knowledge.lookup_test_hints(
        KnowledgeTestHintQuery(languages=("python",))
    )

    assert [result.id for result in docs] == [
        "documentation:docs/adr/ADR-001-login.md",
        "documentation:docs/login-overview.md",
    ]
    assert [result.id for result in manifests] == [
        "dependency-manifest:pyproject.toml"
    ]
    assert [result.attributes["command"] for result in hints] == ["python -m pytest"]


def test_every_result_has_revision_snapshot_and_source_provenance() -> None:
    results = provider(all_records()).lookup_documentation(DocumentationQuery())

    assert results
    for result in results:
        assert result.provenance.repository_revision == REVISION
        assert result.provenance.snapshot_version == "snapshot-v12"
        assert result.provenance.snapshot_created_at == "2026-07-12T10:00:00Z"
        assert result.provenance.snapshot_producer == "mvp-scanner/1.0.0"
        assert result.provenance.source.path
        assert result.provenance.traversal_path[0] == "snapshot:snapshot-v12"


def test_candidate_search_filters_and_ranks_without_a_model() -> None:
    knowledge = provider(all_records())

    results = knowledge.search_candidate_files(
        CandidateFileQuery(
            terms=("auth", "service"),
            languages=("python",),
            include_tests=False,
        )
    )

    assert [(result.id, result.score) for result in results] == [
        ("file:src/auth/service.py", 6),
        ("file:src/auth.py", 3),
    ]


@pytest.mark.parametrize(
    "query",
    [
        DocumentationQuery(),
        DependencyManifestQuery(),
        KnowledgeTestHintQuery(),
        CandidateFileQuery(limit=None),
    ],
)
def test_results_have_stable_order_independent_of_snapshot_insertion(query) -> None:
    records = all_records()
    first = provider(records)
    second = provider(list(reversed(records)))

    method_name = {
        DocumentationQuery: "lookup_documentation",
        DependencyManifestQuery: "lookup_dependency_manifests",
        KnowledgeTestHintQuery: "lookup_test_hints",
        CandidateFileQuery: "search_candidate_files",
    }[type(query)]

    first_results = getattr(first, method_name)(query)
    second_results = getattr(second, method_name)(query)
    first_ids = [result.id for result in first_results]
    second_ids = [result.id for result in second_results]

    assert first_ids == second_ids
    paths = [result.provenance.source.path for result in first_results]
    assert paths == sorted(paths, key=lambda path: (path.casefold(), path))


def test_queries_support_path_prefix_and_limits() -> None:
    knowledge = provider(all_records())

    docs = knowledge.lookup_documentation(
        DocumentationQuery(path_prefix="docs", limit=1)
    )
    files = knowledge.search_candidate_files(
        CandidateFileQuery(path_prefix="src/auth", limit=1)
    )

    assert [result.id for result in docs] == [
        "documentation:docs/adr/ADR-001-login.md"
    ]
    assert [result.id for result in files] == ["file:src/auth/service.py"]


def test_snapshot_and_results_are_recursively_immutable() -> None:
    attributes = {"nested": {"items": ["original"]}}
    source = record("file", KnowledgeKind.FILE, "src/file.py", attributes=attributes)
    knowledge = provider([source])
    result = knowledge.lookup_file(FileQuery("src/file.py"))[0]
    attributes["nested"]["items"][0] = "changed"

    assert source.attributes["nested"]["items"] == ("original",)
    with pytest.raises(TypeError):
        result.attributes["language"] = "Go"


@pytest.mark.parametrize(
    "value",
    [
        {"mutable"},
        {"nested": {"mutable"}},
        {1: "non-string key"},
        float("nan"),
        object(),
    ],
)
def test_snapshot_rejects_non_json_attribute_values(value) -> None:
    with pytest.raises(ValueError, match="JSON-compatible"):
        record(
            "unsupported",
            KnowledgeKind.FILE,
            "src/file.py",
            attributes={"value": value},
        )


@pytest.mark.parametrize(
    "path",
    [
        "../secret",
        "/absolute",
        "",
        r"C:\Windows\system.ini",
        "C:/Windows/system.ini",
        r"C:relative\secret.txt",
        r"\\server\share\secret.txt",
        " " + r"C:\Windows\system.ini",
        "\tC:/Windows/system.ini ",
        " " + r"\\server\share\secret.txt",
    ],
)
def test_queries_reject_paths_outside_the_repository(path: str) -> None:
    with pytest.raises(ValueError, match="relative paths"):
        FileQuery(path)


def test_snapshot_rejects_duplicate_record_ids() -> None:
    duplicate = record("one", KnowledgeKind.FILE, "src/one.py")
    with pytest.raises(ValueError, match="unique stable identifiers"):
        provider([duplicate, record("two", KnowledgeKind.FILE, "src/one.py")])


def test_source_locations_can_express_future_ast_backed_results() -> None:
    location = SourceLocation("src/auth.py", start_line=7, end_line=11, symbol="login")

    assert location.symbol == "login"
    assert (location.start_line, location.end_line) == (7, 11)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"source": "src/auth.py"}, "source must be a SourceLocation"),
        ({"traversal_path": "snapshot:file"}, "sequence of strings"),
        ({"traversal_path": {"snapshot:file"}}, "sequence of strings"),
        ({"traversal_path": ("snapshot:file", 1)}, "sequence of non-empty strings"),
        ({"traversal_path": ()}, "must explain how the result was selected"),
        ({"repository_revision": 123}, "repository_revision must not be empty"),
    ],
)
def test_query_provenance_rejects_invalid_contract_values(changes, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        provenance(**changes)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"id": 123}, "result id must not be empty"),
        ({"kind": "FILE"}, "result kind must be a KnowledgeKind"),
        ({"attributes": []}, "result attributes must be a mapping"),
        ({"provenance": {}}, "provenance must be a QueryProvenance"),
    ],
)
def test_knowledge_result_rejects_invalid_contract_values(changes, message: str) -> None:
    values = {
        "id": "file-auth",
        "kind": KnowledgeKind.FILE,
        "score": 0,
        "attributes": {"language": "Python"},
        "provenance": provenance(),
    }
    values.update(changes)

    with pytest.raises(ValueError, match=message):
        KnowledgeResult(**values)
