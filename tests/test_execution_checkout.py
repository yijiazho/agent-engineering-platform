from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import os
from pathlib import Path
import subprocess
from threading import Event, Lock, Thread
import time
import traceback

import pytest

from aep.execution_checkout import (
    CheckoutClaimInProgress,
    CheckoutFailureClass,
    CheckoutProvisionError,
    CheckoutRequest,
    CheckoutState,
    ExecutionCheckout,
    ExecutionCheckoutManager,
    GitRepositorySource,
    InMemoryCheckoutRegistry,
    RepositoryIdentity,
    SqliteCheckoutRegistry,
    local_checkout_registry,
)


REPOSITORY = RepositoryIdentity("github", "acme", "widgets")


def git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(repository), *arguments),
        capture_output=True,
        check=True,
    )
    return result.stdout.decode().strip()


@pytest.fixture
def remote_repository(tmp_path: Path) -> tuple[Path, str]:
    source = tmp_path / "upstream"
    source.mkdir()
    git(source, "init", "-b", "main")
    git(source, "config", "user.name", "AEP Test")
    git(source, "config", "user.email", "aep@example.test")
    (source / "README.md").write_text("revision bound\n", encoding="utf-8")
    git(source, "add", "README.md")
    git(source, "commit", "-m", "Initialize fixture")
    return source, git(source, "rev-parse", "HEAD")


def request(revision: str, *, execution_id: str = "workflow-execution/123") -> CheckoutRequest:
    return CheckoutRequest(
        execution_id=execution_id,
        repository=REPOSITORY,
        default_branch="main",
        base_revision=revision,
        knowledge_revision=revision,
    )


def manager(tmp_path: Path, remote: Path, **kwargs) -> ExecutionCheckoutManager:
    return ExecutionCheckoutManager(
        source=GitRepositorySource(str(remote)),
        source_cache_root=tmp_path / "cache",
        worktree_root=tmp_path / "worktrees",
        registry=kwargs.pop("registry", InMemoryCheckoutRegistry()),
        **kwargs,
    )


def test_provisions_clean_exact_revision_and_common_boundary_metadata(
    tmp_path: Path, remote_repository: tuple[Path, str]
) -> None:
    remote, revision = remote_repository

    checkout = manager(tmp_path, remote).provision(request(revision))

    assert checkout.state is CheckoutState.READY
    assert git(checkout.workspace_path, "rev-parse", "HEAD") == revision
    assert git(checkout.workspace_path, "status", "--porcelain") == ""
    assert git(checkout.workspace_path, "branch", "--show-current") == checkout.branch
    assert checkout.branch.startswith("aep/execution/")
    assert checkout.boundary_metadata() == {
        "executionId": "workflow-execution/123",
        "repository": "github:acme/widgets",
        "repositoryRevision": revision,
        "knowledgeRevision": revision,
        "branch": checkout.branch,
        "workspacePath": str(checkout.workspace_path),
    }


def test_retry_returns_same_checkout_even_after_execution_changes(
    tmp_path: Path, remote_repository: tuple[Path, str]
) -> None:
    remote, revision = remote_repository
    checkout_manager = manager(tmp_path, remote)
    first = checkout_manager.provision(request(revision))
    (first.workspace_path / "change.txt").write_text("work in progress", encoding="utf-8")

    second = checkout_manager.provision(request(revision))

    assert second == first
    assert (second.workspace_path / "change.txt").is_file()


def test_execution_id_cannot_be_reused_for_another_revision(
    tmp_path: Path, remote_repository: tuple[Path, str]
) -> None:
    remote, revision = remote_repository
    checkout_manager = manager(tmp_path, remote)
    checkout_manager.provision(request(revision))

    with pytest.raises(CheckoutProvisionError) as raised:
        checkout_manager.provision(replace(request(revision), base_revision="a" * 40, knowledge_revision="a" * 40))

    assert raised.value.classification is CheckoutFailureClass.CONFIGURATION
    assert raised.value.code == "execution_identity_conflict"


