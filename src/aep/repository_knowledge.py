"""Deterministic MVP repository knowledge scanning."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import subprocess
from typing import Final


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
