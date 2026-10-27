# macOS And Linux Host Operations

This guide is the POSIX companion to the
[self-hosting dogfood runbook](self-hosting-dogfood.md). It covers the same
repository-bound deployment and controlled pilot on macOS or Linux. The Compose
services, environment variable names, immutable Resource identity, webhook
contract, and evidence contracts are identical; only host paths, shell syntax,
and a few platform-specific settings differ.

The original dogfood profile was operated from Windows PowerShell. Both profiles
are supported. Use this guide when the host shell is `zsh`, `bash`, or another
POSIX shell, or when the host is macOS.

## Platform Scope

* Windows PowerShell 5 is documented in the main runbook.
* macOS arm64, macOS x86_64, and Linux x86_64 are documented here.
* CPython 3.12 is the pinned validation interpreter. Newer 3.12+ interpreters
  run the development suite, but the hash-locked offline proof is pinned to
  3.12.
* The deterministic pytest suite runs on Windows, macOS, and Linux.
* The Docker validation gate and published-image checks build and run
  `linux/amd64` containers. Docker Desktop, Colima, or another Docker daemon
  must support that platform.

## Prerequisites

Install and verify:

```sh
git --version
python3 --version
docker version
docker compose version
cloudflared --version
```

Python 3.12 is recommended for the offline validation proof. If `python3` is a
newer interpreter, create the virtual environment with an explicit 3.12
interpreter such as `python3.12`; the full development suite still runs on
newer 3.12+ interpreters.

Docker Desktop on macOS must share the directory that contains the Resource
checkout and state root. A checkout or state directory under `$HOME` is shared
by default. With Colima, verify that the same host paths are mounted into the
Docker VM.

## Platform Conventions

| Operation | Windows PowerShell | macOS or Linux |
| --- | --- | --- |
| Virtual environment | `.\.venv\Scripts\Activate.ps1` | `source .venv/bin/activate` |
| Python executable | `.\.venv\Scripts\python.exe` | `.venv/bin/python` |
| CLI executable | `.\.venv\Scripts\aep.exe` | `.venv/bin/aep` |
| Create directory | `New-Item -ItemType Directory -Force` | `mkdir -p` |
| Copy a file | `Copy-Item` | `cp` |
| Write a literal marker | `Set-Content -NoNewline` | `printf %s` or `touch` |
| HTTP request | `Invoke-RestMethod` | `curl -fsS` |
| List files | `Get-ChildItem` | `ls` or `find` |
| Elevated process | Administrator PowerShell | `sudo` where required |
| Docker socket | `/var/run/docker.sock` | `/var/run/docker.sock` |
| CRLF checkout | `AEP_RESOURCE_GIT_AUTOCRLF=true` | `AEP_RESOURCE_GIT_AUTOCRLF=false` |

Run repository commands from the repository root. Run only the Cloudflare
connector with elevated privileges when the tunnel token or port binding
requires it.

## Prepare The Host And Pinned Inputs

Create a detached, read-only Resource checkout at the release commit. Do not run
the control plane from a branch checkout.

```sh
export AEP_HOST_ROOT="$HOME/aep"
export AEP_RESOURCE_CHECKOUT="$AEP_HOST_ROOT/resources/agent-engineering-platform"
export AEP_STATE_DIRECTORY="$AEP_HOST_ROOT/state/agent-engineering-platform"

mkdir -p "$AEP_HOST_ROOT/resources" "$AEP_STATE_DIRECTORY/control"
git clone --filter=blob:none \
  https://github.com/yijiazho/agent-engineering-platform \
  "$AEP_RESOURCE_CHECKOUT"
git -C "$AEP_RESOURCE_CHECKOUT" checkout --detach \
  'replace-with-40-character-release-commit'
cp deploy/self-hosting/.env.example deploy/self-hosting/.env
```

Set these values in `deploy/self-hosting/.env`:

```text
AEP_IMAGE_REPOSITORY=ghcr.io/yijiazho/agent-engineering-platform
AEP_IMAGE_DIGEST=<registry-reported-64-character-digest>
AEP_RESOURCE_REVISION=<40-character-release-commit>
AEP_RESOURCE_CHECKOUT=/Users/<you>/aep/resources/agent-engineering-platform
AEP_STATE_DIRECTORY=/Users/<you>/aep/state/agent-engineering-platform
AEP_DOCKER_SOCKET=/var/run/docker.sock
AEP_RESOURCE_GIT_AUTOCRLF=false
```