def test_different_executions_never_share_path_or_branch(
    tmp_path: Path, remote_repository: tuple[Path, str]
) -> None:
    remote, revision = remote_repository
    checkout_manager = manager(tmp_path, remote)

    left = checkout_manager.provision(request(revision, execution_id="one"))
    right = checkout_manager.provision(request(revision, execution_id="two"))

    assert left.workspace_path != right.workspace_path
    assert left.branch != right.branch
    (left.workspace_path / "private.txt").write_text("one", encoding="utf-8")
    assert not (right.workspace_path / "private.txt").exists()


class BlockingSource:
    def __init__(self, delegate: GitRepositorySource, entered: Event, release: Event) -> None:
        self.delegate = delegate
        self.entered = entered
        self.release = release

    def materialize(self, **kwargs):
        self.entered.set()
        assert self.release.wait(timeout=5)
        return self.delegate.materialize(**kwargs)


def test_concurrent_claim_has_only_one_owner(
    tmp_path: Path, remote_repository: tuple[Path, str]
) -> None:
    remote, revision = remote_repository
    registry = InMemoryCheckoutRegistry()
    entered = Event()
    release = Event()
    checkout_manager = ExecutionCheckoutManager(
        source=BlockingSource(GitRepositorySource(str(remote)), entered, release),
        source_cache_root=tmp_path / "cache",
        worktree_root=tmp_path / "worktrees",
        registry=registry,
    )
    outcomes: list[object] = []
    lock = Lock()

    def run() -> None:
        try:
            value: object = checkout_manager.provision(request(revision))
        except Exception as error:
            value = error
        with lock:
            outcomes.append(value)

    first = Thread(target=run)
    first.start()
    assert entered.wait(timeout=5)
    second = Thread(target=run)
    second.start()
    second.join(timeout=10)
    release.set()
    first.join(timeout=10)

    assert sum(isinstance(value, ExecutionCheckout) for value in outcomes) == 1
    assert sum(isinstance(value, CheckoutClaimInProgress) for value in outcomes) == 1


def test_revision_drift_is_configuration_failure(
    tmp_path: Path, remote_repository: tuple[Path, str]
) -> None:
    remote, revision = remote_repository
    (remote / "second.txt").write_text("second", encoding="utf-8")
    git(remote, "add", "second.txt")
    git(remote, "commit", "-m", "Advance default branch")

    with pytest.raises(CheckoutProvisionError) as raised:
        manager(tmp_path, remote).provision(request(revision))

    assert raised.value.classification is CheckoutFailureClass.CONFIGURATION
    assert raised.value.code == "revision_drift"


def test_missing_recorded_commit_is_configuration_failure(
    tmp_path: Path, remote_repository: tuple[Path, str]
) -> None:
    remote, _revision = remote_repository

    with pytest.raises(CheckoutProvisionError) as raised:
        manager(tmp_path, remote).provision(request("a" * 40))

    assert raised.value.classification is CheckoutFailureClass.CONFIGURATION
    assert raised.value.code == "missing_commit"


