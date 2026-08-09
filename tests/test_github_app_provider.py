from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
from threading import Lock
import time
from typing import Any

import pytest

from aep.execution_checkout import RepositoryIdentity
from aep.git_tool import GitSandboxCommandResult, GitToolAdapter, InMemoryGitCommandLogStore
from aep.github_app_provider import (
    GitHubAppAuthorizationError,
    GitHubAppAmbiguousMutationError,
    GitHubAppClient,
    GitHubAppConfig,
    GitHubAppGitCredentialProvider,
    GitHubAppTokenProvider,
    GitHubAppValidationError,
    HttpResponse,
    HttpTransportTimeout,
    github_app_provider_from_environment,
)
from aep.github_tool import GitHubToolAdapter
from aep.tool_runtime import ToolCaller, ToolRequest, ToolResultStatus, invoke_tool


class StaticSecret:
    def __init__(self, value: bytes = b"private-key-sentinel") -> None:
        self.value = value

    def read(self) -> bytes:
        return self.value


class FakeSigner:
    def sign(self, message: bytes, private_key: bytes) -> bytes:
        assert private_key == b"private-key-sentinel"
        assert message.count(b".") == 1
        return b"signed"


class ScriptedTransport:
    def __init__(self, outcomes: Sequence[HttpResponse | Exception], *, after_request=None) -> None:
        self._outcomes = deque(outcomes)
        self.requests: list[dict[str, Any]] = []
        self._lock = Lock()
        self._after_request = after_request or (lambda: None)

    def request(self, **kwargs) -> HttpResponse:
        with self._lock:
            self.requests.append(dict(kwargs))
            if not self._outcomes:
                raise AssertionError("unexpected HTTP request")
            outcome = self._outcomes.popleft()
            self._after_request()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def response(status: int, value: Any, *, request_id: str = "request-1", headers: Mapping[str, str] | None = None) -> HttpResponse:
    return HttpResponse(
        status,
        {"X-GitHub-Request-Id": request_id, **dict(headers or {})},
        json.dumps(value).encode(),
    )


def config() -> GitHubAppConfig:
    return GitHubAppConfig(
        app_id=42,
        owner="acme",
        repository="widgets",
        base_branch="main",
        authorized_branch_prefix="aep/execution/",
    )


def token_response(value: str = "ghs_installation_secret", expires: str = "2030-01-01T00:00:00Z") -> HttpResponse:
    return response(201, {"token": value, "expires_at": expires})


def tokens(transport: ScriptedTransport, *, clock=None, monotonic_clock=None) -> GitHubAppTokenProvider:
    return GitHubAppTokenProvider(
        config(), private_key=StaticSecret(), transport=transport,
        signer=FakeSigner(), clock=clock or (lambda: datetime(2029, 1, 1, tzinfo=UTC)),
        monotonic_clock=monotonic_clock or time.monotonic,
    )


def test_token_cache_resolves_installation_once_and_is_concurrency_safe() -> None:
    transport = ScriptedTransport([
        response(200, {"id": 137}), token_response(), response(200, {"id": 137}),
    ])
    provider = tokens(transport)

    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(lambda _: provider.token(), range(16)))

    assert values == ["ghs_installation_secret"] * 16
    assert len(transport.requests) == 2
    assert transport.requests[0]["url"].endswith("/repos/acme/widgets/installation")
    assert transport.requests[1]["url"].endswith("/app/installations/137/access_tokens")
    serialized = repr(transport.requests)
    assert "private-key-sentinel" not in serialized
    assert "ghs_installation_secret" not in str(provider.readiness())


def test_readiness_revalidates_and_adopts_a_reinstalled_identity() -> None:
    transport = ScriptedTransport([
        response(200, {"id": 137}), response(200, {"id": 204}),
    ])
    provider = tokens(transport)
    assert provider.readiness()["installationId"] == 137
    assert provider.readiness()["installationId"] == 204
    assert len(transport.requests) == 2


def test_failed_token_refresh_invalidates_installation_for_reresolution() -> None:
    now = [datetime(2029, 1, 1, tzinfo=UTC)]
    transport = ScriptedTransport([
        response(200, {"id": 137}),
        token_response("ghs_first", "2029-01-01T00:10:00Z"),
        response(404, {"message": "installation missing"}),
        response(200, {"id": 204}),
        token_response("ghs_second", "2029-01-01T01:00:00Z"),
    ])
    provider = tokens(transport, clock=lambda: now[0])
    assert provider.token() == "ghs_first"
    now[0] += timedelta(minutes=6)
    with pytest.raises(GitHubAppValidationError):
        provider.token()
    assert provider.token() == "ghs_second"
    installation_requests = [
        item for item in transport.requests if item["url"].endswith("/installation")
    ]
    assert len(installation_requests) == 2