Set `AEP_RESOURCE_REVISION` to `git -C "$AEP_RESOURCE_CHECKOUT" rev-parse HEAD`.
The checkout and state directory must be non-overlapping. The image is pulled by
digest, runs with a read-only root filesystem, and mounts the Resource checkout
read-only. Startup fails if the checkout is attached to a branch, dirty, or at a
different revision.

## Configure Secrets

Create the three operator-managed files outside Git:

```sh
mkdir -p deploy/self-hosting/secrets
chmod 700 deploy/self-hosting/secrets
printf %s 'replace-with-a-random-webhook-secret' \
  > deploy/self-hosting/secrets/github-webhook-secret.txt
cp /secure/location/github-app-private-key.pem \
  deploy/self-hosting/secrets/github-app-private-key.pem
printf %s 'replace-with-openai-api-key' \
  > deploy/self-hosting/secrets/openai-api-key.txt
chmod 600 deploy/self-hosting/secrets/*
```

Follow [GitHub App Provider Operations](github-app.md) for App permissions and
[OpenAI Model Provider Operations](openai-model-provider.md) for the model
credential. Do not commit secret values. The webhook secret is mounted only into
Event Controller, the App key only into Workflow Runtime and Tool Runtime, and
the model key only into Workflow Runtime and Agent Resolver.

## Validate Without Credentials

Create the development environment and run the platform suite:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev,yaml]"
python -m pytest
```

The offline execution proof requires CPython 3.12 on macOS arm64/x86_64, Linux x86_64,
or Windows amd64. It skips with an explicit reason on other interpreters or
platforms. To exercise it on macOS:

```sh
python3.12 -m venv .venv312
. .venv312/bin/activate
python -m pip install -e ".[dev,yaml]"
python -m pytest tests/test_self_hosting_resource_bundle.py
```

Run the Docker validation gate when the validation image, wheelhouse,
RunValidation Task, or Docker Tool changes:

```sh
python deploy/validation/verify.py verify
```

That gate is Linux/amd64. It is authoritative even when the host shell is macOS.
The local pytest suite is not a substitute for the published-image gate.

## Start And Verify The Stack

```sh
docker compose --env-file deploy/self-hosting/.env \
  -f deploy/self-hosting/compose.yaml config --quiet
docker compose --env-file deploy/self-hosting/.env \
  -f deploy/self-hosting/compose.yaml pull
docker compose --env-file deploy/self-hosting/.env \
  -f deploy/self-hosting/compose.yaml up -d
docker compose --env-file deploy/self-hosting/.env \
  -f deploy/self-hosting/compose.yaml ps
```

Verify one repository and Workspace identity:

```sh
curl -fsS http://127.0.0.1:8081/healthz | python3 -m json.tool
curl -fsS http://127.0.0.1:8082/v1/resources | python3 -m json.tool
```

Expected observations:

* every service is `ready`;
* repository is `github:yijiazho/agent-engineering-platform`;
* workspace is `agent-engineering-platform:1.0.0`;
* the Resource Controller resolves the Workspace, Event, Workflow, six Tasks,
  four Agents, and every referenced Prompt, Model, Tool, Policy, Evaluation, and
  KnowledgeBase.

Provider readiness:

```sh
docker compose --env-file deploy/self-hosting/.env \
  -f deploy/self-hosting/compose.yaml exec -T workflow-runtime \
  python -c "import json,os; from aep.github_app_provider import github_app_provider_from_environment; print(json.dumps(github_app_provider_from_environment(os.environ).readiness(),sort_keys=True))"
docker compose --env-file deploy/self-hosting/.env \
  -f deploy/self-hosting/compose.yaml exec -T workflow-runtime \
  /usr/local/bin/python -m aep.github_app_provider askpass-readiness
docker compose --env-file deploy/self-hosting/.env \
  -f deploy/self-hosting/compose.yaml exec -T workflow-runtime \
  python -c "import json,os; from aep.openai_model_provider import openai_model_adapter_from_environment; print(json.dumps(openai_model_adapter_from_environment('openai',environ=os.environ).readiness(),sort_keys=True))"
docker compose --env-file deploy/self-hosting/.env \
  -f deploy/self-hosting/compose.yaml exec -T workflow-runtime \
  docker version --format '{{.Server.Version}}'