def test_repository_source_uses_only_scoped_git_and_credential_environment(
    tmp_path: Path,
    remote_repository: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote, revision = remote_repository
    source = GitRepositorySource(str(remote))
    original_run = subprocess.run
    environments: list[dict[str, str]] = []
    monkeypatch.setenv("HOME", "ambient-home-must-not-enter-git")
    monkeypatch.setenv("HTTPS_PROXY", "http://ambient-proxy.invalid")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "ambient-gitconfig")
    monkeypatch.setenv("AEP_AMBIENT_CREDENTIAL", "ambient-secret")

    def recording_run(*args, **kwargs):
        environments.append(dict(kwargs["env"]))
        return original_run(*args, **kwargs)

    monkeypatch.setattr("aep.execution_checkout.subprocess.run", recording_run)
    result = source.materialize(
        repository=REPOSITORY,
        default_branch="main",
        expected_revision=revision,
        cache_path=tmp_path / "cache" / "repository.git",
        credentials={
            "GIT_ASKPASS": str(tmp_path / "askpass"),
            "GIT_ASKPASS_REQUIRE": "force",
            "AEP_TEST_USERNAME": "synthetic-user",
            "AEP_TEST_PASSWORD": "synthetic-password",
        },
        mutation_lease=nullcontext,
    )

    assert result.revision == revision
    assert environments
    allowed = {
        "GIT_ASKPASS",
        "GIT_ASKPASS_REQUIRE",
        "AEP_TEST_USERNAME",
        "AEP_TEST_PASSWORD",
        "GIT_TERMINAL_PROMPT",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
    }
    if os.name == "nt":
        allowed.add("SYSTEMROOT")
    assert all(set(environment) == allowed for environment in environments)
    assert all(
        not {
            "PATH",
            "HOME",
            "HTTPS_PROXY",
            "GIT_CONFIG_GLOBAL",
            "AEP_AMBIENT_CREDENTIAL",
        }
        & set(environment)
        for environment in environments
    )


def test_repository_source_rejects_unscoped_credential_environment(
    tmp_path: Path, remote_repository: tuple[Path, str]
) -> None:
    remote, revision = remote_repository

    with pytest.raises(CheckoutProvisionError) as raised:
        GitRepositorySource(str(remote)).materialize(
            repository=REPOSITORY,
            default_branch="main",
            expected_revision=revision,
            cache_path=tmp_path / "cache" / "repository.git",
            credentials={"HTTPS_PROXY": "http://ambient-proxy.invalid"},
            mutation_lease=nullcontext,
        )

    assert raised.value.classification is CheckoutFailureClass.CONFIGURATION
    assert raised.value.code == "unsafe_source_credential_environment"


@pytest.mark.parametrize(
    "change,code",
    [
        ({"knowledge_revision": "b" * 40}, "knowledge_revision_mismatch"),
        ({"default_branch": "../escape"}, "unsafe_default_branch"),
        ({"base_revision": "main"}, "invalid_revision"),
    ],
)
def test_unsafe_or_inconsistent_requests_fail_configuration(change: dict[str, str], code: str) -> None:
    values = {
        "execution_id": "execution",
        "repository": REPOSITORY,
        "default_branch": "main",
        "base_revision": "a" * 40,
        "knowledge_revision": "a" * 40,
        **change,
    }
    with pytest.raises(CheckoutProvisionError) as raised:
        CheckoutRequest(**values)
    assert raised.value.classification is CheckoutFailureClass.CONFIGURATION
    assert raised.value.code == code


class SecretLease:
    def __init__(self) -> None:
        self.closed = False
        self.secret = "opaque credential !@#$%^&*() with spaces"

    @property
    def environment(self):
        return {"AEP_TEST_TOKEN": self.secret}

    def close(self) -> None:
        self.closed = True


class SecretProvider:
    def __init__(self, lease: SecretLease) -> None:
        self.lease = lease

    def acquire(self, *, repository):
        return self.lease


class FailingSecretProvider:
    def __init__(self, secret: str) -> None:
        self.secret = secret

    def acquire(self, *, repository):
        raise RuntimeError(f"credential provider leaked {self.secret}")


class FailingSource:
    def materialize(self, **kwargs):
        secret = kwargs["credentials"]["AEP_TEST_TOKEN"]
        raise RuntimeError(f"provider exposed {secret}")


