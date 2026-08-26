# GitHub App Provider Operations

The AEP GitHub provider binds one deployment to one installed GitHub App and
one repository. The App authenticates GitHub API issue reads and pull-request
creation, repository source fetches, and execution-branch pushes. It does not
authorize those actions: Publication Policy and the separate `git.push` and
`github.create_pr` capability decisions remain mandatory Tool Runtime gates.

## Create And Install The App

Create a GitHub App owned by the repository owner and configure its webhook to
send JSON to:

```text
https://<event-controller>/v1/webhooks/github
```

Set a webhook secret and subscribe only to **Issues**. AEP accepts the
`issues/opened` action; the App does not need Push or Pull request webhook
subscriptions for the MVP.

Grant these repository permissions and no organization or account permissions:

| Repository permission | Access | Used for |
| --- | --- | --- |
| Metadata | Read | Installation and repository identity |
| Issues | Read | Triggering issue retrieval |
| Contents | Read and write | Default-branch fetch and execution-branch push |
| Pull requests | Read and write | Duplicate lookup and PR creation |

Install the App on **Only select repositories**, select the bound repository,
and record the numeric App ID. AEP resolves the installation through
`GET /repos/{owner}/{repository}/installation`; operators do not configure or
persist an installation token or installation ID.

## Supply Runtime Secrets And Binding

Mount the App's unencrypted PEM private key as a read-only secret file. Keep it
separate from the webhook HMAC secret. Configure the provider process with:

```text
AEP_GITHUB_APP_ID=<numeric-app-id>
AEP_GITHUB_APP_PRIVATE_KEY_FILE=/run/secrets/github-app-private-key.pem
AEP_REPOSITORY_OWNER=<owner>
AEP_REPOSITORY_NAME=<repository>
AEP_REPOSITORY_DEFAULT_BRANCH=main
AEP_STATE_ROOT=/var/lib/aep
```

`AEP_GITHUB_API_URL` defaults to `https://api.github.com` and may identify a
trusted GitHub Enterprise API endpoint. `AEP_GITHUB_AUTHORIZED_BRANCH_PREFIX`
defaults to `aep/execution/`. The standard environment factory rejects missing
inputs, non-HTTPS API URLs, invalid App IDs, and inconsistent provider
bindings. Never put these values, the PEM content, webhook secret, JWTs, or
installation tokens in `.ai/` Resources.

At startup, construct the provider with
`github_app_provider_from_environment`. Its bundle supplies:

* `client` to `GitHubToolAdapter`;
* `credentials` to both `GitToolAdapter` and `ExecutionCheckoutManager`; and
* `readiness()` for a credential-safe installation diagnostic.

Readiness must report `READY`, provider, repository, App ID, installation ID,
base branch, and authorized branch prefix. It never reports secret paths,
private-key material, JWTs, or installation tokens. A failed installation
lookup keeps the provider unready. Every readiness probe revalidates the
repository installation; an uninstall, suspension, or replacement installation
therefore cannot remain hidden behind a process-local identity cache.

Independently prove the credential-helper executable without a private key,
installation token, GitHub network call, or inherited process environment:

```powershell
python -m aep.github_app_provider askpass-readiness
```

Run that exact command in the release container. It must report `READY` and
the resolved absolute interpreter embedded in the helper (the current standard
service image is `/usr/local/bin/python3.12`). A relative interpreter,
`env python3`, missing file,
or non-executable file is configuration drift and must block publication.
The Docker-capable validation workflow builds `deploy/local/Dockerfile` and
runs the service-image askpass pytest gate with
`AEP_RUN_SERVICE_IMAGE_TESTS=1`; changes to the Dockerfile also trigger that
workflow. For the equivalent focused local check, run:

```powershell
$env:AEP_RUN_SERVICE_IMAGE_TESTS = "1"
.\.venv\Scripts\python.exe -m pytest tests/test_dogfood_deployment.py::test_service_image_runs_askpass_with_its_absolute_interpreter
```

## Token And Mutation Behavior

The provider signs a GitHub App JWT only at the authentication boundary,
requests a repository-restricted installation token, and renews it before
expiry under a concurrency lock. API authentication failure discards the
cached token. Git credential leases inject a token through a temporary askpass
program and process environment; the checkout remote URL and Git command
arguments never contain credentials. Each private `0700` lease directory holds
one `0700` helper whose shebang is the running service's verified absolute
Python executable. The helper answers only Git username and password prompts,
fails closed for every other invocation, and receives only askpass and
credential variables. Each lease clears its environment and removes its helper
and private directory after fetch or push, including failure and timeout paths.
Authenticated Git commands also apply an empty command-scoped
`credential.helper` value, resetting any helper configured in the repository
before the token-bearing process starts.

Before `git push`, the Git Tool executes both expected prompts using the same
minimal helper environment. A deterministic helper configuration/startup
failure is Tool `STARTUP` with remote mutation `NOT_ATTEMPTED`; it may be
retried after correcting the immutable service generation. Once the push
process starts, any unconfirmed failure remains `UNKNOWN` and is never
automatically replayed. Do not diagnose either case by printing helper source,
environment, stdout, provider response bodies, or unrestricted stderr.

Before PR creation, the Tool Runtime verifies the immutable publication
evidence graph and matching successful push. The provider then independently
checks the repository, execution-branch prefix, and base branch. It queries for
an existing PR with the same owner/head/base before posting, making a retry
reconcile rather than duplicate the PR. Timeouts and retryable server responses
after a PR POST are recorded as an unknown mutation outcome and are not
automatically replayed.

One monotonic deadline covers installation lookup, token creation, duplicate
lookup, and PR creation. Each request receives only the remaining time. A
timeout before the PR POST begins is `NOT_ATTEMPTED` and may be retried; a
timeout after the POST begins is `UNKNOWN` and requires reconciliation.
This includes transport failure, oversized or malformed successful responses,
and any other condition that prevents confirmation after the POST starts.
Installation lookup and installation-token transport timeouts preserve timeout
classification through the Tool boundary rather than becoming provider errors.
Git push credential acquisition receives the Git Tool's remaining deadline, so
an empty or expiring token cache cannot extend the invocation beyond its
configured timeout. GitHub secondary-limit `403` responses carrying
`Retry-After` are treated as bounded, retryable rate limits even when
`X-RateLimit-Remaining` is absent.

For an `UNKNOWN` push, obtain the expected repository, execution branch, base,
and committed head from persisted evidence, then query the remote branch and
open PRs. No branch means the mutation was absent but does not create a PR;
the exact expected branch/head may be confirmed and reused; any different head
is a conflict and requires operator isolation. In all three cases, separately
verify Publication Policy and the `git.push` decision before push handling, and
require a new `github.create_pr` authorization only after the exact pushed head
is confirmed. Never infer PR permission from askpass success.

## Rotate Or Revoke Credentials

To rotate the App private key:

1. Generate a second private key in GitHub App settings.
2. Atomically replace the mounted secret file and restart or roll the provider
   processes so no process retains an old file handle.
3. Require readiness to resolve the same repository and installation identity.
4. Delete the old private key in GitHub only after every process is ready.

Installation tokens expire automatically and are never operator-managed. To
revoke access immediately, suspend or uninstall the App from the repository,
then stop AEP workers. Reinstalling or changing selected repositories requires
a fresh readiness check before webhook delivery is enabled.
