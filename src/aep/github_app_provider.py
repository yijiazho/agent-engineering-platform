"""Repository-bound GitHub App authentication and provider adapters.

Authentication is deliberately runtime configuration rather than a Resource.
The module exposes small secret, signing, and HTTP boundaries so tests and
deployments never need to put credentials in Tool inputs or persisted evidence.
"""

from __future__ import annotations

from base64 import urlsafe_b64encode
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
import os
from pathlib import Path
from threading import Condition
from time import monotonic
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from aep.execution_checkout import RepositoryIdentity
from aep.github_tool import (
    GitHubProviderError,
    GitHubProviderOperation,
    GitHubRateLimitError,
)


class GitHubAppConfigurationError(ValueError):
    """Safe, non-secret configuration failure."""


class GitHubAppAuthenticationError(GitHubProviderError):
    classification = "AUTHENTICATION"


class GitHubAppAuthorizationError(GitHubProviderError):
    classification = "AUTHORIZATION"


class GitHubAppValidationError(GitHubProviderError):
    classification = "VALIDATION"


class GitHubAppAmbiguousMutationError(GitHubProviderError):
    classification = "AMBIGUOUS_MUTATION"


class SecretProvider(Protocol):
    def read(self) -> bytes: ...


class FileSecretProvider:
    """Read a secret at use time so rotation does not require process restart."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)

    def read(self) -> bytes:
        try:
            value = self._path.read_bytes()
        except OSError:
            raise GitHubAppConfigurationError("GitHub App private key is unavailable") from None
        if not value.strip():
            raise GitHubAppConfigurationError("GitHub App private key is empty")
        return value


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class HttpTransport(Protocol):
    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_ms: int,
    ) -> HttpResponse: ...


class HttpTransportTimeout(TimeoutError):
    pass


class UrllibHttpTransport:
    """Minimal standard-library HTTPS transport with bounded response bodies."""

    def __init__(self, *, max_response_bytes: int = 2 * 1024 * 1024) -> None:
        if max_response_bytes < 1:
            raise ValueError("max_response_bytes must be positive")
        self._limit = max_response_bytes

    def request(self, *, method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout_ms: int) -> HttpResponse:
        request = Request(url, data=body, headers=dict(headers), method=method)
        try:
            with urlopen(request, timeout=max(0.001, timeout_ms / 1000)) as response:
                content = response.read(self._limit + 1)
                if len(content) > self._limit:
                    raise GitHubProviderError("GitHub response exceeded configured limit")
                return HttpResponse(response.status, dict(response.headers.items()), content)
        except HTTPError as error:
            content = error.read(self._limit + 1)
            return HttpResponse(error.code, dict(error.headers.items()), content[: self._limit])
        except (TimeoutError, URLError, OSError):
            raise HttpTransportTimeout("GitHub request did not complete") from None


@dataclass(frozen=True, slots=True)
class GitHubAppConfig:
    app_id: int
    owner: str
    repository: str
    authorized_branch_prefix: str = "aep/execution/"
    base_branch: str = "main"
    api_url: str = "https://api.github.com"
    token_refresh_skew: timedelta = timedelta(minutes=5)

    def __post_init__(self) -> None:
        if self.app_id < 1:
            raise GitHubAppConfigurationError("GitHub App id must be positive")
        for label, value in (("owner", self.owner), ("repository", self.repository), ("base branch", self.base_branch)):
            if not value or any(character.isspace() for character in value):
                raise GitHubAppConfigurationError(f"GitHub {label} is invalid")
        if not self.authorized_branch_prefix:
            raise GitHubAppConfigurationError("authorized branch prefix is required")
        parsed_api = urlsplit(self.api_url)
        if (
            parsed_api.scheme != "https"
            or not parsed_api.hostname
            or parsed_api.username
            or parsed_api.password
            or parsed_api.query
            or parsed_api.fragment
        ):
            raise GitHubAppConfigurationError("GitHub API URL must use HTTPS")
        if self.token_refresh_skew < timedelta(0):
            raise GitHubAppConfigurationError("token refresh skew cannot be negative")

    @property
    def repository_id(self) -> str:
        return f"{self.owner}/{self.repository}"


class RS256Signer:
    """Sign GitHub App JWTs using unencrypted PKCS#1 or PKCS#8 PEM keys."""

    _DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")

    def sign(self, message: bytes, private_key: bytes) -> bytes:
        der = _pem_der(private_key)
        values = _der_sequence(der)
        if len(values) >= 9 and values[0][0] == 2:  # PKCS#1 RSAPrivateKey
            rsa_values = values
        elif len(values) == 3 and values[2][0] == 4:  # PKCS#8 PrivateKeyInfo
            rsa_values = _der_sequence(values[2][1])
        else:
            raise GitHubAppConfigurationError("GitHub App private key format is unsupported")
        try:
            modulus = _der_integer(rsa_values[1])
            private_exponent = _der_integer(rsa_values[3])
        except (IndexError, ValueError):
            raise GitHubAppConfigurationError("GitHub App private key is invalid") from None
        size = (modulus.bit_length() + 7) // 8
        if size < 256:
            raise GitHubAppConfigurationError("GitHub App RSA key must be at least 2048 bits")
        digest_info = self._DIGEST_INFO + sha256(message).digest()
        padding_length = size - len(digest_info) - 3
        if padding_length < 8:
            raise GitHubAppConfigurationError("GitHub App RSA key is invalid")
        encoded = b"\x00\x01" + b"\xff" * padding_length + b"\x00" + digest_info
        signature = pow(int.from_bytes(encoded), private_exponent, modulus)
        return signature.to_bytes(size, "big")