def test_credential_failure_is_recoverable_redacted_and_lease_is_closed(tmp_path: Path) -> None:
    lease = SecretLease()
    registry = InMemoryCheckoutRegistry()
    checkout_manager = ExecutionCheckoutManager(
        source=FailingSource(),
        source_cache_root=tmp_path / "cache",
        worktree_root=tmp_path / "worktrees",
        registry=registry,
        credential_provider=SecretProvider(lease),
    )

    with pytest.raises(CheckoutProvisionError) as raised:
        checkout_manager.provision(request("a" * 40))

    assert raised.value.classification is CheckoutFailureClass.RECOVERABLE
    assert lease.secret not in str(raised.value)
    stored = registry.get("workflow-execution/123")
    assert stored is not None
    assert lease.secret not in (stored.failure_message or "")
    _assert_absent_from_exception(raised.value, lease.secret)
    assert lease.closed


def test_credential_provider_exception_has_no_unsafe_traceback_chain(
    tmp_path: Path,
) -> None:
    secret = "credential-chain-sentinel-opaque"
    checkout_manager = ExecutionCheckoutManager(
        source=FailingSource(),
        source_cache_root=tmp_path / "cache",
        worktree_root=tmp_path / "worktrees",
        registry=InMemoryCheckoutRegistry(),
        credential_provider=FailingSecretProvider(secret),
    )

    with pytest.raises(CheckoutProvisionError) as raised:
        checkout_manager.provision(request("a" * 40))

    assert raised.value.code == "credential_boundary_failure"
    _assert_absent_from_exception(raised.value, secret)


class ClassifiedSecretSource:
    def __init__(self, secret: str) -> None:
        self.secret = secret

    def materialize(self, **kwargs):
        raise CheckoutProvisionError(
            CheckoutFailureClass.CONFIGURATION,
            "adapter_supplied_code",
            f"classified source leaked {self.secret}",
        )


def test_adapter_classified_source_error_is_normalized_before_raise_and_persistence(
    tmp_path: Path,
) -> None:
    secret = "opaque classified secret ?x=one two three"
    registry = InMemoryCheckoutRegistry()
    checkout_manager = ExecutionCheckoutManager(
        source=ClassifiedSecretSource(secret),
        source_cache_root=tmp_path / "cache",
        worktree_root=tmp_path / "worktrees",
        registry=registry,
    )

    with pytest.raises(CheckoutProvisionError) as raised:
        checkout_manager.provision(request("a" * 40))

    assert raised.value.classification is CheckoutFailureClass.RECOVERABLE
    assert raised.value.code == "source_boundary_failure"
    assert str(raised.value) == "repository source boundary failed"
    stored = registry.get("workflow-execution/123")
    assert stored is not None
    assert secret not in (stored.failure_message or "")
    _assert_absent_from_exception(raised.value, secret)


def test_expired_interrupted_claim_is_recovered(
    tmp_path: Path, remote_repository: tuple[Path, str]
) -> None:
    remote, revision = remote_repository
    database = tmp_path / "state" / "checkouts.sqlite3"
    registry = SqliteCheckoutRegistry(database)
    now = datetime(2026, 8, 7, tzinfo=UTC)
    digest = sha256(f"{REPOSITORY.canonical}\0workflow-execution/123".encode()).hexdigest()
    interrupted = ExecutionCheckout(
        execution_id="workflow-execution/123",
        repository=REPOSITORY,
        base_revision=revision,
        knowledge_revision=revision,
        branch=f"aep/execution/{digest[:20]}",
        workspace_path=(tmp_path / "worktrees" / f"execution-{digest[:24]}").resolve(),
        source_cache_path=(tmp_path / "cache" / f"repository-{sha256(REPOSITORY.canonical.encode()).hexdigest()[:24]}").resolve(),
        state=CheckoutState.PROVISIONING,
        created_at=now - timedelta(minutes=5),
        updated_at=now - timedelta(minutes=5),
        owner_token="dead-worker",
        lease_expires_at=now - timedelta(minutes=1),
    )
    registry.claim(interrupted, now=now - timedelta(minutes=5))

    checkout = manager(
        tmp_path,
        remote,
        registry=SqliteCheckoutRegistry(database),
        clock=lambda: now,
    ).provision(request(revision))

    assert checkout.state is CheckoutState.READY
    assert git(checkout.workspace_path, "rev-parse", "HEAD") == revision


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value
        self.lock = Lock()

    def __call__(self) -> datetime:
        with self.lock:
            return self.value

    def advance(self, delta: timedelta) -> None:
        with self.lock:
            self.value += delta