def test_token_is_renewed_before_expiry_and_rotated_secret_is_read_again() -> None:
    now = [datetime(2029, 1, 1, tzinfo=UTC)]
    transport = ScriptedTransport([
        response(200, {"id": 137}),
        token_response("ghs_first", "2029-01-01T00:10:00Z"),
        token_response("ghs_second", "2029-01-01T01:00:00Z"),
    ])
    provider = tokens(transport, clock=lambda: now[0])
    assert provider.token() == "ghs_first"
    now[0] += timedelta(minutes=6)
    assert provider.token() == "ghs_second"
    assert len(transport.requests) == 3


def test_issue_read_uses_existing_tool_client_interface() -> None:
    transport = ScriptedTransport([
        response(200, {"id": 137}), token_response(),
        response(200, {
            "number": 7, "title": "Fix widget", "body": "Details", "state": "open",
            "html_url": "https://github.com/acme/widgets/issues/7",
            "user": {"login": "octocat"}, "labels": [{"name": "bug"}],
        }, request_id="issue-request"),
    ])
    client = GitHubAppClient(config(), tokens=tokens(transport), transport=transport)
    adapter = GitHubToolAdapter(client, max_read_attempts=1)
    request = ToolRequest(
        tool_ref={"kind": "Tool", "name": "github", "version": "1.0.0"},
        input={"operation": "readIssue", "repository": "acme/widgets", "issueNumber": 7},
        caller=ToolCaller(kind="TaskExecution", id="task-read"),
        capabilities=("github.issue.read",), timeout_ms=1000,
        correlation={"traceId": "trace-read", "workflowExecutionId": "workflow-read", "taskExecutionId": "task-read"},
    )
    result = invoke_tool(request, validator=type("Validator", (), {"validate_input": lambda self, value: None, "validate_output": lambda self, value: None})(), authorize=lambda _: True, adapter=adapter)
    assert result.status is ToolResultStatus.SUCCEEDED
    assert result.output["issue"]["number"] == 7
    assert result.output["metadata"]["providerRequestId"] == "issue-request"


def test_pr_creation_reconciles_existing_duplicate_without_posting() -> None:
    transport = ScriptedTransport([
        response(200, {"id": 137}), token_response(),
        response(200, [{"number": 9, "html_url": "https://github.com/acme/widgets/pull/9"}], request_id="reconcile-request"),
    ])
    client = GitHubAppClient(config(), tokens=tokens(transport), transport=transport)
    operation = client.start_create_pull_request(
        "acme/widgets", head="aep/execution/abc", base="main", title="Change", body="Body",
    )
    assert operation.wait(1000) == {
        "number": 9, "url": "https://github.com/acme/widgets/pull/9",
        "head": "aep/execution/abc", "base": "main", "requestId": "reconcile-request",
    }
    assert [item["method"] for item in transport.requests] == ["GET", "POST", "GET"]


def test_pr_creation_posts_once_after_empty_reconciliation() -> None:
    transport = ScriptedTransport([
        response(200, {"id": 137}), token_response(), response(200, []),
        response(201, {"number": 10, "html_url": "https://github.com/acme/widgets/pull/10"}, request_id="create-request"),
    ])
    client = GitHubAppClient(config(), tokens=tokens(transport), transport=transport)
    operation = client.start_create_pull_request(
        "acme/widgets", head="aep/execution/abc", base="main", title="Change", body="Body",
    )
    result = operation.wait(1000)
    assert result is not None and not isinstance(result, Exception)
    assert result["number"] == 10
    assert result["requestId"] == "create-request"
    assert [item["method"] for item in transport.requests].count("POST") == 2


