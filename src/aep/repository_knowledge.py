"""Deterministic repository knowledge scanning and provider-neutral queries."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
from math import isfinite
from pathlib import Path, PurePosixPath, PureWindowsPath
import subprocess
from types import MappingProxyType
from typing import Any, Final


SNAPSHOT_API_VERSION: Final = "aep.dev/repository-knowledge/v1"
SCANNER_VERSION: Final = "aep-mvp-scanner/1.0"

DEFAULT_IGNORED_DIRECTORIES: Final = frozenset(
    {
        ".git",
        ".hg",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "coverage",
        "dist",
        "generated",
        "node_modules",
        "out",
        "target",
        "vendor",
        "venv",
    }
)

_LANGUAGES_BY_SUFFIX: Final = {
    ".adoc": "AsciiDoc",
    ".c": "C",
    ".cc": "C++",
    ".cpp": "C++",
    ".cs": "C#",
    ".css": "CSS",
    ".cts": "TypeScript",
    ".go": "Go",
    ".h": "C",
    ".hpp": "C++",
    ".html": "HTML",
    ".java": "Java",
    ".js": "JavaScript",
    ".json": "JSON",
    ".jsx": "JavaScript",
    ".kt": "Kotlin",
    ".kts": "Kotlin",
    ".md": "Markdown",
    ".mdx": "Markdown",
    ".mjs": "JavaScript",
    ".mts": "TypeScript",
    ".php": "PHP",
    ".ps1": "PowerShell",
    ".py": "Python",
    ".pyi": "Python",
    ".rb": "Ruby",
    ".rs": "Rust",
    ".rst": "reStructuredText",
    ".sh": "Shell",
    ".sql": "SQL",
    ".swift": "Swift",
    ".toml": "TOML",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".xml": "XML",
    ".yaml": "YAML",
    ".yml": "YAML",
}

_LANGUAGES_BY_FILENAME: Final = {
    "dockerfile": "Dockerfile",
    "gemfile": "Ruby",
    "makefile": "Makefile",
}

_DOCUMENTATION_SUFFIXES: Final = frozenset({".adoc", ".md", ".mdx", ".rst"})
_DOCUMENTATION_NAMES: Final = (
    "changelog",
    "contributing",
    "license",
    "readme",
)


@dataclass(frozen=True, slots=True)
class SourceProvenance:
    """Origin metadata carried by every discovered repository fact."""

    source_path: str
    repository_revision: str
    scanned_at: str
    scanner_version: str


@dataclass(frozen=True, slots=True)
class RepositoryFile:
    """A file in the scanned repository inventory."""

    path: str
    language: str | None
    is_documentation: bool
    provenance: SourceProvenance


@dataclass(frozen=True, slots=True)
class DependencyManifest:
    """A recognized dependency or build manifest."""

    path: str
    ecosystem: str
    manifest_type: str
    provenance: SourceProvenance


@dataclass(frozen=True, slots=True)
class TestCommandHint:
    """A deterministic test command inferred from a repository file."""

    command: str
    source_path: str
    provenance: SourceProvenance


@dataclass(frozen=True, slots=True)
class RepositoryKnowledgeSnapshot:
    """An immutable repository inventory tied to exactly one revision."""

    api_version: str
    snapshot_version: str
    repository_revision: str
    created_at: str
    scanner_version: str
    files: tuple[RepositoryFile, ...]
    documentation: tuple[RepositoryFile, ...]
    dependency_manifests: tuple[DependencyManifest, ...]
    test_command_hints: tuple[TestCommandHint, ...]


class RepositoryScanError(Exception):
    """Base class for repository scanner failures."""


class NotGitRepositoryError(RepositoryScanError):
    """Raised when the scan root is not the root of a Git repository."""


class InvalidRepositoryRevisionError(RepositoryScanError):
    """Raised when a requested revision does not resolve to a Git commit."""


class MvpRepositoryScanner:
    """Compile a Git commit into the replaceable MVP knowledge snapshot."""

    def __init__(
        self,
        *,
        ignored_directories: frozenset[str] = DEFAULT_IGNORED_DIRECTORIES,
        scanner_version: str = SCANNER_VERSION,
    ) -> None:
        if not scanner_version:
            raise ValueError("scanner_version must not be empty")
        self._ignored_directories = frozenset(
            directory.casefold() for directory in ignored_directories
        )
        self._scanner_version = scanner_version

    def scan(
        self,
        repository_root: str | Path,
        *,
        revision: str,
        scanned_at: datetime | str | None = None,
    ) -> RepositoryKnowledgeSnapshot:
        """Scan the requested Git commit without reading working-tree contents."""

        root = Path(repository_root).resolve()
        if not root.is_dir():
            raise ValueError(f"repository_root is not a directory: {root}")
        if not isinstance(revision, str) or not revision.strip():
            raise ValueError("revision must be a non-empty string")
        requested_revision = revision.strip()

        timestamp = _normalize_timestamp(scanned_at)
        self._validate_repository_root(root)
        revision = self._resolve_revision(root, requested_revision)
        paths = self._discover_paths(root, revision)
        files = tuple(
            self._file_record(path, revision=revision, scanned_at=timestamp)
            for path in paths
        )
        documentation = tuple(file for file in files if file.is_documentation)
        manifests = tuple(
            manifest
            for path in paths
            if (
                manifest := self._dependency_manifest(
                    path, revision=revision, scanned_at=timestamp
                )
            )
            is not None
        )
        hints = self._test_command_hints(
            paths,
            revision=revision,
            scanned_at=timestamp,
        )
        snapshot_version = _snapshot_version(
            revision=revision,
            scanner_version=self._scanner_version,
            files=files,
            manifests=manifests,
            hints=hints,
        )

        return RepositoryKnowledgeSnapshot(
            api_version=SNAPSHOT_API_VERSION,
            snapshot_version=snapshot_version,
            repository_revision=revision,
            created_at=timestamp,
            scanner_version=self._scanner_version,
            files=files,
            documentation=documentation,
            dependency_manifests=manifests,
            test_command_hints=hints,
        )

    def _validate_repository_root(self, root: Path) -> None:
        result = _run_git(root, "rev-parse", "--show-toplevel")
        if result.returncode != 0:
            raise NotGitRepositoryError(f"repository_root is not a Git repository: {root}")
        discovered_root = Path(_decode_git_output(result.stdout).strip()).resolve()
        if discovered_root != root:
            raise NotGitRepositoryError(
                f"repository_root must be the Git repository root: {root}"
            )

    def _resolve_revision(self, root: Path, revision: str) -> str:
        result = _run_git(
            root,
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{revision}^{{commit}}",
        )
        if result.returncode != 0:
            raise InvalidRepositoryRevisionError(
                f"revision does not resolve to a Git commit: {revision!r}"
            )
        return _decode_git_output(result.stdout).strip()

    def _discover_paths(self, root: Path, revision: str) -> tuple[str, ...]:
        result = _run_git(root, "ls-tree", "-r", "-z", "--full-tree", revision)
        if result.returncode != 0:
            raise RepositoryScanError(
                f"failed to read repository tree for revision {revision!r}"
            )

        discovered: list[str] = []
        for entry in result.stdout.split(b"\0"):
            if not entry:
                continue
            metadata, separator, raw_path = entry.partition(b"\t")
            fields = metadata.split()
            if not separator or len(fields) != 3:
                raise RepositoryScanError("Git returned an invalid tree entry")
            mode, object_type, _object_id = fields
            if object_type != b"blob" or mode == b"120000":
                continue
            path = raw_path.decode("utf-8", errors="surrogateescape")
            if self._is_ignored(path):
                continue
            discovered.append(path)
        return tuple(sorted(discovered, key=lambda value: (value.casefold(), value)))

    def _is_ignored(self, path: str) -> bool:
        return any(
            part.casefold() in self._ignored_directories
            for part in Path(path).parts[:-1]
        )

    def _file_record(
        self, path: str, *, revision: str, scanned_at: str
    ) -> RepositoryFile:
        provenance = self._provenance(path, revision=revision, scanned_at=scanned_at)
        return RepositoryFile(
            path=path,
            language=_detect_language(path),
            is_documentation=_is_documentation(path),
            provenance=provenance,
        )

    def _dependency_manifest(
        self, path: str, *, revision: str, scanned_at: str
    ) -> DependencyManifest | None:
        detected = _detect_dependency_manifest(path)
        if detected is None:
            return None
        ecosystem, manifest_type = detected
        return DependencyManifest(
            path=path,
            ecosystem=ecosystem,
            manifest_type=manifest_type,
            provenance=self._provenance(path, revision=revision, scanned_at=scanned_at),
        )

    def _test_command_hints(
        self,
        paths: tuple[str, ...],
        *,
        revision: str,
        scanned_at: str,
    ) -> tuple[TestCommandHint, ...]:
        path_set = set(paths)
        hints: list[TestCommandHint] = []
        for path in paths:
            command = _detect_test_command(path, path_set)
            if command is None:
                continue
            hints.append(
                TestCommandHint(
                    command=command,
                    source_path=path,
                    provenance=self._provenance(
                        path, revision=revision, scanned_at=scanned_at
                    ),
                )
            )
        return tuple(hints)

    def _provenance(
        self, source_path: str, *, revision: str, scanned_at: str
    ) -> SourceProvenance:
        return SourceProvenance(
            source_path=source_path,
            repository_revision=revision,
            scanned_at=scanned_at,
            scanner_version=self._scanner_version,
        )


def _normalize_timestamp(value: datetime | str | None) -> str:
    if value is None:
        value = datetime.now(timezone.utc)
    if isinstance(value, str):
        if not value.strip():
            raise ValueError("scanned_at must not be empty")
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("scanned_at must be an ISO 8601 timestamp") from error
    if not isinstance(value, datetime):
        raise TypeError("scanned_at must be a datetime, string, or None")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("scanned_at datetime must include a timezone")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _run_git(root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ("git", "-C", str(root), *arguments),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError as error:
        raise RepositoryScanError("Git executable was not found") from error


def _decode_git_output(value: bytes) -> str:
    return value.decode("utf-8", errors="surrogateescape")


def _detect_language(path: str) -> str | None:
    name = Path(path).name.casefold()
    return _LANGUAGES_BY_FILENAME.get(name) or _LANGUAGES_BY_SUFFIX.get(
        Path(name).suffix.casefold()
    )


def _is_documentation(path: str) -> bool:
    name = Path(path).name.casefold()
    return Path(name).suffix in _DOCUMENTATION_SUFFIXES or name.startswith(
        _DOCUMENTATION_NAMES
    )


def _detect_dependency_manifest(path: str) -> tuple[str, str] | None:
    name = Path(path).name.casefold()
    suffix = Path(name).suffix
    exact = {
        "build.gradle": ("Gradle", "build"),
        "build.gradle.kts": ("Gradle", "build"),
        "cargo.toml": ("Cargo", "dependencies"),
        "composer.json": ("Composer", "dependencies"),
        "gemfile": ("Bundler", "dependencies"),
        "go.mod": ("Go Modules", "dependencies"),
        "package.json": ("npm", "dependencies"),
        "pom.xml": ("Maven", "build"),
        "pyproject.toml": ("Python", "project"),
        "setup.py": ("Python", "project"),
    }
    if name in exact:
        return exact[name]
    if name.startswith("requirements") and suffix == ".txt":
        return "Python", "dependencies"
    if suffix == ".csproj":
        return ".NET", "project"
    return None


def _detect_test_command(path: str, all_paths: set[str]) -> str | None:
    name = Path(path).name.casefold()
    if name in {"pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini"}:
        return "python -m pytest"
    if name == "package.json":
        return "npm test"
    if name == "pom.xml":
        return "mvn test"
    if name in {"build.gradle", "build.gradle.kts"}:
        parent = Path(path).parent
        wrapper = (parent / "gradlew").as_posix()
        return "./gradlew test" if wrapper in all_paths else "gradle test"
    if name == "go.mod":
        return "go test ./..."
    if name == "cargo.toml":
        return "cargo test"
    if Path(name).suffix == ".csproj":
        return "dotnet test"
    if name == "gemfile":
        return "bundle exec rake test"
    return None


def _snapshot_version(
    *,
    revision: str,
    scanner_version: str,
    files: tuple[RepositoryFile, ...],
    manifests: tuple[DependencyManifest, ...],
    hints: tuple[TestCommandHint, ...],
) -> str:
    content = {
        "apiVersion": SNAPSHOT_API_VERSION,
        "repositoryRevision": revision,
        "scannerVersion": scanner_version,
        "files": [
            {
                "path": file.path,
                "language": file.language,
                "isDocumentation": file.is_documentation,
            }
            for file in files
        ],
        "dependencyManifests": [
            {
                "path": manifest.path,
                "ecosystem": manifest.ecosystem,
                "manifestType": manifest.manifest_type,
            }
            for manifest in manifests
        ],
        "testCommandHints": [
            {"command": hint.command, "sourcePath": hint.source_path}
            for hint in hints
        ],
    }
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"))
    return f"sha256:{sha256(canonical.encode('utf-8')).hexdigest()}"


JsonObject = Mapping[str, Any]


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JSON-compatible mappings must use string keys")
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and isfinite(value):
        return value
    raise ValueError(
        f"attributes must contain JSON-compatible values, got {type(value).__name__}"
    )


def _normalize_path(value: str, *, allow_root: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError("repository paths must be strings")
    stripped = value.strip()
    if PureWindowsPath(stripped).drive:
        raise ValueError("repository paths must be non-empty relative paths")
    converted = stripped.replace("\\", "/")
    if converted.startswith("/"):
        raise ValueError("repository paths must be non-empty relative paths")
    normalized = converted.strip("/")
    if allow_root and normalized in ("", "."):
        return ""
    path = PurePosixPath(normalized)
    if not normalized or normalized == "." or path.is_absolute() or ".." in path.parts:
        raise ValueError("repository paths must be non-empty relative paths")
    return path.as_posix()


def _normalize_terms(values: Sequence[str], field_name: str) -> tuple[str, ...]:
    if isinstance(values, str) or any(not isinstance(value, str) for value in values):
        raise ValueError(f"{field_name} must be a sequence of strings")
    terms = tuple(dict.fromkeys(value.strip().casefold() for value in values if value.strip()))
    if len(terms) != len(values):
        raise ValueError(f"{field_name} must contain unique, non-empty values")
    return terms


def _validate_limit(limit: int | None) -> None:
    if limit is not None and (
        not isinstance(limit, int) or isinstance(limit, bool) or limit < 1
    ):
        raise ValueError("limit must be a positive integer")


class KnowledgeKind(str, Enum):
    """Kinds understood by the MVP and richer future providers."""

    FILE = "FILE"
    DOCUMENTATION = "DOCUMENTATION"
    DEPENDENCY_MANIFEST = "DEPENDENCY_MANIFEST"
    TEST_HINT = "TEST_HINT"
    SYMBOL = "SYMBOL"
    AST_NODE = "AST_NODE"


@dataclass(frozen=True)
class SourceLocation:
    """A repository-relative source span; line data is optional for flat scans."""

    path: str
    start_line: int | None = None
    end_line: int | None = None
    symbol: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _normalize_path(self.path))
        if (self.start_line is None) != (self.end_line is None):
            raise ValueError("start_line and end_line must be provided together")
        if self.start_line is not None:
            if (
                not isinstance(self.start_line, int)
                or isinstance(self.start_line, bool)
                or not isinstance(self.end_line, int)
                or isinstance(self.end_line, bool)
                or self.start_line < 1
                or self.end_line < self.start_line
            ):
                raise ValueError("source line range must be positive and ordered")
        if self.symbol is not None and not self.symbol:
            raise ValueError("symbol must not be empty")


@dataclass(frozen=True)
class SnapshotRecord:
    """Internal flat query fact adapted from the scanner-owned snapshot."""

    id: str
    kind: KnowledgeKind
    location: SourceLocation
    attributes: JsonObject = field(default_factory=dict)
    keywords: Sequence[str] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("record id must not be empty")
        if not isinstance(self.kind, KnowledgeKind):
            raise ValueError("record kind must be a KnowledgeKind")
        if not isinstance(self.location, SourceLocation):
            raise ValueError("record location must be a SourceLocation")
        if not isinstance(self.attributes, Mapping):
            raise ValueError("record attributes must be a mapping")
        object.__setattr__(self, "attributes", _freeze(self.attributes))
        object.__setattr__(self, "keywords", _normalize_terms(self.keywords, "keywords"))


@dataclass(frozen=True)
class QueryProvenance:
    """Evidence explaining where and at which revision a result originated."""

    repository_revision: str
    snapshot_version: str
    snapshot_created_at: str
    snapshot_producer: str
    source: SourceLocation
    traversal_path: Sequence[str]

    def __post_init__(self) -> None:
        for name in (
            "repository_revision",
            "snapshot_version",
            "snapshot_created_at",
            "snapshot_producer",
        ):
            if not getattr(self, name):
                raise ValueError(f"{name} must not be empty")
        traversal = tuple(self.traversal_path)
        if not traversal or any(not step for step in traversal):
            raise ValueError("traversal_path must explain how the result was selected")
        object.__setattr__(self, "traversal_path", traversal)


@dataclass(frozen=True)
class KnowledgeResult:
    """Provider-neutral query result with immutable payload and provenance."""

    id: str
    kind: KnowledgeKind
    score: int
    attributes: JsonObject
    provenance: QueryProvenance

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("result id must not be empty")
        if not isinstance(self.score, int) or isinstance(self.score, bool) or self.score < 0:
            raise ValueError("score must be a non-negative integer")
        object.__setattr__(self, "attributes", _freeze(self.attributes))


@dataclass(frozen=True)
class FileQuery:
    path: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _normalize_path(self.path))


@dataclass(frozen=True)
class DocumentationQuery:
    terms: Sequence[str] = field(default_factory=tuple)
    path_prefix: str = ""
    limit: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "terms", _normalize_terms(self.terms, "terms"))
        object.__setattr__(self, "path_prefix", _normalize_path(self.path_prefix, allow_root=True))
        _validate_limit(self.limit)


@dataclass(frozen=True)
class DependencyManifestQuery:
    ecosystems: Sequence[str] = field(default_factory=tuple)
    path_prefix: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "ecosystems", _normalize_terms(self.ecosystems, "ecosystems")
        )
        object.__setattr__(self, "path_prefix", _normalize_path(self.path_prefix, allow_root=True))


@dataclass(frozen=True)
class TestHintQuery:
    languages: Sequence[str] = field(default_factory=tuple)
    path_prefix: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "languages", _normalize_terms(self.languages, "languages"))
        object.__setattr__(self, "path_prefix", _normalize_path(self.path_prefix, allow_root=True))


@dataclass(frozen=True)
class CandidateFileQuery:
    terms: Sequence[str] = field(default_factory=tuple)
    languages: Sequence[str] = field(default_factory=tuple)
    path_prefix: str = ""
    include_tests: bool = True
    limit: int | None = 20

    def __post_init__(self) -> None:
        object.__setattr__(self, "terms", _normalize_terms(self.terms, "terms"))
        object.__setattr__(self, "languages", _normalize_terms(self.languages, "languages"))
        object.__setattr__(self, "path_prefix", _normalize_path(self.path_prefix, allow_root=True))
        if not isinstance(self.include_tests, bool):
            raise ValueError("include_tests must be a boolean")
        _validate_limit(self.limit)


class RepositoryKnowledgeProvider(ABC):
    """Stable query boundary used by the Context Builder."""

    @abstractmethod
    def lookup_file(self, query: FileQuery) -> tuple[KnowledgeResult, ...]:
        """Return the file at an exact repository-relative path, if present."""

    @abstractmethod
    def lookup_documentation(
        self, query: DocumentationQuery
    ) -> tuple[KnowledgeResult, ...]:
        """Return documentation selected by structured filters."""

    @abstractmethod
    def lookup_dependency_manifests(
        self, query: DependencyManifestQuery
    ) -> tuple[KnowledgeResult, ...]:
        """Return dependency manifests selected by ecosystem and path."""

    @abstractmethod
    def lookup_test_hints(self, query: TestHintQuery) -> tuple[KnowledgeResult, ...]:
        """Return scanner-discovered test commands and related hints."""

    @abstractmethod
    def search_candidate_files(
        self, query: CandidateFileQuery
    ) -> tuple[KnowledgeResult, ...]:
        """Return deterministically ranked files without model inference."""


class InMemoryRepositoryKnowledgeProvider(RepositoryKnowledgeProvider):
    """MVP provider over a flat, immutable repository knowledge snapshot."""

    def __init__(self, snapshot: RepositoryKnowledgeSnapshot) -> None:
        if not isinstance(snapshot, RepositoryKnowledgeSnapshot):
            raise ValueError("snapshot must be a RepositoryKnowledgeSnapshot")
        self._snapshot = snapshot
        self._records = self._snapshot_records(snapshot)

    @staticmethod
    def _is_test_file(path: str) -> bool:
        file_path = PurePosixPath(path)
        directory_parts = {part.casefold() for part in file_path.parts[:-1]}
        stem = file_path.stem.casefold()
        return bool(directory_parts.intersection({"test", "tests", "spec", "specs"})) or (
            stem.startswith("test_") or stem.endswith("_test")
        )

    @classmethod
    def _snapshot_records(
        cls, snapshot: RepositoryKnowledgeSnapshot
    ) -> tuple[SnapshotRecord, ...]:
        language_by_path = {file.path: file.language for file in snapshot.files}
        records: list[SnapshotRecord] = []
        for file in snapshot.files:
            records.append(
                SnapshotRecord(
                    id=f"file:{file.path}",
                    kind=KnowledgeKind.FILE,
                    location=SourceLocation(file.path),
                    attributes={
                        "language": file.language,
                        "is_documentation": file.is_documentation,
                        "is_test": cls._is_test_file(file.path),
                    },
                )
            )
        for document in snapshot.documentation:
            records.append(
                SnapshotRecord(
                    id=f"documentation:{document.path}",
                    kind=KnowledgeKind.DOCUMENTATION,
                    location=SourceLocation(document.path),
                    attributes={"language": document.language},
                )
            )
        for manifest in snapshot.dependency_manifests:
            records.append(
                SnapshotRecord(
                    id=f"dependency-manifest:{manifest.path}",
                    kind=KnowledgeKind.DEPENDENCY_MANIFEST,
                    location=SourceLocation(manifest.path),
                    attributes={
                        "ecosystem": manifest.ecosystem,
                        "manifest_type": manifest.manifest_type,
                    },
                )
            )
        for hint in snapshot.test_command_hints:
            records.append(
                SnapshotRecord(
                    id=f"test-hint:{hint.source_path}:{hint.command}",
                    kind=KnowledgeKind.TEST_HINT,
                    location=SourceLocation(hint.source_path),
                    attributes={
                        "command": hint.command,
                        "language": language_by_path.get(hint.source_path),
                    },
                )
            )
        ids = [record.id for record in records]
        if len(ids) != len(set(ids)):
            raise ValueError("snapshot facts must have unique stable identifiers")
        return tuple(records)

    @staticmethod
    def _has_prefix(path: str, prefix: str) -> bool:
        return not prefix or path == prefix or path.startswith(prefix + "/")

    @staticmethod
    def _attribute_values(value: Any) -> Iterable[str]:
        if isinstance(value, Mapping):
            for key in sorted(value):
                yield str(key)
                yield from InMemoryRepositoryKnowledgeProvider._attribute_values(value[key])
        elif isinstance(value, (list, tuple)):
            for item in value:
                yield from InMemoryRepositoryKnowledgeProvider._attribute_values(item)
        elif value is not None:
            yield str(value)

    @classmethod
    def _search_text(cls, record: SnapshotRecord) -> str:
        values = [record.location.path, *record.keywords]
        values.extend(cls._attribute_values(record.attributes))
        return "\n".join(values).casefold()

    @classmethod
    def _term_score(cls, record: SnapshotRecord, terms: Sequence[str]) -> int:
        if not terms:
            return 0
        path = record.location.path.casefold()
        text = cls._search_text(record)
        return sum(3 if term in path else 1 for term in terms if term in text)

    @staticmethod
    def _attribute_matches(record: SnapshotRecord, name: str, values: Sequence[str]) -> bool:
        if not values:
            return True
        value = record.attributes.get(name)
        candidates = value if isinstance(value, (list, tuple)) else (value,)
        normalized = {
            str(candidate).casefold()
            for candidate in candidates
            if candidate is not None
        }
        return bool(normalized.intersection(values))

    def _result(self, record: SnapshotRecord, *, score: int, operation: str) -> KnowledgeResult:
        return KnowledgeResult(
            id=record.id,
            kind=record.kind,
            score=score,
            attributes=record.attributes,
            provenance=QueryProvenance(
                repository_revision=self._snapshot.repository_revision,
                snapshot_version=self._snapshot.snapshot_version,
                snapshot_created_at=self._snapshot.created_at,
                snapshot_producer=self._snapshot.scanner_version,
                source=record.location,
                traversal_path=(
                    f"snapshot:{self._snapshot.snapshot_version}",
                    f"{operation}:{record.id}",
                ),
            ),
        )

    @staticmethod
    def _ordered(
        records: Iterable[tuple[SnapshotRecord, int]], limit: int | None = None
    ) -> tuple[tuple[SnapshotRecord, int], ...]:
        ordered = sorted(
            records,
            key=lambda item: (
                -item[1],
                item[0].location.path.casefold(),
                item[0].location.path,
                item[0].id,
            ),
        )
        return tuple(ordered if limit is None else ordered[:limit])

    def lookup_file(self, query: FileQuery) -> tuple[KnowledgeResult, ...]:
        records = (
            (record, 0)
            for record in self._records
            if record.kind is KnowledgeKind.FILE and record.location.path == query.path
        )
        return tuple(
            self._result(record, score=score, operation="file")
            for record, score in self._ordered(records)
        )

    def lookup_documentation(
        self, query: DocumentationQuery
    ) -> tuple[KnowledgeResult, ...]:
        def selected() -> Iterable[tuple[SnapshotRecord, int]]:
            for record in self._records:
                if record.kind is not KnowledgeKind.DOCUMENTATION:
                    continue
                if not self._has_prefix(record.location.path, query.path_prefix):
                    continue
                score = self._term_score(record, query.terms)
                if query.terms and score == 0:
                    continue
                yield record, score

        return tuple(
            self._result(record, score=score, operation="documentation")
            for record, score in self._ordered(selected(), query.limit)
        )

    def lookup_dependency_manifests(
        self, query: DependencyManifestQuery
    ) -> tuple[KnowledgeResult, ...]:
        records = (
            (record, 0)
            for record in self._records
            if record.kind is KnowledgeKind.DEPENDENCY_MANIFEST
            and self._has_prefix(record.location.path, query.path_prefix)
            and self._attribute_matches(record, "ecosystem", query.ecosystems)
        )
        return tuple(
            self._result(record, score=score, operation="dependency-manifest")
            for record, score in self._ordered(records)
        )

    def lookup_test_hints(self, query: TestHintQuery) -> tuple[KnowledgeResult, ...]:
        records = (
            (record, 0)
            for record in self._records
            if record.kind is KnowledgeKind.TEST_HINT
            and self._has_prefix(record.location.path, query.path_prefix)
            and self._attribute_matches(record, "language", query.languages)
        )
        return tuple(
            self._result(record, score=score, operation="test-hint")
            for record, score in self._ordered(records)
        )

    def search_candidate_files(
        self, query: CandidateFileQuery
    ) -> tuple[KnowledgeResult, ...]:
        def selected() -> Iterable[tuple[SnapshotRecord, int]]:
            for record in self._records:
                if record.kind is not KnowledgeKind.FILE:
                    continue
                if not self._has_prefix(record.location.path, query.path_prefix):
                    continue
                if not query.include_tests and bool(record.attributes.get("is_test")):
                    continue
                if not self._attribute_matches(record, "language", query.languages):
                    continue
                score = self._term_score(record, query.terms)
                if query.terms and score == 0:
                    continue
                yield record, score

        return tuple(
            self._result(record, score=score, operation="candidate-file")
            for record, score in self._ordered(selected(), query.limit)
        )
