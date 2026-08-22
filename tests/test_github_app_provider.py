from __future__ import annotations

from base64 import b64encode
from collections import deque
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from threading import Lock, Thread
import time
from typing import Any
from urllib.parse import urlsplit

import pytest

from aep.execution_checkout import RepositoryIdentity
from aep.git_tool import (
    GitSandboxCommandResult,
    GitToolAdapter,
    InMemoryGitCommandLogStore,
    SubprocessGitSandbox,
)
from aep.github_app_provider import (
    GitHubAppAuthorizationError,
    GitHubAppAmbiguousMutationError,
    GitHubAppClient,
    GitHubAppConfig,
    GitHubAppConfigurationError,
    GitHubAppGitCredentialProvider,
    GitHubAppTokenProvider,
    GitHubAppValidationError,
    HttpResponse,
    HttpTransportTimeout,
    github_app_provider_from_environment,
)
from aep.github_tool import GitHubRateLimitError, GitHubToolAdapter
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


def test_default_branch_revision_is_resolved_through_bound_app() -> None:
    revision = "a" * 40
    transport = ScriptedTransport([
        response(200, {"id": 137}),
        token_response(),
        response(200, {"object": {"type": "commit", "sha": revision}}),
    ])
    client = GitHubAppClient(config(), tokens=tokens(transport), transport=transport)

    assert client.resolve_default_branch_revision() == revision
    assert transport.requests[-1]["url"].endswith(
        "/repos/acme/widgets/git/ref/heads/main"
    )


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


def test_secondary_rate_limit_uses_retry_after_without_remaining_header() -> None:
    transport = ScriptedTransport([
        response(200, {"id": 137}), token_response(),
        response(
            403,
            {"message": "secondary rate limit"},
            request_id="secondary-limit",
            headers={"Retry-After": "3"},
        ),
    ])
    client = GitHubAppClient(config(), tokens=tokens(transport), transport=transport)
    error = client.start_read_issue("acme/widgets", 1).wait(1000)

    assert isinstance(error, GitHubRateLimitError)
    assert error.retryable is True
    assert error.retry_after_ms == 3000
    assert error.request_id == "secondary-limit"


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