class FirstCallPausedSource:
    def __init__(self, delegate: GitRepositorySource) -> None:
        self.delegate = delegate
        self.entered = Event()
        self.release = Event()
        self.calls = 0
        self.lock = Lock()

    def materialize(self, **kwargs):
        with self.lock:
            self.calls += 1
            call = self.calls
        if call == 1:
            self.entered.set()
            assert self.release.wait(timeout=10)
        return self.delegate.materialize(**kwargs)


class ActiveMutationPausedSource:
    def __init__(self, delegate: GitRepositorySource) -> None:
        self.delegate = delegate
        self.entered = Event()
        self.release = Event()

    def materialize(self, **kwargs):
        with kwargs["mutation_lease"]():
            self.entered.set()
            assert self.release.wait(timeout=10)
        return self.delegate.materialize(**kwargs)


def test_heartbeat_prevents_lease_expiry_takeover_during_active_mutation(
    tmp_path: Path, remote_repository: tuple[Path, str]
) -> None:
    remote, revision = remote_repository
    database = tmp_path / "state" / "checkouts.sqlite3"
    source = ActiveMutationPausedSource(GitRepositorySource(str(remote)))

    def new_manager() -> ExecutionCheckoutManager:
        return ExecutionCheckoutManager(
            source=source,
            source_cache_root=tmp_path / "cache",
            worktree_root=tmp_path / "worktrees",
            registry=SqliteCheckoutRegistry(database),
            claim_ttl=timedelta(milliseconds=1500),
        )

    outcome: list[object] = []

    def run_first() -> None:
        try:
            outcome.append(new_manager().provision(request(revision)))
        except Exception as error:
            outcome.append(error)

    thread = Thread(target=run_first)
    thread.start()
    assert source.entered.wait(timeout=5)
    time.sleep(2.2)

    with pytest.raises(CheckoutClaimInProgress):
        new_manager().provision(request(revision))

    source.release.set()
    thread.join(timeout=10)
    assert len(outcome) == 1
    assert isinstance(outcome[0], ExecutionCheckout)


def test_expired_live_owner_is_fenced_before_mutation_and_new_owner_takes_over(
    tmp_path: Path, remote_repository: tuple[Path, str]
) -> None:
    remote, revision = remote_repository
    database = tmp_path / "state" / "checkouts.sqlite3"
    source = FirstCallPausedSource(GitRepositorySource(str(remote)))
    clock = MutableClock(datetime(2026, 8, 7, tzinfo=UTC))
    old_manager = ExecutionCheckoutManager(
        source=source,
        source_cache_root=tmp_path / "cache",
        worktree_root=tmp_path / "worktrees",
        registry=SqliteCheckoutRegistry(database),
        claim_ttl=timedelta(seconds=5),
        clock=clock,
    )
    new_manager = ExecutionCheckoutManager(
        source=source,
        source_cache_root=tmp_path / "cache",
        worktree_root=tmp_path / "worktrees",
        registry=SqliteCheckoutRegistry(database),
        claim_ttl=timedelta(seconds=5),
        clock=clock,
    )
    old_outcome: list[object] = []

    def run_old() -> None:
        try:
            old_outcome.append(old_manager.provision(request(revision)))
        except Exception as error:
            old_outcome.append(error)

    thread = Thread(target=run_old)
    thread.start()
    assert source.entered.wait(timeout=5)
    clock.advance(timedelta(seconds=6))

    replacement = new_manager.provision(request(revision))
    source.release.set()
    thread.join(timeout=10)

    assert replacement.state is CheckoutState.READY
    assert replacement.fence_token == 2
    assert len(old_outcome) == 1
    assert isinstance(old_outcome[0], CheckoutProvisionError)
    assert old_outcome[0].code == "source_boundary_failure"
    assert git(replacement.workspace_path, "rev-parse", "HEAD") == revision