```

Never print the PEM, API key, webhook secret, JWTs, installation tokens, prompt
bodies, or artifact bodies while diagnosing readiness.

## Ingress

Point the Cloudflare tunnel at the loopback Event Controller. Keep the public
hostname on HTTPS and preserve the GitHub `X-Hub-Signature-256`,
`X-GitHub-Event`, and `X-GitHub-Delivery` headers.

```sh
export AEP_PUBLIC_WEBHOOK_URL="https://<public-host>/v1/webhooks/github"
```

```sh
cloudflared tunnel run --token-file "$HOME/.cloudflared/aep-token"
```

An unsigned request must reach AEP and fail there with `401`:

```sh
curl --silent --show-error --output /dev/null --write-out '%{http_code}\n' \
  -X POST \
  -H 'Content-Type: application/json' \
  -H 'X-GitHub-Event: issues' \
  -H 'X-GitHub-Delivery: aep-public-preflight' \
  --data-binary '{}' \
  "$AEP_PUBLIC_WEBHOOK_URL"
```

A tunnel timeout, `502`, or connection refusal is not an AEP authentication
result.

## Emergency Disable And Recovery

Create the durable marker:

```sh
touch "$AEP_STATE_DIRECTORY/control/EMERGENCY_DISABLE"
```

Admission must return `503` with `emergency_disabled`, and no new Event or
outbox row may appear. Re-enable only after the blocking condition is resolved:

```sh
rm "$AEP_STATE_DIRECTORY/control/EMERGENCY_DISABLE"
```

Health must return to `ready` and a controlled signed delivery must be accepted.

## Inspect Evidence

Use the installed CLI against the durable checkpoint:

```sh
.venv/bin/aep --state-file "$AEP_STATE_DIRECTORY/runtime/objects.json" executions list
.venv/bin/aep --state-file "$AEP_STATE_DIRECTORY/runtime/objects.json" executions show <execution-id>
.venv/bin/aep --state-file "$AEP_STATE_DIRECTORY/runtime/objects.json" explain <execution-id>
```

Inspect the webhook outbox directly with the container Python, not by printing
raw request bodies:

```sh
docker compose --env-file deploy/self-hosting/.env \
  -f deploy/self-hosting/compose.yaml exec -T event-controller \
  python -c "import sqlite3; c=sqlite3.connect('/var/lib/aep/shared/github-webhook.sqlite3'); print({'events':c.execute('select count(*) from github_webhook_events').fetchone()[0],'outbox':c.execute('select status,count(*) from reconciliation_outbox group by status').fetchall(),'failures':c.execute('select count(*) from reconciliation_failures').fetchone()[0]})"
```

List durable evidence locations:

```sh
find "$AEP_STATE_DIRECTORY/runtime" -maxdepth 1 -type f
find "$AEP_STATE_DIRECTORY/artifacts/objects" -maxdepth 1 -type f | head
find "$AEP_STATE_DIRECTORY/execution-worktrees" -maxdepth 2 -type d | head
```

## Backup, Rotation, And Removal

Back up the state directory while no reconciliation worker is mutating it. A
simple offline copy is:

```sh
docker compose --env-file deploy/self-hosting/.env \
  -f deploy/self-hosting/compose.yaml down
tar -czf "$HOME/aep-backup-$(date +%Y%m%dT%H%M%S).tgz" \
  -C "$(dirname "$AEP_STATE_DIRECTORY")" "$(basename "$AEP_STATE_DIRECTORY")"
docker compose --env-file deploy/self-hosting/.env \
  -f deploy/self-hosting/compose.yaml up -d
```

Rotate the GitHub App private key or OpenAI key by atomically replacing the
mounted secret file, restarting the consuming services, and requiring readiness
to resolve the same identity before deleting the old credential at the provider.
See the main runbook for the full rotation sequence.

Stop and remove the stack without deleting recoverable state:

```sh
docker compose --env-file deploy/self-hosting/.env \
  -f deploy/self-hosting/compose.yaml down
```

Add `--volumes` only when intentionally discarding the local Compose volume.
The durable host state remains under `AEP_STATE_DIRECTORY`; remove it separately
after preserving required evidence.

## Differences From The Windows Profile

* Paths are POSIX absolute paths under `$HOME`; do not use `C:\` examples.
* `AEP_RESOURCE_GIT_AUTOCRLF` is normally `false`. Set it to `true` only for a
  checkout created with Windows Git CRLF normalization.
* Secret files use `chmod 600`; the secrets directory uses `chmod 700`.
* The Cloudflare connector may run as a foreground process or a user
  `launchd`/systemd unit. Do not install it as a system daemon without
  protecting the token file.
* Docker Desktop on macOS uses a Linux VM. Keep the Resource checkout and state
  directory inside a shared host path.
* The published validation image and its Docker gate remain Linux/amd64.

## Related Documents

* [Self-hosting dogfood runbook](self-hosting-dogfood.md)
* [GitHub App provider operations](github-app.md)
* [OpenAI model provider operations](openai-model-provider.md)
* [Validation image runbook](../../deploy/validation/README.md)