@dataclass(frozen=True, slots=True)
class _Token:
    value: str
    expires_at: datetime
    installation_id: int


class GitHubAppTokenProvider:
    """Resolve one installation and concurrency-safely cache its short-lived token."""

    def __init__(self, config: GitHubAppConfig, *, private_key: SecretProvider, transport: HttpTransport, signer: RS256Signer | None = None, clock: Callable[[], datetime] | None = None, monotonic_clock: Callable[[], float] = monotonic) -> None:
        self.config = config
        self._secret = private_key
        self._transport = transport
        self._signer = signer or RS256Signer()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._monotonic = monotonic_clock
        self._condition = Condition()
        self._refreshing = False
        self._token: _Token | None = None
        self._installation_id: int | None = None

    def token(self, *, timeout_ms: int = 10_000) -> str:
        if timeout_ms < 1:
            raise GitHubProviderError("GitHub token timeout must be positive")
        deadline = self._monotonic() + timeout_ms / 1000
        with self._condition:
            while self._refreshing:
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    raise HttpTransportTimeout(
                        "GitHub token refresh exceeded the operation deadline"
                    )
                self._condition.wait(timeout=remaining)
            now = self._now()
            if self._token is not None and self._token.expires_at - self.config.token_refresh_skew > now:
                return self._token.value
            self._refreshing = True
        try:
            token = self._refresh(deadline)
        finally:
            with self._condition:
                self._refreshing = False
                self._condition.notify_all()
        with self._condition:
            if self._installation_id != token.installation_id:
                raise GitHubProviderError(
                    "GitHub installation changed during token refresh",
                    retryable=True,
                )
            self._token = token
            return token.value

    def discard(self, value: str | None = None) -> None:
        with self._condition:
            if self._token is not None and (value is None or self._token.value == value):
                self._token = None

    def readiness(self, *, timeout_ms: int = 10_000) -> dict[str, Any]:
        if timeout_ms < 1:
            raise GitHubProviderError("GitHub readiness timeout must be positive")
        deadline = self._monotonic() + timeout_ms / 1000
        installation = self._resolve_installation(deadline, revalidate=True)
        return {
            "status": "READY",
            "provider": "github-app",
            "repository": self.config.repository_id,
            "appId": self.config.app_id,
            "installationId": installation,
            "baseBranch": self.config.base_branch,
            "authorizedBranchPrefix": self.config.authorized_branch_prefix,
        }

    def _refresh(self, deadline: float) -> _Token:
        installation = self._resolve_installation(deadline)
        try:
            response = self._request(
                "POST",
                f"/app/installations/{installation}/access_tokens",
                bearer=self._app_jwt(),
                body={"repositories": [self.config.repository]},
                timeout_ms=self._remaining_ms(deadline),
            )
        except (
            GitHubAppAuthenticationError,
            GitHubAppAuthorizationError,
            GitHubAppValidationError,
        ):
            self._invalidate_installation(installation)
            raise
        payload = _json_object(response.body)
        token = payload.get("token")
        expires = payload.get("expires_at")
        if not isinstance(token, str) or not token or not isinstance(expires, str):
            raise GitHubProviderError("GitHub token response was invalid")
        try:
            expires_at = datetime.fromisoformat(expires.replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            raise GitHubProviderError("GitHub token expiry was invalid") from None
        if expires_at <= self._now():
            raise GitHubProviderError("GitHub returned an expired installation token")
        return _Token(token, expires_at, installation)

    def _resolve_installation(self, deadline: float, *, revalidate: bool = False) -> int:
        with self._condition:
            if self._installation_id is not None and not revalidate:
                return self._installation_id
            previous = self._installation_id
        try:
            response = self._request(
                "GET",
                f"/repos/{quote(self.config.owner, safe='')}/{quote(self.config.repository, safe='')}/installation",
                bearer=self._app_jwt(),
                body=None,
                timeout_ms=self._remaining_ms(deadline),
            )
        except Exception:
            if revalidate:
                self._invalidate_installation(previous)
            raise
        value = _json_object(response.body).get("id")
        if not isinstance(value, int) or value < 1:
            raise GitHubProviderError("GitHub installation response was invalid")
        with self._condition:
            if self._installation_id != value:
                self._installation_id = value
                self._token = None
            return self._installation_id

    def _invalidate_installation(self, expected: int | None) -> None:
        with self._condition:
            if expected is None or self._installation_id == expected:
                self._installation_id = None
                self._token = None

    def _remaining_ms(self, deadline: float) -> int:
        remaining = int((deadline - self._monotonic()) * 1000)
        if remaining < 1:
            raise HttpTransportTimeout("GitHub operation deadline expired")
        return remaining

    def _app_jwt(self) -> str:
        now = int(self._now().timestamp())
        header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
        claims = _b64url(json.dumps({"iat": now - 60, "exp": now + 540, "iss": str(self.config.app_id)}, separators=(",", ":")).encode())
        signing_input = f"{header}.{claims}".encode("ascii")
        try:
            key = self._secret.read()
            signature = self._signer.sign(signing_input, key)
        except GitHubAppConfigurationError:
            raise
        except Exception:
            raise GitHubAppConfigurationError("GitHub App signing boundary failed") from None
        return f"{header}.{claims}.{_b64url(signature)}"

    def _request(self, method: str, path: str, *, bearer: str, body: Mapping[str, Any] | None, timeout_ms: int) -> HttpResponse:
        encoded = json.dumps(body, separators=(",", ":")).encode() if body is not None else None
        try:
            response = self._transport.request(
                method=method,
                url=self.config.api_url.rstrip("/") + path,
                headers=_headers(bearer),
                body=encoded,
                timeout_ms=timeout_ms,
            )
        except HttpTransportTimeout:
            raise HttpTransportTimeout("GitHub authentication request timed out") from None
        if response.status < 200 or response.status >= 300:
            raise _provider_error(response)
        return response

    def _now(self) -> datetime:
        value = self._clock()
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class _Operation(GitHubProviderOperation):
    def __init__(self, run: Callable[[float, Callable[[], None]], Mapping[str, Any]], *, clock: Callable[[], float]) -> None:
        self._run = run
        self._clock = clock
        self._done = False
        self._cancelled = False
        self._timed_out = False
        self._result: Mapping[str, Any] | Exception | None = None
        self._request_id: str | None = None
        self._mutation_started = False

    @property
    def request_id(self) -> str | None:
        return self._request_id

    @property
    def mutation_started(self) -> bool:
        return self._mutation_started

    def wait(self, timeout_ms: int) -> Mapping[str, Any] | Exception | None:
        if self._timed_out:
            return None
        if self._cancelled and not self._done:
            return None
        if not self._done:
            try:
                deadline = self._clock() + timeout_ms / 1000
                self._result = self._run(deadline, self._mark_mutation_started)
                if isinstance(self._result, Mapping):
                    value = self._result.get("requestId")
                    self._request_id = value if isinstance(value, str) else None
            except HttpTransportTimeout:
                self._timed_out = True
                return None
            except Exception as error:
                self._result = error
            self._done = True
        return self._result

    def terminate(self) -> None:
        self._cancelled = True

    def kill(self) -> None:
        self._cancelled = True

    def cleanup(self) -> None:
        self._run = lambda _deadline, _mark_mutation: {}

    def _mark_mutation_started(self) -> None:
        self._mutation_started = True


class GitHubAppClient:
    """GitHub client implementation consumed by ``GitHubToolAdapter``."""

    def __init__(self, config: GitHubAppConfig, *, tokens: GitHubAppTokenProvider, transport: HttpTransport, clock: Callable[[], float] = monotonic) -> None:
        if tokens.config != config:
            raise GitHubAppConfigurationError("GitHub token and client bindings differ")
        self.config = config
        self._tokens = tokens
        self._transport = transport
        self._clock = clock

    def start_read_issue(self, repository: str, issue_number: int) -> GitHubProviderOperation:
        self._require_repository(repository)
        return _Operation(
            lambda deadline, _mark: self._read_issue(issue_number, deadline),
            clock=self._clock,
        )

    def start_create_pull_request(self, repository: str, *, head: str, base: str, title: str, body: str) -> GitHubProviderOperation:
        self._require_repository(repository)
        if not head.startswith(self.config.authorized_branch_prefix) or head == self.config.authorized_branch_prefix:
            raise GitHubAppAuthorizationError("pull-request head is outside the authorized execution branch scope")
        if base != self.config.base_branch:
            raise GitHubAppValidationError("pull-request base does not match the bound repository")
        return _Operation(
            lambda deadline, mark: self._create_or_reconcile(
                head, base, title, body, deadline, mark
            ),
            clock=self._clock,
        )

    def _read_issue(self, issue_number: int, deadline: float) -> Mapping[str, Any]:
        response = self._api("GET", f"/issues/{issue_number}", None, deadline)
        value = _json_object(response.body)
        return {
            "number": value.get("number"), "title": value.get("title"), "body": value.get("body"),
            "state": value.get("state"), "url": value.get("html_url"),
            "author": value.get("user", {}).get("login") if isinstance(value.get("user"), Mapping) else None,
            "labels": [item.get("name") for item in value.get("labels", []) if isinstance(item, Mapping) and isinstance(item.get("name"), str)],
            "requestId": _request_id(response.headers),
        }

    def _create_or_reconcile(self, head: str, base: str, title: str, body: str, deadline: float, mark_mutation: Callable[[], None]) -> Mapping[str, Any]:
        query = urlencode({"state": "all", "head": f"{self.config.owner}:{head}", "base": base, "per_page": "2"})
        existing_response = self._api("GET", f"/pulls?{query}", None, deadline)
        existing = _json_value(existing_response.body)
        if not isinstance(existing, list):
            raise GitHubProviderError("GitHub pull-request reconciliation response was invalid")
        if len(existing) > 1:
            raise GitHubAppValidationError("multiple pull requests match the authorized publication target")
        if existing:
            return _pull_response(existing[0], head, base, _request_id(existing_response.headers))
        response = self._api(
            "POST",
            "/pulls",
            {"head": head, "base": base, "title": title, "body": body},
            deadline,
            mark_mutation=mark_mutation,
        )
        try:
            return _pull_response(
                _json_object(response.body),
                head,
                base,
                _request_id(response.headers),
            )
        except Exception:
            raise GitHubAppAmbiguousMutationError(
                "GitHub pull-request mutation outcome is unknown",
                retryable=False,
                request_id=_request_id(response.headers),
            ) from None

    def _api(self, method: str, suffix: str, body: Mapping[str, Any] | None, deadline: float, *, mark_mutation: Callable[[], None] | None = None) -> HttpResponse:
        token = self._tokens.token(timeout_ms=self._remaining_ms(deadline))
        encoded = json.dumps(body, separators=(",", ":")).encode() if body is not None else None
        path = f"/repos/{quote(self.config.owner, safe='')}/{quote(self.config.repository, safe='')}{suffix}"
        try:
            timeout_ms = self._remaining_ms(deadline)
            if mark_mutation is not None:
                mark_mutation()
            response = self._transport.request(method=method, url=self.config.api_url.rstrip("/") + path, headers=_headers(token), body=encoded, timeout_ms=timeout_ms)
        except HttpTransportTimeout:
            raise HttpTransportTimeout("GitHub request timed out") from None
        except Exception:
            if mark_mutation is not None:
                raise GitHubAppAmbiguousMutationError(
                    "GitHub pull-request mutation outcome is unknown",
                    retryable=False,
                ) from None
            raise GitHubProviderError("GitHub transport failed", retryable=True) from None
        if response.status < 200 or response.status >= 300:
            error = _provider_error(response)
            if response.status == 401:
                self._tokens.discard(token)
            if method == "POST" and suffix == "/pulls" and error.retryable:
                raise GitHubAppAmbiguousMutationError(
                    "GitHub pull-request mutation outcome is unknown",
                    retryable=False,
                    request_id=error.request_id,
                ) from None
            raise error
        return response

    def _remaining_ms(self, deadline: float) -> int:
        remaining = int((deadline - self._clock()) * 1000)
        if remaining < 1:
            raise HttpTransportTimeout("GitHub operation deadline expired")
        return remaining

    def _require_repository(self, repository: str) -> None:
        if repository.casefold() != self.config.repository_id.casefold():
            raise GitHubAppAuthorizationError("repository differs from the GitHub App binding")


class _GitCredentialLease:
    def __init__(self, token: str, askpass_path: Path) -> None:
        self._environment = {
            "GIT_ASKPASS": str(askpass_path), "GIT_ASKPASS_REQUIRE": "force",
            "AEP_GITHUB_USERNAME": "x-access-token", "AEP_GITHUB_PASSWORD": token,
        }
        self._path = askpass_path
        self._closed = False

    @property
    def environment(self) -> Mapping[str, str]:
        return {} if self._closed else dict(self._environment)

    def close(self) -> None:
        if self._closed:
            return
        for key in tuple(self._environment):
            self._environment[key] = ""
        self._environment.clear()
        try:
            self._path.unlink(missing_ok=True)
        except OSError:
            pass
        self._closed = True


class GitHubAppGitCredentialProvider:
    """Supply one-use askpass leases for source fetches and authorized pushes."""

    def __init__(self, config: GitHubAppConfig, *, tokens: GitHubAppTokenProvider, lease_root: Path | str) -> None:
        if tokens.config != config:
            raise GitHubAppConfigurationError("GitHub token and Git credential bindings differ")
        self.config = config
        self._tokens = tokens
        self._root = Path(lease_root).resolve()

    def acquire(self, *, remote: str | None = None, branch: str | None = None, repository: RepositoryIdentity | None = None) -> _GitCredentialLease:
        if repository is not None and repository.canonical.casefold() != f"github:{self.config.repository_id}".casefold():
            raise GitHubAppAuthorizationError("source repository differs from the GitHub App binding")
        if branch is not None and (not branch.startswith(self.config.authorized_branch_prefix) or branch == self.config.authorized_branch_prefix):
            raise GitHubAppAuthorizationError("Git branch is outside the authorized execution branch scope")
        if remote is not None and remote != "origin":
            raise GitHubAppAuthorizationError("Git remote differs from the authorized remote")
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            self._root.chmod(0o700)
        except OSError:
            raise GitHubProviderError(
                "Git credential lease directory is unavailable", retryable=True
            ) from None
        path = self._root / f"askpass-{os.urandom(16).hex()}.py"
        script = (
            "#!/usr/bin/env python3\nimport os,sys\n"
            "key='AEP_GITHUB_USERNAME' if 'username' in sys.argv[1].lower() else 'AEP_GITHUB_PASSWORD'\n"
            "sys.stdout.write(os.environ.get(key,''))\n"
        )
        try:
            path.write_text(script, encoding="utf-8")
            path.chmod(0o700)
        except OSError:
            path.unlink(missing_ok=True)
            raise GitHubProviderError("Git credential lease could not be created", retryable=True) from None
        try:
            return _GitCredentialLease(self._tokens.token(), path)
        except Exception:
            path.unlink(missing_ok=True)
            raise


@dataclass(frozen=True, slots=True)
class GitHubAppProviderBundle:
    config: GitHubAppConfig
    tokens: GitHubAppTokenProvider
    client: GitHubAppClient
    credentials: GitHubAppGitCredentialProvider

    def readiness(self, *, timeout_ms: int = 10_000) -> dict[str, Any]:
        return self.tokens.readiness(timeout_ms=timeout_ms)


def github_app_provider_from_environment(
    environment: Mapping[str, str],
    *,
    transport: HttpTransport | None = None,
    signer: RS256Signer | None = None,
    clock: Callable[[], datetime] | None = None,
    monotonic_clock: Callable[[], float] = monotonic,
) -> GitHubAppProviderBundle:
    """Build the live provider from deployment inputs, failing closed."""

    required = (
        "AEP_GITHUB_APP_ID",
        "AEP_GITHUB_APP_PRIVATE_KEY_FILE",
        "AEP_REPOSITORY_OWNER",
        "AEP_REPOSITORY_NAME",
        "AEP_REPOSITORY_DEFAULT_BRANCH",
        "AEP_STATE_ROOT",
    )
    missing = [name for name in required if not environment.get(name, "").strip()]
    if missing:
        raise GitHubAppConfigurationError(
            "missing GitHub App configuration: " + ", ".join(missing)
        )
    try:
        app_id = int(environment["AEP_GITHUB_APP_ID"])
    except ValueError:
        raise GitHubAppConfigurationError("AEP_GITHUB_APP_ID must be an integer") from None
    config = GitHubAppConfig(
        app_id=app_id,
        owner=environment["AEP_REPOSITORY_OWNER"],
        repository=environment["AEP_REPOSITORY_NAME"],
        base_branch=environment["AEP_REPOSITORY_DEFAULT_BRANCH"],
        authorized_branch_prefix=environment.get(
            "AEP_GITHUB_AUTHORIZED_BRANCH_PREFIX", "aep/execution/"
        ),
        api_url=environment.get("AEP_GITHUB_API_URL", "https://api.github.com"),
    )
    http = transport or UrllibHttpTransport()
    tokens = GitHubAppTokenProvider(
        config,
        private_key=FileSecretProvider(environment["AEP_GITHUB_APP_PRIVATE_KEY_FILE"]),
        transport=http,
        signer=signer,
        clock=clock,
        monotonic_clock=monotonic_clock,
    )
    credentials = GitHubAppGitCredentialProvider(
        config,
        tokens=tokens,
        lease_root=Path(environment["AEP_STATE_ROOT"]) / "github-app-credential-leases",
    )
    return GitHubAppProviderBundle(
        config=config,
        tokens=tokens,
        client=GitHubAppClient(
            config, tokens=tokens, transport=http, clock=monotonic_clock
        ),
        credentials=credentials,
    )


def _headers(bearer: str) -> dict[str, str]:
    return {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {bearer}", "Content-Type": "application/json", "User-Agent": "aep-github-app/1", "X-GitHub-Api-Version": "2022-11-28"}


def _provider_error(response: HttpResponse) -> GitHubProviderError:
    request_id = _request_id(response.headers)
    retry_after = _retry_after_ms(response.headers)
    if response.status == 401:
        return GitHubAppAuthenticationError("GitHub App authentication failed", request_id=request_id)
    if response.status == 403 and retry_after is None and not _rate_limited(response.headers):
        return GitHubAppAuthorizationError("GitHub App permission denied", request_id=request_id)
    if response.status in {400, 404, 409, 422}:
        return GitHubAppValidationError("GitHub rejected the bound request", request_id=request_id)
    if response.status == 429 or _rate_limited(response.headers):
        return GitHubRateLimitError(request_id=request_id, retry_after_ms=retry_after or 1000)
    if response.status >= 500:
        return GitHubProviderError("GitHub service is temporarily unavailable", retryable=True, request_id=request_id, retry_after_ms=retry_after)
    return GitHubProviderError("GitHub provider request failed", request_id=request_id)


def _request_id(headers: Mapping[str, str]) -> str | None:
    for key, value in headers.items():
        if key.casefold() == "x-github-request-id" and value:
            return value
    return None


def _retry_after_ms(headers: Mapping[str, str]) -> int | None:
    for key, value in headers.items():
        if key.casefold() == "retry-after":
            try:
                return min(60_000, max(0, int(float(value) * 1000)))
            except ValueError:
                return None
    return None


def _rate_limited(headers: Mapping[str, str]) -> bool:
    return any(key.casefold() == "x-ratelimit-remaining" and value == "0" for key, value in headers.items())


def _json_value(body: bytes) -> Any:
    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise GitHubProviderError("GitHub returned malformed JSON") from None


def _json_object(body: bytes) -> Mapping[str, Any]:
    value = _json_value(body)
    if not isinstance(value, Mapping):
        raise GitHubProviderError("GitHub returned an invalid response shape")
    return value


def _pull_response(value: Any, head: str, base: str, request_id: str | None) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not isinstance(value.get("number"), int) or not isinstance(value.get("html_url"), str):
        raise GitHubProviderError("GitHub pull-request response was invalid")
    return {"number": value["number"], "url": value["html_url"], "head": head, "base": base, "requestId": request_id}


def _b64url(value: bytes) -> str:
    return urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _pem_der(value: bytes) -> bytes:
    lines = [line.strip() for line in value.splitlines()]
    payload = b"".join(line for line in lines if line and not line.startswith(b"-----"))
    try:
        import base64
        return base64.b64decode(payload, validate=True)
    except Exception:
        raise GitHubAppConfigurationError("GitHub App private key is invalid") from None


def _der_length(value: bytes, offset: int) -> tuple[int, int]:
    if offset >= len(value):
        raise ValueError("truncated DER")
    first = value[offset]
    if first < 128:
        return first, offset + 1
    count = first & 0x7F
    if count == 0 or count > 4 or offset + count >= len(value):
        raise ValueError("invalid DER length")
    return int.from_bytes(value[offset + 1 : offset + 1 + count], "big"), offset + 1 + count


def _der_sequence(value: bytes) -> list[tuple[int, bytes]]:
    if not value or value[0] != 0x30:
        raise GitHubAppConfigurationError("GitHub App private key is invalid")
    length, offset = _der_length(value, 1)
    end = offset + length
    if end != len(value):
        raise GitHubAppConfigurationError("GitHub App private key is invalid")
    items: list[tuple[int, bytes]] = []
    while offset < end:
        tag = value[offset]
        length, content_offset = _der_length(value, offset + 1)
        content_end = content_offset + length
        if content_end > end:
            raise GitHubAppConfigurationError("GitHub App private key is invalid")
        items.append((tag, value[content_offset:content_end]))
        offset = content_end
    return items


def _der_integer(value: tuple[int, bytes]) -> int:
    if value[0] != 2 or not value[1]:
        raise ValueError("not a DER integer")
    return int.from_bytes(value[1], "big", signed=False)