def test_repository_cache_claim_serializes_distinct_executions_and_existing_cache_retry(
    tmp_path: Path, remote_repository: tuple[Path, str]
) -> None:
    remote, revision = remote_repository
    database = tmp_path / "state" / "checkouts.sqlite3"
    source = FirstCallPausedSource(GitRepositorySource(str(remote)))

    def new_manager() -> ExecutionCheckoutManager:
        return ExecutionCheckoutManager(
            source=source,
            source_cache_root=tmp_path / "cache",
            worktree_root=tmp_path / "worktrees",
            registry=SqliteCheckoutRegistry(database),
        )

    first_outcome: list[object] = []

    def run_first() -> None:
        try:
            first_outcome.append(
                new_manager().provision(request(revision, execution_id="first"))
            )
        except Exception as error:
            first_outcome.append(error)

    thread = Thread(target=run_first)
    thread.start()
    assert source.entered.wait(timeout=5)

    with pytest.raises(CheckoutProvisionError) as raised:
        new_manager().provision(request(revision, execution_id="second"))
    assert raised.value.code == "cache_claim_in_progress"

    source.release.set()
    thread.join(timeout=10)
    assert isinstance(first_outcome[0], ExecutionCheckout)

    second = new_manager().provision(request(revision, execution_id="second"))
    assert second.state is CheckoutState.READY
    assert second.workspace_path != first_outcome[0].workspace_path


def test_sqlite_registry_survives_manager_restart_and_reuses_ready_checkout(
    tmp_path: Path, remote_repository: tuple[Path, str]
) -> None:
    remote, revision = remote_repository
    database = tmp_path / "state" / "checkouts.sqlite3"
    first_manager = ExecutionCheckoutManager(
        source=GitRepositorySource(str(remote)),
        source_cache_root=tmp_path / "cache",
        worktree_root=tmp_path / "worktrees",
        registry=SqliteCheckoutRegistry(database),
    )
    first = first_manager.provision(request(revision))

    restarted_manager = ExecutionCheckoutManager(
        source=GitRepositorySource(str(remote)),
        source_cache_root=tmp_path / "cache",
        worktree_root=tmp_path / "worktrees",
        registry=SqliteCheckoutRegistry(database),
    )
    second = restarted_manager.provision(request(revision))

    assert second == first
    assert local_checkout_registry(tmp_path / "local-state").database_path == (
        tmp_path / "local-state" / "checkout-manager" / "registry.sqlite3"
    ).resolve()


@pytest.mark.parametrize("collision", ["path", "branch"])
def test_sqlite_registry_enforces_cross_execution_path_and_branch_uniqueness(
    tmp_path: Path, collision: str
) -> None:
    registry = SqliteCheckoutRegistry(tmp_path / "state" / "registry.sqlite3")
    now = datetime(2026, 8, 7, tzinfo=UTC)

    def candidate(execution_id: str, path: str, branch: str) -> ExecutionCheckout:
        return ExecutionCheckout(
            execution_id=execution_id,
            repository=REPOSITORY,
            base_revision="a" * 40,
            knowledge_revision="a" * 40,
            branch=branch,
            workspace_path=(tmp_path / path).resolve(),
            source_cache_path=(tmp_path / "cache").resolve(),
            state=CheckoutState.PROVISIONING,
            created_at=now,
            updated_at=now,
            owner_token=execution_id,
            lease_expires_at=now + timedelta(minutes=1),
        )

    registry.claim(candidate("one", "one", "aep/execution/one"), now=now)
    second = candidate(
        "two",
        "one" if collision == "path" else "two",
        "aep/execution/one" if collision == "branch" else "aep/execution/two",
    )
    with pytest.raises(CheckoutProvisionError) as raised:
        registry.claim(second, now=now)
    assert raised.value.code == "checkout_reuse"