def test_retryable_pr_server_response_is_an_explicit_unknown_mutation() -> None:
    transport = ScriptedTransport([
        response(200, {"id": 137}), token_response(), response(200, []),
        response(503, {"message": "provider detail"}, request_id="ambiguous-request"),
    ])
    client = GitHubAppClient(config(), tokens=tokens(transport), transport=transport)
    error = client.start_create_pull_request(
        "acme/widgets", head="aep/execution/abc", base="main", title="Change", body="Body",
    ).wait(1000)
    assert isinstance(error, GitHubAppAmbiguousMutationError)
    assert error.classification == "AMBIGUOUS_MUTATION"
    assert error.retryable is False
    assert error.request_id == "ambiguous-request"

    evidence_transport = ScriptedTransport([
        response(200, {"id": 137}), token_response(), response(200, []),
        response(503, {"message": "provider detail"}, request_id="ambiguous-evidence"),
    ])
    adapter = GitHubToolAdapter(
        GitHubAppClient(config(), tokens=tokens(evidence_transport), transport=evidence_transport)
    )
    request = ToolRequest(
        tool_ref={"kind": "Tool", "name": "github", "version": "1.0.0"},
        input={"operation": "createPullRequest", "repository": "acme/widgets", "head": "aep/execution/abc", "base": "main", "title": "Change", "body": "Body"},
        caller=ToolCaller(kind="TaskExecution", id="task-pr"), capabilities=("github.create_pr",), timeout_ms=1000,
        correlation={"traceId": "trace-pr", "workflowExecutionId": "workflow-pr", "taskExecutionId": "task-pr"},
    )
    validator = type("Validator", (), {"validate_input": lambda self, value: None, "validate_output": lambda self, value: None})()
    result = invoke_tool(request, validator=validator, authorize=lambda _: True, adapter=adapter)
    assert result.output["failure"]["category"] == "AMBIGUOUS_MUTATION"
    assert result.output["failure"]["mutationState"] == "UNKNOWN"
    assert result.output["failure"]["retryable"] is False


def test_malformed_successful_pr_response_is_an_explicit_unknown_mutation() -> None:
    transport = ScriptedTransport([
        response(200, {"id": 137}), token_response(), response(200, []),
        response(201, {"number": "invalid", "html_url": None}, request_id="malformed-success"),
    ])
    adapter = GitHubToolAdapter(
        GitHubAppClient(config(), tokens=tokens(transport), transport=transport)
    )
    request = ToolRequest(
        tool_ref={"kind": "Tool", "name": "github", "version": "1.0.0"},
        input={"operation": "createPullRequest", "repository": "acme/widgets", "head": "aep/execution/abc", "base": "main", "title": "Change", "body": "Body"},
        caller=ToolCaller(kind="TaskExecution", id="task-malformed-pr"), capabilities=("github.create_pr",), timeout_ms=1000,
        correlation={"traceId": "trace-malformed-pr", "workflowExecutionId": "workflow-malformed-pr", "taskExecutionId": "task-malformed-pr"},
    )
    validator = type("Validator", (), {"validate_input": lambda self, value: None, "validate_output": lambda self, value: None})()
    result = invoke_tool(request, validator=validator, authorize=lambda _: True, adapter=adapter)

    assert result.status is ToolResultStatus.FAILED
    assert result.output["failure"]["category"] == "AMBIGUOUS_MUTATION"
    assert result.output["failure"]["mutationState"] == "UNKNOWN"
    assert result.output["failure"]["retryable"] is False
    assert result.output["failure"]["providerRequestId"] == "malformed-success"


@pytest.mark.parametrize(
    ("repository", "head", "base"),
    [("other/widgets", "aep/execution/abc", "main"), ("acme/widgets", "feature/unbound", "main"), ("acme/widgets", "aep/execution/abc", "develop")],
)
def test_mutation_binding_mismatch_is_non_retryable(repository: str, head: str, base: str) -> None:
    transport = ScriptedTransport([])
    client = GitHubAppClient(config(), tokens=tokens(transport), transport=transport)
    with pytest.raises((GitHubAppAuthorizationError, GitHubAppValidationError)):
        client.start_create_pull_request(repository, head=head, base=base, title="Change", body="Body")
    assert transport.requests == []


def test_rate_limit_is_safe_retryable_and_bounded() -> None:
    transport = ScriptedTransport([
        response(200, {"id": 137}), token_response(),
        response(403, {"message": "secret provider message ghs_hidden"}, headers={"X-RateLimit-Remaining": "0", "Retry-After": "2"}),
    ])
    client = GitHubAppClient(config(), tokens=tokens(transport), transport=transport)
    error = client.start_read_issue("acme/widgets", 1).wait(1000)
    assert isinstance(error, Exception)
    assert getattr(error, "retryable") is True
    assert getattr(error, "retry_after_ms") == 2000
    assert "ghs_hidden" not in str(error)


