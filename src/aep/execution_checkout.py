"""Trusted provisioning of revision-bound execution checkouts.

This module is control-plane infrastructure.  It is deliberately not a Tool:
Agents cannot select repositories, revisions, branches, or checkout paths.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import Enum
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
from threading import Event, Lock, Thread
from typing import Any, Iterator, Protocol
from urllib.parse import urlsplit
from uuid import uuid4


_REVISION = re.compile(r"^[0-9a-f]{40}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
class CheckoutFailureClass(str, Enum):
    """Failure classes understood by control-plane retry policy."""

    RECOVERABLE = "RECOVERABLE"
    CONFIGURATION = "CONFIGURATION"


class CheckoutState(str, Enum):
    PROVISIONING = "PROVISIONING"
    READY = "READY"
    CLEANUP_FAILED = "CLEANUP_FAILED"
    CLEANED = "CLEANED"
    FAILED = "FAILED"


class CheckoutProvisionError(RuntimeError):
    """A safe, classified checkout operation failure."""

    def __init__(
        self, classification: CheckoutFailureClass, code: str, message: str
    ) -> None:
        self.classification = classification
        self.code = code
        super().__init__(message)


class CheckoutClaimInProgress(CheckoutProvisionError):
    def __init__(self) -> None:
        super().__init__(
            CheckoutFailureClass.RECOVERABLE,
            "claim_in_progress",
            "checkout provisioning is owned by another worker",
        )


class CheckoutCacheClaimInProgress(CheckoutProvisionError):
    def __init__(self) -> None:
        super().__init__(
            CheckoutFailureClass.RECOVERABLE,
            "cache_claim_in_progress",
            "repository source cache is owned by another worker",
        )


class RepositorySourceFailure(CheckoutProvisionError):
    """Typed source result; the manager still replaces its message from a whitelist."""


@dataclass(frozen=True, slots=True)
class RepositoryIdentity:
    provider: str
    owner: str
    name: str

    def __post_init__(self) -> None:
        if not all(_IDENTIFIER.fullmatch(value) for value in (self.provider, self.owner, self.name)):
            raise CheckoutProvisionError(
                CheckoutFailureClass.CONFIGURATION,
                "unsafe_repository_identity",
                "repository identity contains unsafe characters",
            )

    @property
    def canonical(self) -> str:
        return f"{self.provider.casefold()}:{self.owner.casefold()}/{self.name.casefold()}"


@dataclass(frozen=True, slots=True)
class CheckoutRequest:
    execution_id: str
    repository: RepositoryIdentity
    default_branch: str
    base_revision: str
    knowledge_revision: str

    def __post_init__(self) -> None:
        if not self.execution_id or len(self.execution_id) > 256:
            raise CheckoutProvisionError(
                CheckoutFailureClass.CONFIGURATION,
                "unsafe_execution_id",
                "execution_id must contain between 1 and 256 characters",
            )
        if not _safe_branch(self.default_branch):
            raise CheckoutProvisionError(
                CheckoutFailureClass.CONFIGURATION,
                "unsafe_default_branch",
                "default branch is not a safe Git branch",
            )
        if not _REVISION.fullmatch(self.base_revision):
            raise CheckoutProvisionError(
                CheckoutFailureClass.CONFIGURATION,
                "invalid_revision",
                "base_revision must be a lowercase 40-character commit id",
            )
        if self.knowledge_revision != self.base_revision:
            raise CheckoutProvisionError(
                CheckoutFailureClass.CONFIGURATION,
                "knowledge_revision_mismatch",
                "repository knowledge and checkout revisions must match",
            )


@dataclass(frozen=True, slots=True)
class SourceRevision:
    cache_path: Path
    repository: RepositoryIdentity
    revision: str


@dataclass(frozen=True, slots=True)
class ExecutionCheckout:
    execution_id: str
    repository: RepositoryIdentity
    base_revision: str
    knowledge_revision: str
    branch: str
    workspace_path: Path
    source_cache_path: Path
    state: CheckoutState
    created_at: datetime
    updated_at: datetime
    fence_token: int = 0
    owner_token: str | None = None
    lease_expires_at: datetime | None = None
    cleanup_attempts: int = 0
    failure_class: CheckoutFailureClass | None = None
    failure_code: str | None = None
    failure_message: str | None = None

    def boundary_metadata(self) -> Mapping[str, str]:
        """Return the common immutable binding for downstream boundaries."""

        if self.state is not CheckoutState.READY:
            raise CheckoutProvisionError(
                CheckoutFailureClass.CONFIGURATION,
                "checkout_not_ready",
                "only a ready checkout can be supplied to execution boundaries",
            )
        return {
            "executionId": self.execution_id,
            "repository": self.repository.canonical,
            "repositoryRevision": self.base_revision,
            "knowledgeRevision": self.knowledge_revision,
            "branch": self.branch,
            "workspacePath": str(self.workspace_path),
        }

    def binding(self) -> "ExecutionCheckoutBinding":
        """Create the shared production binding consumed by execution boundaries."""

        metadata = self.boundary_metadata()
        return ExecutionCheckoutBinding(
            execution_id=metadata["executionId"],
            repository_id=metadata["repository"],
            repository_revision=metadata["repositoryRevision"],
            knowledge_revision=metadata["knowledgeRevision"],
            branch=metadata["branch"],
            workspace_path=Path(metadata["workspacePath"]),
        )


@dataclass(frozen=True, slots=True)
class ExecutionCheckoutBinding:
    """One immutable identity/revision binding for all checkout consumers."""

    execution_id: str
    repository_id: str
    repository_revision: str
    knowledge_revision: str
    branch: str
    workspace_path: Path

    @property
    def repository_knowledge_input(self) -> tuple[Path, str]:
        return self.workspace_path, self.knowledge_revision

    @property
    def filesystem_workspace(self) -> Path:
        return self.workspace_path

    @property
    def git_input(self) -> Mapping[str, object]:
        return {
            "repository": self.workspace_path,
            "repository_id": self.repository_id,
            "expected_revision": self.repository_revision,
            "working_branch": self.branch,
        }

    @property
    def docker_workspace(self) -> Path:
        return self.workspace_path

    @property
    def publication_evidence(self) -> Mapping[str, str]:
        return {
            "executionId": self.execution_id,
            "repository": self.repository_id,
            "repositoryRevision": self.repository_revision,
            "branch": self.branch,
        }

    def orchestration(self) -> "CheckoutBoundOrchestration":
        """Return the only supported seam for constructing checkout consumers."""

        return CheckoutBoundOrchestration(self)


class CheckoutBoundOrchestration:
    """Construct downstream boundaries from one checkout binding.

    Callers cannot independently supply a path or revision. Runtime maps that
    already carry these fields are validated before canonical values are added.
    """

    def __init__(self, binding: ExecutionCheckoutBinding) -> None:
        self._binding = binding

    def repository_context(
        self, scanner: Any, *, scanned_at: datetime | str | None = None
    ) -> tuple[Any, Any]:
        from aep.repository_knowledge import InMemoryRepositoryKnowledgeProvider

        snapshot = scanner.scan(
            self._binding.workspace_path,
            revision=self._binding.repository_revision,
            scanned_at=scanned_at,
        )
        if snapshot.repository_revision != self._binding.repository_revision:
            raise _configuration(
                "boundary_revision_mismatch",
                "repository scan returned a revision outside the checkout binding",
            )
        return snapshot, InMemoryRepositoryKnowledgeProvider(snapshot)

    def filesystem_adapter(self, **options: Any) -> Any:
        from aep.filesystem_tool import FilesystemToolAdapter

        return FilesystemToolAdapter(self._binding.workspace_path, **options)

    def git_adapter(
        self,
        *,
        log_store: Any,
        sandbox: Any,
        credential_provider: Any = None,
        remote: str = "origin",
    ) -> Any:
        from aep.git_tool import GitToolAdapter

        return GitToolAdapter(
            repository=self._binding.workspace_path,
            repository_id=self._binding.repository_id,
            expected_revision=self._binding.repository_revision,
            working_branch=self._binding.branch,
            log_store=log_store,
            sandbox=sandbox,
            credential_provider=credential_provider,
            remote=remote,
        )

    def docker_adapter(self, executor: Any) -> Any:
        from aep.docker_validation_tool import DockerValidationAdapter

        return DockerValidationAdapter(executor, self._binding.workspace_path)

    def task_execution_input(self, value: Mapping[str, Any]) -> dict[str, Any]:
        bound = self._validated(value, "TaskExecution")
        bound.update(
            {
                "workflowExecutionId": self._binding.execution_id,
                "workspacePath": str(self._binding.workspace_path),
                "repositoryRevision": self._binding.repository_revision,
                "knowledgeRevision": self._binding.knowledge_revision,
                "repository": self._binding.repository_id,
                "branch": self._binding.branch,
                "workingBranch": self._binding.branch,
            }
        )
        return bound

    def publication_input(self, value: Mapping[str, Any]) -> dict[str, Any]:
        bound = self._validated(value, "publication evidence")
        bound.update(self._binding.publication_evidence)
        bound["workspacePath"] = str(self._binding.workspace_path)
        bound["knowledgeRevision"] = self._binding.knowledge_revision
        bound["workingBranch"] = self._binding.branch
        return bound

    def _validated(self, value: Mapping[str, Any], label: str) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise _configuration(
                "invalid_boundary_input", f"{label} must be an object"
            )
        expected = {
            "executionId": self._binding.execution_id,
            "workflowExecutionId": self._binding.execution_id,
            "workspacePath": str(self._binding.workspace_path),
            "repositoryRevision": self._binding.repository_revision,
            "knowledgeRevision": self._binding.knowledge_revision,
            "repository": self._binding.repository_id,
            "branch": self._binding.branch,
            "workingBranch": self._binding.branch,
        }
        for field, canonical in expected.items():
            if field in value and value[field] != canonical:
                raise _configuration(
                    "boundary_identity_mismatch",
                    f"{label}.{field} differs from the execution checkout binding",
                )
        return dict(value)


class SourceCredentialLease(Protocol):
    @property
    def environment(self) -> Mapping[str, str]: ...

    def close(self) -> None: ...


class SourceCredentialProvider(Protocol):
    def acquire(self, *, repository: RepositoryIdentity) -> SourceCredentialLease: ...


class RepositorySource(Protocol):
    def materialize(
        self,
        *,
        repository: RepositoryIdentity,
        default_branch: str,
        expected_revision: str,
        cache_path: Path,
        credentials: Mapping[str, str],
        mutation_lease: Callable[[], AbstractContextManager[None]],
    ) -> SourceRevision: ...


class _EmptyLease:
    @property
    def environment(self) -> Mapping[str, str]:
        return {}

    def close(self) -> None:
        return None


class NoSourceCredentials:
    def acquire(self, *, repository: RepositoryIdentity) -> SourceCredentialLease:
        return _EmptyLease()


class GitRepositorySource:
    """Git-backed source cache with credentials injected only into processes."""

    def __init__(self, remote_url: str, *, timeout_seconds: float = 60.0) -> None:
        parsed = urlsplit(remote_url)
        if parsed.scheme in {"http", "https"} and (
            parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise CheckoutProvisionError(
                CheckoutFailureClass.CONFIGURATION,
                "credential_in_remote_url",
                "HTTP repository remote URL must not contain credentials, a query, or a fragment",
            )
        if not remote_url or "\n" in remote_url or "\r" in remote_url:
            raise CheckoutProvisionError(
                CheckoutFailureClass.CONFIGURATION,
                "unsafe_remote_url",
                "repository remote URL is invalid",
            )
        self._remote_url = remote_url
        self._timeout = timeout_seconds

    def materialize(
        self,
        *,
        repository: RepositoryIdentity,
        default_branch: str,
        expected_revision: str,
        cache_path: Path,
        credentials: Mapping[str, str],
        mutation_lease: Callable[[], AbstractContextManager[None]],
    ) -> SourceRevision:
        environment = {str(key): str(value) for key, value in credentials.items()}
        environment.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "credential.helper",
                "GIT_CONFIG_VALUE_0": "",
            }
        )
        with mutation_lease():
            cache_path.parent.mkdir(parents=True, exist_ok=True)
        if cache_path.exists():
            if not cache_path.is_dir():
                raise _source_configuration("unsafe_source_cache")
            bare = self._git(cache_path, ("rev-parse", "--is-bare-repository"), environment)
            if bare.strip() != "true":
                raise _source_configuration("invalid_source_cache")
            origin = self._git(cache_path, ("remote", "get-url", "origin"), environment)
            if origin.strip() != self._remote_url:
                raise _source_configuration("source_identity_mismatch")
        else:
            with mutation_lease():
                self._run(
                    ("git", "clone", "--bare", "--no-tags", "--", self._remote_url, str(cache_path)),
                    cwd=cache_path.parent,
                    environment=environment,
                )
        with mutation_lease():
            self._git(
                cache_path,
                ("fetch", "--no-tags", "--prune", "origin", f"+refs/heads/{default_branch}:refs/remotes/origin/{default_branch}"),
                environment,
            )
        revision = self._git(
            cache_path,
            ("rev-parse", "--verify", "--end-of-options", f"refs/remotes/origin/{default_branch}^{{commit}}"),
            environment,
        ).strip().casefold()
        if not _REVISION.fullmatch(revision):
            raise _source_recoverable("source_revision_invalid")
        commit = subprocess.run(
            ("git", "-C", str(cache_path), "cat-file", "-e", f"{expected_revision}^{{commit}}"),
            env={**os.environ, **environment},
            capture_output=True,
            check=False,
            timeout=self._timeout,
        )
        if commit.returncode != 0:
            raise _source_configuration("missing_commit")
        return SourceRevision(cache_path.resolve(), repository, revision)

    def _git(self, cwd: Path, arguments: Sequence[str], environment: Mapping[str, str]) -> str:
        return self._run(("git", "-C", str(cwd), *arguments), cwd=cwd, environment=environment)

    def _run(self, arguments: Sequence[str], *, cwd: Path, environment: Mapping[str, str]) -> str:
        try:
            result = subprocess.run(
                arguments,
                cwd=cwd,
                env={**os.environ, **environment},
                capture_output=True,
                check=False,
                timeout=self._timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise _source_recoverable("source_unavailable") from error
        if result.returncode:
            raise _source_recoverable("source_operation_failed")
        return result.stdout.decode("utf-8", errors="replace")


class CheckoutRegistry(Protocol):
    def get(self, execution_id: str) -> ExecutionCheckout | None: ...

    def claim(self, candidate: ExecutionCheckout, *, now: datetime) -> ExecutionCheckout: ...

    def renew(
        self,
        execution_id: str,
        *,
        owner_token: str,
        fence_token: int,
        now: datetime,
        lease_expires_at: datetime,
    ) -> ExecutionCheckout: ...

    def begin_cleanup(
        self,
        execution_id: str,
        *,
        owner_token: str,
        now: datetime,
        lease_expires_at: datetime,
    ) -> ExecutionCheckout: ...

    def update(
        self,
        checkout: ExecutionCheckout,
        *,
        owner_token: str,
        fence_token: int,
        now: datetime,
    ) -> None: ...

    def claim_cache(
        self,
        repository_id: str,
        *,
        execution_id: str,
        owner_token: str,
        execution_fence: int,
        now: datetime,
        lease_expires_at: datetime,
    ) -> int: ...

    def renew_cache(
        self,
        repository_id: str,
        *,
        execution_id: str,
        owner_token: str,
        execution_fence: int,
        cache_fence: int,
        now: datetime,
        lease_expires_at: datetime,
    ) -> None: ...

    def release_cache(
        self, repository_id: str, *, owner_token: str, cache_fence: int
    ) -> None: ...


class InMemoryCheckoutRegistry:
    """Atomic reference registry; production deployments may replace it durably."""

    def __init__(self) -> None:
        self._records: dict[str, ExecutionCheckout] = {}
        self._paths: dict[Path, str] = {}
        self._branches: dict[tuple[str, str], str] = {}
        self._cache_claims: dict[
            str, tuple[str, int, datetime, str, int]
        ] = {}
        self._cache_fences: dict[str, int] = {}
        self._lock = Lock()

    def get(self, execution_id: str) -> ExecutionCheckout | None:
        with self._lock:
            return self._records.get(execution_id)

    def claim(self, candidate: ExecutionCheckout, *, now: datetime) -> ExecutionCheckout:
        with self._lock:
            existing = self._records.get(candidate.execution_id)
            if existing is not None:
                _same_binding(existing, candidate)
                if existing.state is CheckoutState.READY:
                    return existing
                if (
                    existing.state is CheckoutState.PROVISIONING
                    and existing.lease_expires_at is not None
                    and existing.lease_expires_at > now
                ):
                    raise CheckoutClaimInProgress()
                candidate = replace(
                    candidate,
                    created_at=existing.created_at,
                    fence_token=existing.fence_token + 1,
                )
            else:
                candidate = replace(candidate, fence_token=1)
            path_owner = self._paths.get(candidate.workspace_path)
            branch_owner = self._branches.get((candidate.repository.canonical, candidate.branch))
            if path_owner not in (None, candidate.execution_id) or branch_owner not in (None, candidate.execution_id):
                raise _configuration("checkout_reuse", "checkout path or branch is assigned to another execution")
            self._records[candidate.execution_id] = candidate
            self._paths[candidate.workspace_path] = candidate.execution_id
            self._branches[(candidate.repository.canonical, candidate.branch)] = candidate.execution_id
            return candidate

    def renew(
        self,
        execution_id: str,
        *,
        owner_token: str,
        fence_token: int,
        now: datetime,
        lease_expires_at: datetime,
    ) -> ExecutionCheckout:
        with self._lock:
            current = self._records.get(execution_id)
            if not _owns(current, owner_token, fence_token, now):
                raise CheckoutClaimInProgress()
            renewed = replace(
                current, lease_expires_at=lease_expires_at, updated_at=now
            )
            self._records[execution_id] = renewed
            return renewed

    def begin_cleanup(
        self,
        execution_id: str,
        *,
        owner_token: str,
        now: datetime,
        lease_expires_at: datetime,
    ) -> ExecutionCheckout:
        with self._lock:
            current = self._records.get(execution_id)
            if current is None or current.state not in {
                CheckoutState.READY,
                CheckoutState.CLEANUP_FAILED,
            }:
                raise CheckoutClaimInProgress()
            claimed = replace(
                current,
                state=CheckoutState.PROVISIONING,
                owner_token=owner_token,
                fence_token=current.fence_token + 1,
                lease_expires_at=lease_expires_at,
                updated_at=now,
            )
            self._records[execution_id] = claimed
            return claimed

    def update(
        self,
        checkout: ExecutionCheckout,
        *,
        owner_token: str,
        fence_token: int,
        now: datetime,
    ) -> None:
        with self._lock:
            current = self._records.get(checkout.execution_id)
            if not _owns(current, owner_token, fence_token, now):
                raise CheckoutClaimInProgress()
            self._records[checkout.execution_id] = checkout

    def claim_cache(
        self,
        repository_id: str,
        *,
        execution_id: str,
        owner_token: str,
        execution_fence: int,
        now: datetime,
        lease_expires_at: datetime,
    ) -> int:
        with self._lock:
            if not _owns(
                self._records.get(execution_id),
                owner_token,
                execution_fence,
                now,
            ):
                raise CheckoutClaimInProgress()
            existing = self._cache_claims.get(repository_id)
            if existing is not None and existing[2] > now:
                raise CheckoutCacheClaimInProgress()
            cache_fence = self._cache_fences.get(repository_id, 0) + 1
            self._cache_fences[repository_id] = cache_fence
            self._cache_claims[repository_id] = (
                owner_token,
                cache_fence,
                lease_expires_at,
                execution_id,
                execution_fence,
            )
            return cache_fence

    def renew_cache(
        self,
        repository_id: str,
        *,
        execution_id: str,
        owner_token: str,
        execution_fence: int,
        cache_fence: int,
        now: datetime,
        lease_expires_at: datetime,
    ) -> None:
        with self._lock:
            existing = self._cache_claims.get(repository_id)
            if (
                existing is None
                or existing[0] != owner_token
                or existing[1] != cache_fence
                or existing[2] <= now
                or existing[3] != execution_id
                or existing[4] != execution_fence
                or not _owns(
                    self._records.get(execution_id),
                    owner_token,
                    execution_fence,
                    now,
                )
            ):
                raise CheckoutCacheClaimInProgress()
            self._cache_claims[repository_id] = (
                owner_token,
                cache_fence,
                lease_expires_at,
                execution_id,
                execution_fence,
            )

    def release_cache(
        self, repository_id: str, *, owner_token: str, cache_fence: int
    ) -> None:
        with self._lock:
            existing = self._cache_claims.get(repository_id)
            if existing is None:
                return
            if existing[:2] != (owner_token, cache_fence):
                raise CheckoutCacheClaimInProgress()
            del self._cache_claims[repository_id]


class SqliteCheckoutRegistry:
    """Durable atomic registry with database-enforced uniqueness and fencing."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path.resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS execution_checkouts (
                    execution_id TEXT PRIMARY KEY,
                    repository_id TEXT NOT NULL,
                    branch TEXT NOT NULL,
                    workspace_path TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    owner_token TEXT,
                    fence_token INTEGER NOT NULL,
                    lease_expires_at TEXT,
                    payload TEXT NOT NULL,
                    UNIQUE(repository_id, branch)
                );
                CREATE TABLE IF NOT EXISTS repository_cache_claims (
                    repository_id TEXT PRIMARY KEY,
                    execution_id TEXT,
                    execution_fence INTEGER,
                    owner_token TEXT,
                    fence_token INTEGER NOT NULL,
                    lease_expires_at TEXT
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    def get(self, execution_id: str) -> ExecutionCheckout | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM execution_checkouts WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
        return None if row is None else _checkout_from_json(str(row["payload"]))

    def claim(self, candidate: ExecutionCheckout, *, now: datetime) -> ExecutionCheckout:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT payload FROM execution_checkouts WHERE execution_id = ?",
                (candidate.execution_id,),
            ).fetchone()
            if row is not None:
                existing = _checkout_from_json(str(row["payload"]))
                _same_binding(existing, candidate)
                if existing.state is CheckoutState.READY:
                    return existing
                if (
                    existing.state is CheckoutState.PROVISIONING
                    and existing.lease_expires_at is not None
                    and existing.lease_expires_at > now
                ):
                    raise CheckoutClaimInProgress()
                candidate = replace(
                    candidate,
                    created_at=existing.created_at,
                    fence_token=existing.fence_token + 1,
                )
            else:
                candidate = replace(candidate, fence_token=1)
            try:
                self._put(connection, candidate)
            except sqlite3.IntegrityError as error:
                raise _configuration(
                    "checkout_reuse",
                    "checkout path or branch is assigned to another execution",
                ) from error
            return candidate

    def renew(
        self,
        execution_id: str,
        *,
        owner_token: str,
        fence_token: int,
        now: datetime,
        lease_expires_at: datetime,
    ) -> ExecutionCheckout:
        with self._transaction() as connection:
            current = self._load_locked(connection, execution_id)
            if not _owns(current, owner_token, fence_token, now):
                raise CheckoutClaimInProgress()
            renewed = replace(
                current, lease_expires_at=lease_expires_at, updated_at=now
            )
            self._put(connection, renewed)
            return renewed

    def begin_cleanup(
        self,
        execution_id: str,
        *,
        owner_token: str,
        now: datetime,
        lease_expires_at: datetime,
    ) -> ExecutionCheckout:
        with self._transaction() as connection:
            current = self._load_locked(connection, execution_id)
            if current is None or current.state not in {
                CheckoutState.READY,
                CheckoutState.CLEANUP_FAILED,
            }:
                raise CheckoutClaimInProgress()
            claimed = replace(
                current,
                state=CheckoutState.PROVISIONING,
                owner_token=owner_token,
                fence_token=current.fence_token + 1,
                lease_expires_at=lease_expires_at,
                updated_at=now,
            )
            self._put(connection, claimed)
            return claimed

    def update(
        self,
        checkout: ExecutionCheckout,
        *,
        owner_token: str,
        fence_token: int,
        now: datetime,
    ) -> None:
        with self._transaction() as connection:
            current = self._load_locked(connection, checkout.execution_id)
            if not _owns(current, owner_token, fence_token, now):
                raise CheckoutClaimInProgress()
            self._put(connection, checkout)

    def claim_cache(
        self,
        repository_id: str,
        *,
        execution_id: str,
        owner_token: str,
        execution_fence: int,
        now: datetime,
        lease_expires_at: datetime,
    ) -> int:
        with self._transaction() as connection:
            checkout = self._load_locked(connection, execution_id)
            if not _owns(checkout, owner_token, execution_fence, now):
                raise CheckoutClaimInProgress()
            row = connection.execute(
                "SELECT * FROM repository_cache_claims WHERE repository_id = ?",
                (repository_id,),
            ).fetchone()
            if (
                row is not None
                and row["owner_token"] is not None
                and _parse_datetime(str(row["lease_expires_at"])) > now
            ):
                raise CheckoutCacheClaimInProgress()
            fence = 1 if row is None else int(row["fence_token"]) + 1
            connection.execute(
                """
                INSERT INTO repository_cache_claims
                    (repository_id, execution_id, execution_fence, owner_token,
                     fence_token, lease_expires_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(repository_id) DO UPDATE SET
                    execution_id=excluded.execution_id,
                    execution_fence=excluded.execution_fence,
                    owner_token=excluded.owner_token,
                    fence_token=excluded.fence_token,
                    lease_expires_at=excluded.lease_expires_at
                """,
                (
                    repository_id,
                    execution_id,
                    execution_fence,
                    owner_token,
                    fence,
                    _format_datetime(lease_expires_at),
                ),
            )
            return fence

    def renew_cache(
        self,
        repository_id: str,
        *,
        execution_id: str,
        owner_token: str,
        execution_fence: int,
        cache_fence: int,
        now: datetime,
        lease_expires_at: datetime,
    ) -> None:
        with self._transaction() as connection:
            checkout = self._load_locked(connection, execution_id)
            row = connection.execute(
                "SELECT * FROM repository_cache_claims WHERE repository_id = ?",
                (repository_id,),
            ).fetchone()
            if (
                row is None
                or row["owner_token"] != owner_token
                or int(row["fence_token"]) != cache_fence
                or row["execution_id"] != execution_id
                or int(row["execution_fence"]) != execution_fence
                or _parse_datetime(str(row["lease_expires_at"])) <= now
                or not _owns(checkout, owner_token, execution_fence, now)
            ):
                raise CheckoutCacheClaimInProgress()
            connection.execute(
                "UPDATE repository_cache_claims SET lease_expires_at = ? WHERE repository_id = ?",
                (_format_datetime(lease_expires_at), repository_id),
            )

    def release_cache(
        self, repository_id: str, *, owner_token: str, cache_fence: int
    ) -> None:
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE repository_cache_claims
                SET execution_id = NULL, execution_fence = NULL,
                    owner_token = NULL, lease_expires_at = NULL
                WHERE repository_id = ? AND owner_token = ? AND fence_token = ?
                """,
                (repository_id, owner_token, cache_fence),
            )
            if cursor.rowcount != 1:
                raise CheckoutCacheClaimInProgress()

    def _load_locked(
        self, connection: sqlite3.Connection, execution_id: str
    ) -> ExecutionCheckout | None:
        row = connection.execute(
            "SELECT payload FROM execution_checkouts WHERE execution_id = ?",
            (execution_id,),
        ).fetchone()
        return None if row is None else _checkout_from_json(str(row["payload"]))

    @staticmethod
    def _put(connection: sqlite3.Connection, checkout: ExecutionCheckout) -> None:
        connection.execute(
            """
            INSERT INTO execution_checkouts
                (execution_id, repository_id, branch, workspace_path, state,
                 owner_token, fence_token, lease_expires_at, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(execution_id) DO UPDATE SET
                repository_id=excluded.repository_id,
                branch=excluded.branch,
                workspace_path=excluded.workspace_path,
                state=excluded.state,
                owner_token=excluded.owner_token,
                fence_token=excluded.fence_token,
                lease_expires_at=excluded.lease_expires_at,
                payload=excluded.payload
            """,
            (
                checkout.execution_id,
                checkout.repository.canonical,
                checkout.branch,
                str(checkout.workspace_path),
                checkout.state.value,
                checkout.owner_token,
                checkout.fence_token,
                _format_datetime(checkout.lease_expires_at),
                _checkout_to_json(checkout),
            ),
        )

    class _Transaction:
        def __init__(self, registry: "SqliteCheckoutRegistry") -> None:
            self.connection = registry._connect()

        def __enter__(self) -> sqlite3.Connection:
            self.connection.execute("BEGIN IMMEDIATE")
            return self.connection

        def __exit__(self, exc_type, exc, traceback) -> None:
            try:
                self.connection.execute("COMMIT" if exc_type is None else "ROLLBACK")
            finally:
                self.connection.close()

    def _transaction(self) -> "SqliteCheckoutRegistry._Transaction":
        return self._Transaction(self)


def local_checkout_registry(state_root: Path) -> SqliteCheckoutRegistry:
    """Open the durable registry carried by the local ``aep-state`` volume."""

    return SqliteCheckoutRegistry(
        state_root.resolve() / "checkout-manager" / "registry.sqlite3"
    )


class ExecutionCheckoutManager:
    """Provision and retire one isolated worktree per WorkflowExecution."""

    def __init__(
        self,
        *,
        source: RepositorySource,
        source_cache_root: Path,
        worktree_root: Path,
        registry: CheckoutRegistry,
        credential_provider: SourceCredentialProvider | None = None,
        claim_ttl: timedelta = timedelta(minutes=2),
        clock=lambda: datetime.now(UTC),
    ) -> None:
        self._source = source
        self._cache_root = source_cache_root.resolve()
        self._worktree_root = worktree_root.resolve()
        if self._cache_root == self._worktree_root or _within(self._cache_root, self._worktree_root) or _within(self._worktree_root, self._cache_root):
            raise _configuration("overlapping_storage_roots", "source-cache and worktree roots must not overlap")
        if claim_ttl <= timedelta(0):
            raise _configuration("invalid_claim_ttl", "claim_ttl must be positive")
        self._registry = registry
        self._credentials = credential_provider or NoSourceCredentials()
        self._claim_ttl = claim_ttl
        self._clock = clock

    def provision(self, request: CheckoutRequest) -> ExecutionCheckout:
        existing = self._registry.get(request.execution_id)
        if existing is not None and existing.state is CheckoutState.READY:
            self._verify_request(existing, request)
            self._validate_ready(existing)
            return existing

        digest = sha256(f"{request.repository.canonical}\0{request.execution_id}".encode()).hexdigest()
        branch = f"aep/execution/{digest[:20]}"
        workspace = (self._worktree_root / f"execution-{digest[:24]}").resolve()
        cache = (self._cache_root / f"repository-{sha256(request.repository.canonical.encode()).hexdigest()[:24]}").resolve()
        self._require_child(workspace, self._worktree_root, "unsafe_worktree_path")
        self._require_child(cache, self._cache_root, "unsafe_source_cache_path")

        now = self._now()
        owner = uuid4().hex
        candidate = ExecutionCheckout(
            execution_id=request.execution_id,
            repository=request.repository,
            base_revision=request.base_revision,
            knowledge_revision=request.knowledge_revision,
            branch=branch,
            workspace_path=workspace,
            source_cache_path=cache,
            state=CheckoutState.PROVISIONING,
            created_at=now,
            updated_at=now,
            owner_token=owner,
            lease_expires_at=now + self._claim_ttl,
        )
        claimed = self._registry.claim(candidate, now=now)
        if claimed.state is CheckoutState.READY:
            self._validate_ready(claimed)
            return claimed

        lease: SourceCredentialLease | None = None
        cache_fence: int | None = None

        def renew_fences() -> None:
            nonlocal claimed
            renewed_at = self._now()
            expires_at = renewed_at + self._claim_ttl
            claimed = self._registry.renew(
                request.execution_id,
                owner_token=owner,
                fence_token=claimed.fence_token,
                now=renewed_at,
                lease_expires_at=expires_at,
            )
            if cache_fence is not None:
                self._registry.renew_cache(
                    request.repository.canonical,
                    execution_id=request.execution_id,
                    owner_token=owner,
                    execution_fence=claimed.fence_token,
                    cache_fence=cache_fence,
                    now=renewed_at,
                    lease_expires_at=expires_at,
                )

        try:
            cache_fence = self._registry.claim_cache(
                request.repository.canonical,
                execution_id=request.execution_id,
                owner_token=owner,
                execution_fence=claimed.fence_token,
                now=self._now(),
                lease_expires_at=self._now() + self._claim_ttl,
            )
            credential_failure: CheckoutProvisionError | None = None
            try:
                lease = self._credentials.acquire(repository=request.repository)
                credential_environment = dict(lease.environment)
            except Exception:
                credential_failure = _recoverable(
                    "credential_boundary_failure",
                    "repository credential boundary failed",
                )
            if credential_failure is not None:
                raise credential_failure from None
            source_failure: CheckoutProvisionError | None = None
            try:
                source_revision = self._source.materialize(
                    repository=request.repository,
                    default_branch=request.default_branch,
                    expected_revision=request.base_revision,
                    cache_path=cache,
                    credentials=credential_environment,
                    mutation_lease=lambda: self._lease_heartbeat(renew_fences),
                )
            except Exception as error:
                source_failure = _normalize_source_failure(error)
            if source_failure is not None:
                raise source_failure from None
            if source_revision.repository != request.repository or source_revision.cache_path.resolve() != cache:
                raise _configuration("source_identity_mismatch", "repository source returned unexpected identity or cache path")
            if source_revision.revision != request.base_revision:
                raise _configuration("revision_drift", "default branch revision differs from recorded WorkflowExecution revision")
            self._recover_or_create(
                claimed,
                mutation_lease=lambda: self._lease_heartbeat(renew_fences),
            )
            renew_fences()
            ready = replace(
                claimed,
                state=CheckoutState.READY,
                updated_at=self._now(),
                owner_token=None,
                lease_expires_at=None,
                failure_class=None,
                failure_code=None,
                failure_message=None,
            )
            self._registry.update(
                ready,
                owner_token=owner,
                fence_token=claimed.fence_token,
                now=self._now(),
            )
            return ready
        except CheckoutProvisionError as error:
            failed = replace(
                claimed,
                state=CheckoutState.FAILED,
                updated_at=self._now(),
                owner_token=None,
                lease_expires_at=None,
                failure_class=error.classification,
                failure_code=error.code,
                failure_message=_persisted_failure_message(error),
            )
            self._try_record_terminal(failed, owner, claimed.fence_token)
            raise
        except Exception:
            wrapped = _recoverable(
                "credential_or_source_failure",
                "credential or repository source operation failed",
            )
            failed = replace(
                claimed,
                state=CheckoutState.FAILED,
                updated_at=self._now(),
                owner_token=None,
                lease_expires_at=None,
                failure_class=wrapped.classification,
                failure_code=wrapped.code,
                failure_message=str(wrapped),
            )
            self._try_record_terminal(failed, owner, claimed.fence_token)
            raise wrapped from None
        finally:
            if cache_fence is not None:
                try:
                    self._registry.release_cache(
                        request.repository.canonical,
                        owner_token=owner,
                        cache_fence=cache_fence,
                    )
                except CheckoutProvisionError:
                    pass
            if lease is not None:
                try:
                    lease.close()
                except Exception:
                    pass

    def provision_orchestration(
        self, request: CheckoutRequest
    ) -> tuple[ExecutionCheckout, CheckoutBoundOrchestration]:
        """Provision and return the canonical production consumer seam."""

        checkout = self.provision(request)
        return checkout, checkout.binding().orchestration()

    def cleanup(self, execution_id: str, *, terminal_evidence_durable: bool) -> ExecutionCheckout:
        checkout = self._registry.get(execution_id)
        if checkout is None:
            raise _configuration("checkout_not_found", "execution checkout does not exist")
        if not terminal_evidence_durable:
            raise _configuration("terminal_evidence_not_durable", "checkout cleanup requires durable terminal evidence")
        if checkout.state is CheckoutState.CLEANED:
            return checkout
        if checkout.state not in {CheckoutState.READY, CheckoutState.CLEANUP_FAILED}:
            raise _configuration(
                "checkout_not_ready",
                "only a ready or recovery-retained checkout can be cleaned",
            )
        owner = uuid4().hex
        now = self._now()
        claimed = self._registry.begin_cleanup(
            execution_id,
            owner_token=owner,
            now=now,
            lease_expires_at=now + self._claim_ttl,
        )
        try:
            cache_fence = self._registry.claim_cache(
                checkout.repository.canonical,
                execution_id=execution_id,
                owner_token=owner,
                execution_fence=claimed.fence_token,
                now=now,
                lease_expires_at=now + self._claim_ttl,
            )
        except CheckoutProvisionError as error:
            failed = replace(
                claimed,
                state=CheckoutState.CLEANUP_FAILED,
                updated_at=self._now(),
                owner_token=None,
                lease_expires_at=None,
                cleanup_attempts=checkout.cleanup_attempts + 1,
                failure_class=error.classification,
                failure_code=error.code,
                failure_message=_persisted_failure_message(error),
            )
            self._try_record_terminal(failed, owner, claimed.fence_token)
            return failed

        def renew_fences() -> None:
            nonlocal claimed
            renewed_at = self._now()
            expires_at = renewed_at + self._claim_ttl
            claimed = self._registry.renew(
                execution_id,
                owner_token=owner,
                fence_token=claimed.fence_token,
                now=renewed_at,
                lease_expires_at=expires_at,
            )
            self._registry.renew_cache(
                checkout.repository.canonical,
                execution_id=execution_id,
                owner_token=owner,
                execution_fence=claimed.fence_token,
                cache_fence=cache_fence,
                now=renewed_at,
                lease_expires_at=expires_at,
            )

        try:
            if checkout.workspace_path.exists():
                self._validate_identity(checkout)
                status = _git(checkout.workspace_path, "status", "--porcelain=v1", "--untracked-files=all")
                if status.strip():
                    raise _recoverable("dirty_checkout", "dirty execution checkout retained for operator recovery")
                with self._lease_heartbeat(renew_fences):
                    _git(checkout.source_cache_path, "worktree", "remove", "--", str(checkout.workspace_path))
            branch_exists = subprocess.run(
                ("git", "-C", str(checkout.source_cache_path), "show-ref", "--verify", "--quiet", f"refs/heads/{checkout.branch}"),
                capture_output=True,
                check=False,
            ).returncode == 0
            if branch_exists:
                with self._lease_heartbeat(renew_fences):
                    _git(checkout.source_cache_path, "branch", "-D", "--", checkout.branch)
            renew_fences()
            cleaned = replace(claimed, state=CheckoutState.CLEANED, updated_at=self._now(), owner_token=None, lease_expires_at=None, cleanup_attempts=checkout.cleanup_attempts + 1)
            self._registry.update(
                cleaned,
                owner_token=owner,
                fence_token=claimed.fence_token,
                now=self._now(),
            )
            return cleaned
        except CheckoutProvisionError as error:
            failed = replace(claimed, state=CheckoutState.CLEANUP_FAILED, updated_at=self._now(), owner_token=None, lease_expires_at=None, cleanup_attempts=checkout.cleanup_attempts + 1, failure_class=error.classification, failure_code=error.code, failure_message=_persisted_failure_message(error))
            self._try_record_terminal(failed, owner, claimed.fence_token)
            return failed
        finally:
            try:
                self._registry.release_cache(
                    checkout.repository.canonical,
                    owner_token=owner,
                    cache_fence=cache_fence,
                )
            except CheckoutProvisionError:
                pass

    def _recover_or_create(
        self,
        checkout: ExecutionCheckout,
        *,
        mutation_lease: Callable[[], AbstractContextManager[None]],
    ) -> None:
        path = checkout.workspace_path
        if path.exists():
            # Never force-remove an unknown directory or a worktree whose
            # execution-scoped identity has drifted.  Only a verified partial
            # checkout may be replaced during interrupted-claim recovery.
            self._validate_identity(checkout)
            try:
                self._validate_initial(checkout)
                return
            except CheckoutProvisionError:
                try:
                    with mutation_lease():
                        _git(checkout.source_cache_path, "worktree", "remove", "--force", "--", str(path))
                except CheckoutProvisionError as error:
                    raise _recoverable(
                        "interrupted_provisioning",
                        "interrupted checkout could not be recovered safely",
                    ) from error
        branch_exists = subprocess.run(
            ("git", "-C", str(checkout.source_cache_path), "show-ref", "--verify", "--quiet", f"refs/heads/{checkout.branch}"),
            capture_output=True,
            check=False,
        ).returncode == 0
        if branch_exists:
            with mutation_lease():
                _git(checkout.source_cache_path, "branch", "-D", "--", checkout.branch)
        with mutation_lease():
            path.parent.mkdir(parents=True, exist_ok=True)
        with mutation_lease():
            _git(checkout.source_cache_path, "worktree", "add", "-b", checkout.branch, "--", str(path), checkout.base_revision)
        self._validate_initial(checkout)

    @contextmanager
    def _lease_heartbeat(
        self, renew: Callable[[], None]
    ) -> Iterator[None]:
        """Keep ownership live for the complete duration of one mutation."""

        renew()
        stop = Event()
        failures: list[CheckoutProvisionError] = []
        interval = max(0.01, self._claim_ttl.total_seconds() / 3)

        def heartbeat() -> None:
            while not stop.wait(interval):
                try:
                    renew()
                except Exception:
                    failures.append(CheckoutClaimInProgress())
                    stop.set()

        thread = Thread(
            target=heartbeat,
            name="aep-checkout-lease-heartbeat",
            daemon=True,
        )
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=max(1.0, interval * 2))
        if failures:
            raise failures[0]

    def _try_record_terminal(
        self, checkout: ExecutionCheckout, owner_token: str, fence_token: int
    ) -> None:
        try:
            self._registry.update(
                checkout,
                owner_token=owner_token,
                fence_token=fence_token,
                now=self._now(),
            )
        except CheckoutProvisionError:
            # A newer fenced owner is authoritative.  The stale worker must not
            # overwrite its state, even with failure evidence.
            pass

    def _validate_initial(self, checkout: ExecutionCheckout) -> None:
        self._validate_identity(checkout)
        head = _git(checkout.workspace_path, "rev-parse", "HEAD").strip().casefold()
        if head != checkout.base_revision:
            raise _configuration("revision_drift", "new checkout HEAD differs from recorded revision")
        if _git(checkout.workspace_path, "status", "--porcelain=v1", "--untracked-files=all").strip():
            raise _configuration("dirty_checkout", "new checkout is not clean")

    def _validate_ready(self, checkout: ExecutionCheckout) -> None:
        self._validate_identity(checkout)
        result = subprocess.run(
            ("git", "-C", str(checkout.workspace_path), "merge-base", "--is-ancestor", checkout.base_revision, "HEAD"),
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise _configuration("revision_drift", "checkout no longer descends from its recorded base revision")

    def _validate_identity(self, checkout: ExecutionCheckout) -> None:
        if not checkout.workspace_path.is_dir():
            raise _recoverable("checkout_missing", "recorded execution checkout is missing")
        root = Path(_git(checkout.workspace_path, "rev-parse", "--show-toplevel").strip()).resolve()
        branch = _git(checkout.workspace_path, "symbolic-ref", "--short", "HEAD").strip()
        if root != checkout.workspace_path or branch != checkout.branch:
            raise _configuration("checkout_identity_drift", "checkout path or branch differs from its recorded binding")

    @staticmethod
    def _verify_request(checkout: ExecutionCheckout, request: CheckoutRequest) -> None:
        if (
            checkout.repository != request.repository
            or checkout.base_revision != request.base_revision
            or checkout.knowledge_revision != request.knowledge_revision
        ):
            raise _configuration("execution_identity_conflict", "execution id was reused with different immutable inputs")

    @staticmethod
    def _require_child(path: Path, root: Path, code: str) -> None:
        if path == root or not _within(path, root):
            raise _configuration(code, "derived storage path escapes its configured root")

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise _configuration("naive_clock", "checkout manager clock must be timezone-aware")
        return value.astimezone(UTC)


def _same_binding(left: ExecutionCheckout, right: ExecutionCheckout) -> None:
    if any(
        (
            left.repository != right.repository,
            left.base_revision != right.base_revision,
            left.knowledge_revision != right.knowledge_revision,
            left.branch != right.branch,
            left.workspace_path != right.workspace_path,
            left.source_cache_path != right.source_cache_path,
        )
    ):
        raise _configuration("execution_identity_conflict", "execution id was reused with different immutable inputs")


def _owns(
    checkout: ExecutionCheckout | None,
    owner_token: str,
    fence_token: int,
    now: datetime,
) -> bool:
    return bool(
        checkout is not None
        and checkout.state is CheckoutState.PROVISIONING
        and checkout.owner_token == owner_token
        and checkout.fence_token == fence_token
        and checkout.lease_expires_at is not None
        and checkout.lease_expires_at > now
    )


def _format_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _checkout_to_json(checkout: ExecutionCheckout) -> str:
    return json.dumps(
        {
            "executionId": checkout.execution_id,
            "repository": {
                "provider": checkout.repository.provider,
                "owner": checkout.repository.owner,
                "name": checkout.repository.name,
            },
            "baseRevision": checkout.base_revision,
            "knowledgeRevision": checkout.knowledge_revision,
            "branch": checkout.branch,
            "workspacePath": str(checkout.workspace_path),
            "sourceCachePath": str(checkout.source_cache_path),
            "state": checkout.state.value,
            "createdAt": _format_datetime(checkout.created_at),
            "updatedAt": _format_datetime(checkout.updated_at),
            "fenceToken": checkout.fence_token,
            "ownerToken": checkout.owner_token,
            "leaseExpiresAt": _format_datetime(checkout.lease_expires_at),
            "cleanupAttempts": checkout.cleanup_attempts,
            "failureClass": (
                checkout.failure_class.value if checkout.failure_class else None
            ),
            "failureCode": checkout.failure_code,
            "failureMessage": checkout.failure_message,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _checkout_from_json(value: str) -> ExecutionCheckout:
    data = json.loads(value)
    repository = data["repository"]
    return ExecutionCheckout(
        execution_id=data["executionId"],
        repository=RepositoryIdentity(
            repository["provider"], repository["owner"], repository["name"]
        ),
        base_revision=data["baseRevision"],
        knowledge_revision=data["knowledgeRevision"],
        branch=data["branch"],
        workspace_path=Path(data["workspacePath"]),
        source_cache_path=Path(data["sourceCachePath"]),
        state=CheckoutState(data["state"]),
        created_at=_parse_datetime(data["createdAt"]),
        updated_at=_parse_datetime(data["updatedAt"]),
        fence_token=int(data["fenceToken"]),
        owner_token=data.get("ownerToken"),
        lease_expires_at=(
            _parse_datetime(data["leaseExpiresAt"])
            if data.get("leaseExpiresAt")
            else None
        ),
        cleanup_attempts=int(data.get("cleanupAttempts", 0)),
        failure_class=(
            CheckoutFailureClass(data["failureClass"])
            if data.get("failureClass")
            else None
        ),
        failure_code=data.get("failureCode"),
        failure_message=data.get("failureMessage"),
    )


def _safe_branch(value: str) -> bool:
    if not value or value.startswith(("-", ".", "/")) or value.endswith((".", "/")):
        return False
    if any(part in {"", ".", ".."} or part.endswith(".lock") for part in value.split("/")):
        return False
    return not any(item in value for item in ("..", "@{", "\\", " ", "~", "^", ":", "?", "*", "["))


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _git(repository: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ("git", "-C", str(repository), *arguments),
            capture_output=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise _recoverable(
            "git_unavailable", "Git checkout operation was unavailable"
        ) from error
    if result.returncode:
        raise CheckoutProvisionError(
            CheckoutFailureClass.RECOVERABLE,
            "git_operation_failed",
            "Git checkout operation failed",
        )
    return result.stdout.decode("utf-8", errors="replace")


def _configuration(code: str, message: str) -> CheckoutProvisionError:
    return CheckoutProvisionError(CheckoutFailureClass.CONFIGURATION, code, message)


def _recoverable(code: str, message: str) -> CheckoutProvisionError:
    return CheckoutProvisionError(CheckoutFailureClass.RECOVERABLE, code, message)


def _persisted_failure_message(error: CheckoutProvisionError) -> str:
    if error.classification is CheckoutFailureClass.CONFIGURATION:
        return "execution checkout configuration failure"
    return "recoverable execution checkout operation failure"


_SOURCE_FAILURES: Mapping[
    str, tuple[CheckoutFailureClass, str]
] = {
    "unsafe_source_cache": (
        CheckoutFailureClass.CONFIGURATION,
        "repository source cache path is invalid",
    ),
    "invalid_source_cache": (
        CheckoutFailureClass.CONFIGURATION,
        "repository source cache is not a trusted bare repository",
    ),
    "source_identity_mismatch": (
        CheckoutFailureClass.CONFIGURATION,
        "repository source identity does not match configuration",
    ),
    "missing_commit": (
        CheckoutFailureClass.CONFIGURATION,
        "recorded revision is unavailable from the repository source",
    ),
    "source_revision_invalid": (
        CheckoutFailureClass.RECOVERABLE,
        "repository source returned invalid revision evidence",
    ),
    "source_unavailable": (
        CheckoutFailureClass.RECOVERABLE,
        "repository source is unavailable",
    ),
    "source_operation_failed": (
        CheckoutFailureClass.RECOVERABLE,
        "repository source operation failed",
    ),
}


def _source_configuration(code: str) -> RepositorySourceFailure:
    classification, message = _SOURCE_FAILURES[code]
    return RepositorySourceFailure(classification, code, message)


def _source_recoverable(code: str) -> RepositorySourceFailure:
    classification, message = _SOURCE_FAILURES[code]
    return RepositorySourceFailure(classification, code, message)


def _normalize_source_failure(error: Exception) -> CheckoutProvisionError:
    if isinstance(error, RepositorySourceFailure) and error.code in _SOURCE_FAILURES:
        classification, message = _SOURCE_FAILURES[error.code]
        return CheckoutProvisionError(classification, error.code, message)
    return CheckoutProvisionError(
        CheckoutFailureClass.RECOVERABLE,
        "source_boundary_failure",
        "repository source boundary failed",
    )