def test_ready_binding_supplies_all_production_boundaries(
    tmp_path: Path, remote_repository: tuple[Path, str]
) -> None:
    remote, revision = remote_repository
    checkout = manager(tmp_path, remote).provision(request(revision))
    binding = checkout.binding()

    assert binding.repository_knowledge_input == (checkout.workspace_path, revision)
    assert binding.filesystem_workspace == checkout.workspace_path
    assert binding.git_input == {
        "repository": checkout.workspace_path,
        "repository_id": REPOSITORY.canonical,
        "expected_revision": revision,
        "working_branch": checkout.branch,
    }
    assert binding.docker_workspace == checkout.workspace_path
    assert binding.publication_evidence["repositoryRevision"] == revision


def test_production_orchestration_constructs_consumers_from_one_binding(
    tmp_path: Path, remote_repository: tuple[Path, str]
) -> None:
    from aep.docker_validation_tool import DockerValidationAdapter
    from aep.filesystem_tool import FilesystemToolAdapter
    from aep.git_tool import GitToolAdapter, InMemoryGitCommandLogStore
    from aep.repository_knowledge import FileQuery, MvpRepositoryScanner

    remote, revision = remote_repository
    checkout, orchestration = manager(tmp_path, remote).provision_orchestration(
        request(revision)
    )
    snapshot, context_provider = orchestration.repository_context(
        MvpRepositoryScanner(), scanned_at="2026-08-07T00:00:00Z"
    )
    filesystem = orchestration.filesystem_adapter()

    class Sandbox:
        disabled_hooks_path = "/disabled-hooks"
        null_device_path = "/dev/null"

        def run(self, **kwargs):
            raise AssertionError("constructor integration must not execute Git")

    git_adapter = orchestration.git_adapter(
        log_store=InMemoryGitCommandLogStore(), sandbox=Sandbox()
    )
    docker = orchestration.docker_adapter(object())
    task_input = orchestration.task_execution_input({"kind": "TaskExecution"})
    publication = orchestration.publication_input({"status": "accepted"})

    assert snapshot.repository_revision == revision
    assert context_provider.lookup_file(FileQuery("README.md"))[0].provenance.repository_revision == revision
    assert isinstance(filesystem, FilesystemToolAdapter)
    assert isinstance(git_adapter, GitToolAdapter)
    assert isinstance(docker, DockerValidationAdapter)
    assert task_input["workspacePath"] == str(checkout.workspace_path)
    assert task_input["repositoryRevision"] == revision
    assert task_input["workingBranch"] == checkout.branch
    assert publication["repository"] == REPOSITORY.canonical
    assert publication["branch"] == checkout.branch
    assert publication["workingBranch"] == checkout.branch


@pytest.mark.parametrize(
    "method,value",
    [
        ("task_execution_input", {"workspacePath": "C:/another/worktree"}),
        ("task_execution_input", {"repositoryRevision": "b" * 40}),
        ("publication_input", {"repository": "github:other/repository"}),
        ("publication_input", {"branch": "unbound-branch"}),
        ("task_execution_input", {"workingBranch": "unbound-branch"}),
    ],
)
def test_production_orchestration_rejects_independently_supplied_mismatches(
    tmp_path: Path,
    remote_repository: tuple[Path, str],
    method: str,
    value: dict[str, str],
) -> None:
    remote, revision = remote_repository
    _checkout, orchestration = manager(
        tmp_path, remote
    ).provision_orchestration(request(revision))

    with pytest.raises(CheckoutProvisionError) as raised:
        getattr(orchestration, method)(value)
    assert raised.value.code == "boundary_identity_mismatch"