@pytest.mark.skipif(os.name == "nt", reason="git-http-backend fixture requires POSIX CGI semantics")
def test_real_askpass_and_subprocess_sandbox_push_to_local_auth_endpoint(
    tmp_path: Path,
) -> None:
    synthetic_token = "ghs_SYNTHETIC_LOCAL_PUSH_ONLY"
    expected_authorization = "Basic " + b64encode(
        f"x-access-token:{synthetic_token}".encode()
    ).decode()
    project_root = tmp_path / "http-root"
    project_root.mkdir()
    remote = project_root / "remote.git"
    subprocess.run(("git", "init", "--bare", str(remote)), check=True, capture_output=True)
    _git(remote, "config", "http.receivepack", "true")
    git_executable = Path(shutil.which("git") or "").resolve()

    class GitHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:
            return None

        def do_GET(self) -> None:
            self._serve_git()

        def do_POST(self) -> None:
            self._serve_git()

        def _serve_git(self) -> None:
            if self.headers.get("Authorization") != expected_authorization:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="aep-test"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            parsed = urlsplit(self.path)
            content_length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(content_length)
            environment = {
                "GIT_PROJECT_ROOT": str(project_root),
                "GIT_HTTP_EXPORT_ALL": "1",
                "PATH_INFO": parsed.path,
                "QUERY_STRING": parsed.query,
                "REQUEST_METHOD": self.command,
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                "CONTENT_LENGTH": str(content_length),
                "REMOTE_USER": "x-access-token",
            }
            completed = subprocess.run(
                [str(git_executable), "http-backend"],
                env=environment,
                input=body,
                capture_output=True,
                check=False,
            )
            headers, response_body = completed.stdout.split(b"\r\n\r\n", 1)
            status = 200
            response_headers: list[tuple[str, str]] = []
            for raw in headers.decode("latin-1").split("\r\n"):
                name, value = raw.split(":", 1)
                if name.casefold() == "status":
                    status = int(value.strip().split(" ", 1)[0])
                else:
                    response_headers.append((name.strip(), value.strip()))
            self.send_response(status)
            for name, value in response_headers:
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), GitHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        repository = tmp_path / "work"
        repository.mkdir()
        _git(repository, "init")
        _git(repository, "config", "user.name", "AEP Test")
        _git(repository, "config", "user.email", "aep@example.test")
        _git(
            repository,
            "remote",
            "add",
            "origin",
            f"http://127.0.0.1:{server.server_port}/remote.git",
        )
        (repository / "file.txt").write_text("one\n", encoding="utf-8")
        _git(repository, "add", "file.txt")
        _git(repository, "commit", "-m", "fixture")
        revision = _git(repository, "rev-parse", "HEAD")
        branch = "aep/execution/askpass-integration"
        _git(repository, "switch", "-c", branch)
        transport = ScriptedTransport(
            [response(200, {"id": 137}), token_response(synthetic_token)]
        )
        credentials = GitHubAppGitCredentialProvider(
            config(), tokens=tokens(transport), lease_root=tmp_path / "leases"
        )

        class RecordingSandbox(SubprocessGitSandbox):
            def __init__(self) -> None:
                super().__init__(
                    tmp_path / "disabled-hooks", git_executable=git_executable
                )
                self.environments: list[dict[str, str]] = []

            def run(self, **kwargs):
                self.environments.append(dict(kwargs["environment"]))
                return super().run(**kwargs)

        sandbox = RecordingSandbox()
        logs = InMemoryGitCommandLogStore()
        adapter = GitToolAdapter(
            repository=repository,
            repository_id="acme/widgets",
            expected_revision=revision,
            working_branch=branch,
            log_store=logs,
            sandbox=sandbox,
            credential_provider=credentials,
        )
        request = ToolRequest(
            tool_ref={"kind": "Tool", "name": "git", "version": "1.0.0"},
            input={
                "operation": "push_branch",
                "expectedRevision": revision,
                "branch": branch,
            },
            caller=ToolCaller(kind="TaskExecution", id="task-push-integration"),
            capabilities=("git.push",),
            timeout_ms=10_000,
            correlation={
                "traceId": "trace-push-integration",
                "workflowExecutionId": "workflow-push-integration",
                "taskExecutionId": "task-push-integration",
            },
        )
        result = adapter.start(request).wait(10_000)

        assert result is not None and result.status is ToolResultStatus.SUCCEEDED
        assert result.output["remoteMutationState"] == "CONFIRMED"
        assert _git(remote, "rev-parse", f"refs/heads/{branch}") == revision
        allowed = {
            "GIT_CONFIG_NOSYSTEM",
            "GIT_TERMINAL_PROMPT",
            "GIT_ASKPASS",
            "GIT_ASKPASS_REQUIRE",
            "AEP_GITHUB_USERNAME",
            "AEP_GITHUB_PASSWORD",
        }
        assert all(set(item) <= allowed for item in sandbox.environments)
        assert not any(
            {"PATH", "HOME", "HTTP_PROXY", "HTTPS_PROXY", "GIT_CONFIG_GLOBAL"}
            & set(item)
            for item in sandbox.environments
        )
        assert list((tmp_path / "leases").iterdir()) == []
        assert synthetic_token not in repr(result.output)
        assert synthetic_token not in logs.get(result.logs_ref)
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_askpass_helper_runs_with_only_scoped_environment_and_fails_closed(
    tmp_path: Path,
) -> None:
    synthetic_token = "ghs_SYNTHETIC_ASKPASS_ONLY"
    transport = ScriptedTransport(
        [response(200, {"id": 137}), token_response(synthetic_token)]
    )
    root = tmp_path / "leases"
    provider = GitHubAppGitCredentialProvider(
        config(), tokens=tokens(transport), lease_root=root
    )
    lease = provider.acquire(remote="origin", branch="aep/execution/abc")
    environment = dict(lease.environment)
    helper = Path(environment["GIT_ASKPASS"])
    lease_directory = helper.parent
    command = [str(helper)] if os.name != "nt" else [sys.executable, str(helper)]

    assert set(environment) == {
        "GIT_ASKPASS",
        "GIT_ASKPASS_REQUIRE",
        "AEP_GITHUB_USERNAME",
        "AEP_GITHUB_PASSWORD",
    }
    assert not {
        "PATH",
        "HOME",
        "USERPROFILE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "GIT_CONFIG_GLOBAL",
        "GIT_TERMINAL_PROMPT",
    }.intersection(environment)
    if os.name != "nt":
        assert stat.S_IMODE(lease_directory.stat().st_mode) == 0o700
        assert stat.S_IMODE(helper.stat().st_mode) == 0o700
    assert helper.read_text(encoding="utf-8").splitlines()[0] == (
        f"#!{Path(sys.executable).resolve()}"
    )

    username = subprocess.run(
        [*command, "Username for 'https://github.com':"],
        env=environment,
        capture_output=True,
        check=False,
    )
    password = subprocess.run(
        [*command, "Password for 'https://github.com':"],
        env=environment,
        capture_output=True,
        check=False,
    )
    unsupported = subprocess.run(
        [*command, "Credential for 'https://github.com':"],
        env=environment,
        capture_output=True,
        check=False,
    )
    assert username.returncode == 0 and username.stdout == b"x-access-token"
    assert password.returncode == 0
    assert sha256(password.stdout).digest() == sha256(synthetic_token.encode()).digest()
    assert username.stderr == password.stderr == b""
    assert unsupported.returncode != 0
    assert unsupported.stdout == unsupported.stderr == b""

    lease.validate_startup(timeout_ms=5_000)
    lease.close()
    lease.close()
    assert not lease_directory.exists()
    assert lease.environment == {}
    persisted = repr(
        {
            "username": username.returncode,
            "password": password.returncode,
            "unsupported": unsupported.returncode,
            "environment": lease.environment,
        }
    )
    assert synthetic_token not in persisted


def test_askpass_rejects_unsafe_or_missing_interpreter(tmp_path: Path) -> None:
    provider_tokens = tokens(ScriptedTransport([]))
    for interpreter in ("python", tmp_path / "missing-python"):
        with pytest.raises(GitHubAppConfigurationError, match="absolute executable"):
            GitHubAppGitCredentialProvider(
                config(),
                tokens=provider_tokens,
                lease_root=tmp_path / "leases",
                interpreter=interpreter,
            )


def test_askpass_partial_creation_and_token_failure_remove_private_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "leases"
    provider = GitHubAppGitCredentialProvider(
        config(), tokens=tokens(ScriptedTransport([])), lease_root=root
    )

    def fail_open(*args, **kwargs):
        raise OSError("synthetic creation failure")

    monkeypatch.setattr("aep.github_app_provider.os.open", fail_open)
    with pytest.raises(Exception, match="could not be created"):
        provider.acquire(remote="origin", branch="aep/execution/abc")
    assert list(root.iterdir()) == []

    monkeypatch.undo()
    failing = GitHubAppGitCredentialProvider(
        config(),
        tokens=tokens(ScriptedTransport([RuntimeError("synthetic token failure")])),
        lease_root=root,
    )
    with pytest.raises(RuntimeError, match="synthetic token failure"):
        failing.acquire(remote="origin", branch="aep/execution/abc")
    assert list(root.iterdir()) == []


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