def test_permission_denial_maps_to_non_retryable_tool_evidence() -> None:
    transport = ScriptedTransport([
        response(200, {"id": 137}), token_response(),
        response(403, {"message": "provider detail"}, request_id="denied-request"),
    ])
    adapter = GitHubToolAdapter(
        GitHubAppClient(config(), tokens=tokens(transport), transport=transport),
        max_read_attempts=1,
    )
    request = ToolRequest(
        tool_ref={"kind": "Tool", "name": "github", "version": "1.0.0"},
        input={"operation": "readIssue", "repository": "acme/widgets", "issueNumber": 7},
        caller=ToolCaller(kind="TaskExecution", id="task-denied"),
        capabilities=("github.issue.read",), timeout_ms=1000,
        correlation={"traceId": "trace-denied", "workflowExecutionId": "workflow-denied", "taskExecutionId": "task-denied"},
    )
    validator = type("Validator", (), {"validate_input": lambda self, value: None, "validate_output": lambda self, value: None})()
    result = invoke_tool(request, validator=validator, authorize=lambda _: True, adapter=adapter)
    assert result.status is ToolResultStatus.FAILED
    assert result.output["failure"]["category"] == "AUTHORIZATION"
    assert result.output["failure"]["mutationState"] == "NOT_ATTEMPTED"
    assert result.output["failure"]["retryable"] is False


@pytest.mark.parametrize(
    "outcomes",
    [
        [HttpTransportTimeout("installation timeout ghs_secret")],
        [response(200, {"id": 137}), HttpTransportTimeout("token timeout ghs_secret")],
    ],
    ids=["installation-lookup", "token-request"],
)
def test_authentication_transport_timeout_maps_to_tool_timeout(outcomes) -> None:
    transport = ScriptedTransport(outcomes)
    adapter = GitHubToolAdapter(
        GitHubAppClient(config(), tokens=tokens(transport), transport=transport),
        max_read_attempts=1,
    )
    request = ToolRequest(
        tool_ref={"kind": "Tool", "name": "github", "version": "1.0.0"},
        input={"operation": "readIssue", "repository": "acme/widgets", "issueNumber": 7},
        caller=ToolCaller(kind="TaskExecution", id="task-auth-timeout"), capabilities=("github.issue.read",), timeout_ms=1000,
        correlation={"traceId": "trace-auth-timeout", "workflowExecutionId": "workflow-auth-timeout", "taskExecutionId": "task-auth-timeout"},
    )
    validator = type("Validator", (), {"validate_input": lambda self, value: None, "validate_output": lambda self, value: None})()
    result = invoke_tool(request, validator=validator, authorize=lambda _: True, adapter=adapter)

    assert result.status is ToolResultStatus.TIMED_OUT
    assert result.output["failure"]["category"] == "TIMEOUT"
    assert result.output["failure"]["mutationState"] == "NOT_ATTEMPTED"
    assert result.output["failure"]["retryable"] is True
    assert "ghs_secret" not in (result.failure_message or "")


def test_ambiguous_pr_transport_timeout_is_not_replayed() -> None:
    transport = ScriptedTransport([
        response(200, {"id": 137}), token_response(), response(200, []),
        HttpTransportTimeout("contains ghs_secret"),
    ])
    client = GitHubAppClient(config(), tokens=tokens(transport), transport=transport)
    operation = client.start_create_pull_request(
        "acme/widgets", head="aep/execution/abc", base="main", title="Change", body="Body",
    )
    assert operation.wait(1000) is None
    assert operation.mutation_started is True
    assert operation.wait(1000) is None
    assert [item["method"] for item in transport.requests].count("POST") == 2  # token plus one PR


def test_one_deadline_bounds_cold_pr_sequence_before_mutation() -> None:
    now = [0.0]
    transport = ScriptedTransport(
        [response(200, {"id": 137}), token_response(), response(200, [])],
        after_request=lambda: now.__setitem__(0, now[0] + 0.4),
    )
    provider = tokens(transport, monotonic_clock=lambda: now[0])
    client = GitHubAppClient(
        config(), tokens=provider, transport=transport, clock=lambda: now[0]
    )
    adapter = GitHubToolAdapter(client)
    request = ToolRequest(
        tool_ref={"kind": "Tool", "name": "github", "version": "1.0.0"},
        input={"operation": "createPullRequest", "repository": "acme/widgets", "head": "aep/execution/abc", "base": "main", "title": "Change", "body": "Body"},
        caller=ToolCaller(kind="TaskExecution", id="task-deadline"), capabilities=("github.create_pr",), timeout_ms=1000,
        correlation={"traceId": "trace-deadline", "workflowExecutionId": "workflow-deadline", "taskExecutionId": "task-deadline"},
    )
    validator = type("Validator", (), {"validate_input": lambda self, value: None, "validate_output": lambda self, value: None})()
    result = invoke_tool(request, validator=validator, authorize=lambda _: True, adapter=adapter)

    assert result.status is ToolResultStatus.TIMED_OUT
    assert result.output["failure"]["ambiguousPublication"] is False
    assert result.output["failure"]["mutationState"] == "NOT_ATTEMPTED"
    assert result.output["failure"]["retryable"] is True
    assert [item["method"] for item in transport.requests] == ["GET", "POST", "GET"]
    budgets = [item["timeout_ms"] for item in transport.requests]
    assert budgets[0] > budgets[1] > budgets[2] > 0