def test_cleanup_requires_durable_evidence_and_retains_dirty_checkout(
    tmp_path: Path, remote_repository: tuple[Path, str]
) -> None:
    remote, revision = remote_repository
    checkout_manager = manager(tmp_path, remote)
    checkout = checkout_manager.provision(request(revision))

    with pytest.raises(CheckoutProvisionError) as raised:
        checkout_manager.cleanup(checkout.execution_id, terminal_evidence_durable=False)
    assert raised.value.code == "terminal_evidence_not_durable"

    (checkout.workspace_path / "recovery.txt").write_text("retain me", encoding="utf-8")
    result = checkout_manager.cleanup(checkout.execution_id, terminal_evidence_durable=True)
    assert result.state is CheckoutState.CLEANUP_FAILED
    assert result.failure_code == "dirty_checkout"
    assert checkout.workspace_path.is_dir()

    (checkout.workspace_path / "recovery.txt").unlink()
    recovered = checkout_manager.cleanup(
        checkout.execution_id, terminal_evidence_durable=True
    )
    assert recovered.state is CheckoutState.CLEANED
    assert recovered.cleanup_attempts == 2


def test_clean_checkout_is_removed_idempotently_after_durable_evidence(
    tmp_path: Path, remote_repository: tuple[Path, str]
) -> None:
    remote, revision = remote_repository
    checkout_manager = manager(tmp_path, remote)
    checkout = checkout_manager.provision(request(revision))

    first = checkout_manager.cleanup(checkout.execution_id, terminal_evidence_durable=True)
    second = checkout_manager.cleanup(checkout.execution_id, terminal_evidence_durable=True)

    assert first.state is CheckoutState.CLEANED
    assert second == first
    assert not checkout.workspace_path.exists()
    assert subprocess.run(
        (
            "git",
            "-C",
            str(checkout.source_cache_path),
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/{checkout.branch}",
        ),
        check=False,
    ).returncode == 1


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@example.test/acme/widgets.git",
        "https://example.test/acme/widgets.git?token=opaque",
        "https://example.test/acme/widgets.git#credential",
    ],
)
def test_remote_url_cannot_persist_credentials_query_or_fragment(url: str) -> None:
    with pytest.raises(CheckoutProvisionError) as raised:
        GitRepositorySource(url)
    assert raised.value.code == "credential_in_remote_url"


def test_source_cache_rejects_a_non_bare_or_dirty_repository(
    tmp_path: Path, remote_repository: tuple[Path, str]
) -> None:
    remote, revision = remote_repository
    cache_name = sha256(REPOSITORY.canonical.encode()).hexdigest()[:24]
    cache = tmp_path / "cache" / f"repository-{cache_name}"
    cache.parent.mkdir()
    subprocess.run(("git", "clone", "--", str(remote), str(cache)), check=True, capture_output=True)
    (cache / "dirty.txt").write_text("not a trusted bare cache", encoding="utf-8")

    with pytest.raises(CheckoutProvisionError) as raised:
        manager(tmp_path, remote).provision(request(revision))

    assert raised.value.classification is CheckoutFailureClass.CONFIGURATION
    assert raised.value.code == "invalid_source_cache"


def test_storage_roots_must_not_overlap(tmp_path: Path, remote_repository: tuple[Path, str]) -> None:
    remote, _revision = remote_repository
    with pytest.raises(CheckoutProvisionError) as raised:
        ExecutionCheckoutManager(
            source=GitRepositorySource(str(remote)),
            source_cache_root=tmp_path / "storage",
            worktree_root=tmp_path / "storage" / "worktrees",
            registry=InMemoryCheckoutRegistry(),
        )
    assert raised.value.code == "overlapping_storage_roots"


def _assert_absent_from_exception(error: BaseException, sentinel: str) -> None:
    assert sentinel not in "".join(traceback.format_exception(error))
    assert error.__cause__ is None
    assert error.__context__ is None
    seen: set[int] = set()
    pending: list[BaseException] = [error]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        assert sentinel not in str(current)
        pending.extend(
            item
            for item in (current.__cause__, current.__context__)
            if item is not None
        )