class LocalGitSandbox:
    disabled_hooks_path = os.devnull
    null_device_path = os.devnull

    def run(self, *, repository: Path, arguments: Sequence[str], environment: Mapping[str, str], timeout_ms: int, stdin: bytes | None = None) -> GitSandboxCommandResult:
        process_environment = {**os.environ, **environment}
        completed = subprocess.run(("git", *arguments), cwd=repository, env=process_environment, input=stdin, capture_output=True, timeout=timeout_ms / 1000, check=False)
        return GitSandboxCommandResult(completed.returncode, completed.stdout, completed.stderr)


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(("git", *arguments), cwd=root, capture_output=True, check=True)
    return completed.stdout.decode().strip()


def test_git_credential_lease_pushes_authorized_branch_and_discards_credentials(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    subprocess.run(("git", "init", "--bare", str(remote)), capture_output=True, check=True)
    repository = tmp_path / "work"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.name", "AEP Test")
    _git(repository, "config", "user.email", "aep@example.test")
    _git(repository, "remote", "add", "origin", str(remote))
    (repository / "file.txt").write_text("one\n", encoding="utf-8")
    _git(repository, "add", "file.txt")
    _git(repository, "commit", "-m", "fixture")
    revision = _git(repository, "rev-parse", "HEAD")
    branch = "aep/execution/abc"
    _git(repository, "switch", "-c", branch)
    transport = ScriptedTransport([response(200, {"id": 137}), token_response()])
    credentials = GitHubAppGitCredentialProvider(config(), tokens=tokens(transport), lease_root=tmp_path / "leases")
    adapter = GitToolAdapter(
        repository=repository, repository_id="acme/widgets", expected_revision=revision,
        working_branch=branch, log_store=InMemoryGitCommandLogStore(), sandbox=LocalGitSandbox(),
        credential_provider=credentials,
    )
    request = ToolRequest(
        tool_ref={"kind": "Tool", "name": "git", "version": "1.0.0"},
        input={"operation": "push_branch", "expectedRevision": revision, "branch": branch},
        caller=ToolCaller(kind="TaskExecution", id="task-push"), capabilities=("git.push",), timeout_ms=5000,
        correlation={"traceId": "trace-push", "workflowExecutionId": "workflow-push", "taskExecutionId": "task-push"},
    )
    result = adapter.start(request).wait(5000)
    assert result is not None and result.status is ToolResultStatus.SUCCEEDED
    assert result.output["remoteMutationState"] == "CONFIRMED"
    assert _git(remote, "rev-parse", f"refs/heads/{branch}") == revision
    assert list((tmp_path / "leases").iterdir()) == []
    persisted = repr({"input": request.input, "output": result.output, "failure": result.failure_message})
    assert "ghs_installation_secret" not in persisted


def test_source_credential_binding_rejects_another_repository(tmp_path: Path) -> None:
    transport = ScriptedTransport([])
    credentials = GitHubAppGitCredentialProvider(config(), tokens=tokens(transport), lease_root=tmp_path)
    with pytest.raises(GitHubAppAuthorizationError):
        credentials.acquire(repository=RepositoryIdentity("github", "other", "widgets"))
    assert transport.requests == []


def test_environment_factory_keeps_private_key_file_out_of_readiness(tmp_path: Path) -> None:
    key_file = tmp_path / "app.pem"
    key_file.write_bytes(b"private-key-sentinel")
    transport = ScriptedTransport([response(200, {"id": 137})])
    bundle = github_app_provider_from_environment(
        {
            "AEP_GITHUB_APP_ID": "42",
            "AEP_GITHUB_APP_PRIVATE_KEY_FILE": str(key_file),
            "AEP_REPOSITORY_OWNER": "acme",
            "AEP_REPOSITORY_NAME": "widgets",
            "AEP_REPOSITORY_DEFAULT_BRANCH": "main",
            "AEP_STATE_ROOT": str(tmp_path / "state"),
        },
        transport=transport,
        signer=FakeSigner(),
        clock=lambda: datetime(2029, 1, 1, tzinfo=UTC),
    )
    readiness = bundle.readiness()
    assert readiness["repository"] == "acme/widgets"
    assert readiness["installationId"] == 137
    assert str(key_file) not in repr(readiness)
    assert "private-key-sentinel" not in repr(readiness)
