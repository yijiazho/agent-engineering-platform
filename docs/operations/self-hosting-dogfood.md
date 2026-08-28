# Self-Hosting Dogfood Operations

This runbook operates one AEP deployment bound only to
`github:yijiazho/agent-engineering-platform` and Workspace
`agent-engineering-platform:1.0.0`. The running image and Resource checkout are
immutable. Generated changes use revision-bound worktrees and may create one
unmerged pull request; AEP has no merge or deployment capability.

## Prepare The Host And Pinned Inputs

Use a dedicated host with Docker Compose v2, Git, HTTPS ingress, enough disk for
the repository cache plus concurrent validation worktrees, and a backup target
outside the AEP state directory. Protect the Docker socket as root-equivalent
host access. Workflow Runtime mounts it for the monolithic MVP reconciliation
worker's Docker validation boundary; Tool Runtime mounts it for its separately
addressable service responsibility. No other service receives the socket.

Create a detached, read-only Resource checkout at the release commit. Do not
run the control plane from a branch checkout.

```powershell
git clone --filter=blob:none https://github.com/yijiazho/agent-engineering-platform C:\aep\resources\agent-engineering-platform
git -C C:\aep\resources\agent-engineering-platform checkout --detach 'replace-with-40-character-release-commit'
New-Item -ItemType Directory -Force C:\aep\state\agent-engineering-platform\control
Copy-Item deploy/self-hosting/.env.example deploy/self-hosting/.env
```

Set `AEP_RESOURCE_REVISION` to `git rev-parse HEAD`. Set
`AEP_IMAGE_REPOSITORY` and the registry-reported 64-character
`AEP_IMAGE_DIGEST`; tags such as `latest` are not accepted as identity. Keep
the Resource checkout and state directory non-overlapping. The image is pulled
by digest, runs with a read-only root filesystem, and mounts the pinned Resource
checkout read-only. Durable ingress, checkout ownership, source cache,
worktrees, evidence, and credential leases live under `AEP_STATE_DIRECTORY`.
Startup verifies the checkout is detached and completely clean before loading
`.ai/`; a modified or untracked file fails every service. The deployment also
passes `AEP_STATE_DIRECTORY` as the host-visible Docker state root so nested
validation binds the execution worktree rather than a container-only path.
For Docker Desktop's Linux-container backend, keep `AEP_DOCKER_SOCKET` set to
`/var/run/docker.sock`. Docker Compose resolves that path through the Linux VM
and mounts an actual Unix socket into Workflow Runtime and Tool Runtime. A
Windows named-pipe value such as `//./pipe/docker_engine` becomes a directory
inside a Linux container and makes nested Docker validation unavailable.
Because this profile mounts a checkout prepared by Windows Git into Linux
containers, it explicitly verifies cleanliness with `core.autocrlf=true`.
This matches the host's CRLF worktree normalization while still rejecting
content changes and untracked files. Verification also disables Git's optional
index locks and allows up to 60 seconds per bounded Git probe so all seven
read-only startup checks can safely run concurrently on the Windows bind mount.
Recreate the checkout with Windows Git if
it was prepared with a different line-ending policy; do not bypass the clean
checkout check.

## Configure GitHub And Secrets

Follow [GitHub App Provider Operations](github-app.md). Install the App on only
this repository with Metadata read, Issues read, Contents read/write, and Pull
requests read/write. Subscribe only to Issues and configure the HTTPS webhook
URL as `/v1/webhooks/github`. Do not grant Administration, Actions, Deployments,
Environments, or organization permissions.

Create three distinct non-empty files outside Git and reference them from
`deploy/self-hosting/.env`:

* a random webhook HMAC secret;
* the GitHub App PEM private key; and
* the OpenAI API key described by [OpenAI Model Provider Operations](openai-model-provider.md).

Restrict host ACLs to the deployment operator and container runtime. Compose
mounts the webhook secret only into Event Controller, the App key only into
Workflow Runtime and Tool Runtime, and the model key only into Workflow Runtime
and Agent Resolver. Installation tokens and Git askpass files remain ephemeral.

## Manual End-To-End Test Plan

Use this plan for the controlled AEP-043 pilot and for every replacement host or
release generation. Execute the tests in order. A failed observation is a stop
condition: preserve the state directory and relevant redacted logs, diagnose the
failure, publish a corrected immutable generation, and restart from the first
affected test. Do not skip directly to opening a live issue.

The plan separates read-only checks, isolated ingress checks, fail-closed checks,
and the one authorized live publication. Commands assume Windows PowerShell 5,
Docker Desktop, Git, GitHub CLI, and `cloudflared`. Run repository commands from
the repository root. Run only the Cloudflare connector command from an elevated
Administrator PowerShell.

Record the following in an operator change record without recording secret
values, webhook signatures, request bodies, prompts, or artifact bodies:

* operator, start/end time, host, and change or incident reference;
* image repository and digest, Resource revision, default-branch base SHA, and
  validation-image digest;
* GitHub App ID and installation ID, public webhook hostname, and GitHub
  delivery ID;
* test ID, action, timestamp, observed status, evidence address, and pass/fail;
* WorkflowExecution and trace IDs, execution branch, PR number/URL/head SHA,
  and terminal status; and
* any deviation, retry, provider ambiguity, retained worktree, or cleanup
  decision.

The test gates are:

| Test | Action | Required observation |
| --- | --- | --- |
| MTP-01 | Validate immutable inputs and host paths | Published digest exists; Resource checkout is detached, clean, and at the configured revision; secrets are non-empty without being printed. |
| MTP-02 | Run deterministic tests | Deployment, ingress, checkout, provider, and end-to-end harness tests pass without live credentials. |
| MTP-03 | Cold-start the self-hosting stack | All seven containers become healthy, retain zero restart-count growth, and report one consistent identity. |
| MTP-04 | Verify Resources and live provider boundaries | The complete six-Task graph resolves; GitHub App readiness is `READY`; Docker is reachable; OpenAI local configuration is ready. |
| MTP-05 | Test public ingress | The Cloudflare route reaches AEP and an unsigned request fails at AEP with `401`, not at the tunnel with a timeout, `502`, or connection refusal. |
| MTP-06 | Test authentication and replay in isolated state | First signed fixture is `202 accepted`, replay is `200 duplicate`, negative cases fail closed, and only one pending outbox identity exists. |
| MTP-07 | Test emergency disable | Admission returns `503 emergency_disabled`, no new Event/outbox row appears, and services recover after controlled re-enable. |
| MTP-08 | Establish the publication baseline | No pre-existing branch or PR can be mistaken for the pilot result; backup and capacity checks pass. |
| MTP-09 | Open one controlled issue | GitHub records one successful Issues delivery and AEP creates one WorkflowExecution at the live default-branch SHA. |
| MTP-10 | Observe the six-Task execution | The Tasks succeed in DAG order with correlated context, Agent/Model/Tool, artifact, Evaluation, and Policy evidence. |
| MTP-11 | Verify publication | Exactly one authorized execution branch and one open, unmerged PR exist with the required body and head/base identity. |
| MTP-12 | Verify restart and replay idempotency | Compose restart and GitHub redelivery create no second Event, WorkflowExecution, branch, mutation, or PR. |
| MTP-13 | Review evidence and security | Durable evidence is complete and correlated; logs and exported metadata contain no secret or artifact body. |

### MTP-01: Immutable Input And Host Preflight

Read the non-secret pins from `.env`, verify their format, and compare the
Resource pin with the detached checkout. The status command must print nothing,
and `symbolic-ref` must exit `1`, proving that the checkout is detached.

```powershell
$envLines = Get-Content .\deploy\self-hosting\.env
function Get-AepSetting([string]$name) {
    $line = $envLines | Where-Object { $_ -like "$name=*" } | Select-Object -First 1
    if (-not $line) { throw "$name is not configured" }
    return ($line -split '=', 2)[1].Trim()
}

$imageRepository = Get-AepSetting 'AEP_IMAGE_REPOSITORY'
$imageDigest = Get-AepSetting 'AEP_IMAGE_DIGEST'
$resourceRevision = Get-AepSetting 'AEP_RESOURCE_REVISION'
$resourceCheckout = Get-AepSetting 'AEP_RESOURCE_CHECKOUT'
$stateDirectory = Get-AepSetting 'AEP_STATE_DIRECTORY'

if ($imageDigest -notmatch '^[0-9a-f]{64}$') { throw 'invalid AEP_IMAGE_DIGEST' }
if ($resourceRevision -notmatch '^[0-9a-f]{40}$') { throw 'invalid AEP_RESOURCE_REVISION' }
if ((git -C $resourceCheckout rev-parse HEAD).Trim() -ne $resourceRevision) { throw 'Resource revision mismatch' }
if (git -C $resourceCheckout status --porcelain=v1 --untracked-files=all) { throw 'Resource checkout is dirty' }
git -C $resourceCheckout symbolic-ref -q HEAD
if ($LASTEXITCODE -ne 1) { throw 'Resource checkout is not detached' }
$resourcePath = (Resolve-Path $resourceCheckout).Path.TrimEnd('\')
$statePath = (Resolve-Path $stateDirectory).Path.TrimEnd('\')
if ($resourcePath.Equals($statePath, [StringComparison]::OrdinalIgnoreCase)) { throw 'Resource checkout equals state root' }
if ($resourcePath.StartsWith($statePath + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Resource checkout overlaps state' }
if ($statePath.StartsWith($resourcePath + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'State overlaps Resource checkout' }

docker manifest inspect "$imageRepository@sha256:$imageDigest" | Out-Null
python deploy/validation/verify.py published
docker compose --env-file .\deploy\self-hosting\.env -f .\deploy\self-hosting\compose.yaml config --quiet
```

Verify secret presence without reading their values. Relative secret paths are
resolved from `deploy/self-hosting`, matching Compose behavior.

```powershell
$selfHostingRoot = (Resolve-Path .\deploy\self-hosting).Path
'AEP_GITHUB_WEBHOOK_SECRET_FILE','AEP_GITHUB_APP_PRIVATE_KEY_FILE','AEP_OPENAI_API_KEY_FILE' | ForEach-Object {
    $configured = Get-AepSetting $_
    $path = if ([IO.Path]::IsPathRooted($configured)) { $configured } else { Join-Path $selfHostingRoot $configured }
    $item = Get-Item -LiteralPath $path -ErrorAction Stop
    if ($item.Length -le 0) { throw "$_ is empty" }
    [pscustomobject]@{ Name = $_; Exists = $true; NonEmpty = $true }
}
```

Pass only if the registry manifest and validation image resolve, its image
configuration identity matches the value captured during promotion, both credential-free
readiness probes pass by digest with networking disabled, the Compose
configuration is valid, the checkout is clean and detached at the pin, the two
host roots do not overlap, and all three results report `Exists=True` and
`NonEmpty=True`. Do not display the rendered secret contents or the private key.

### MTP-02: Credential-Free Regression Gate

From a clean release checkout, run the same Docker-capable Linux gate required
by CI:

```powershell
python deploy/validation/verify.py verify
```

This command rejects a dirty release checkout and contract drift before it
builds the reviewed Dockerfile from a Dockerfile-only context. It runs the exact
readiness, offline bootstrap, and complete test sequence in separate clean and
documentation-only dirty workspaces against both the fresh build and published
digest. It also proves the published digest has the configuration identity
captured during promotion and repeats the credential-free probes by digest.
For focused diagnosis, the host-side suites may also be run directly:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_mvp_harness.py tests/test_dogfood_deployment.py
.\.venv\Scripts\python.exe -m pytest tests/test_github_webhook.py tests/test_execution_checkout.py
.\.venv\Scripts\python.exe -m pytest tests/test_github_app_provider.py tests/test_openai_model_provider.py
.\.venv\Scripts\python.exe -m pytest tests/test_self_hosting_resource_bundle.py
.\.venv\Scripts\python.exe -m pytest
```

Pass only if the Docker gate exits `0`. The focused commands must also pass when
used for diagnosis, but they do not replace the Dockerfile-built Linux gate or
the live provider and pilot tests below. See
`deploy/validation/README.md` for the guarded publication procedure; never
update the Resource graph from a separately rebuilt or merely retagged image.
The Docker-capable CI workflow separately sets
`AEP_RUN_SERVICE_IMAGE_TESTS=1` and runs the focused service-image askpass test,
which builds `deploy/local/Dockerfile` and executes the credential-free
readiness command inside it. Run the same focused test locally before
publication when the service Dockerfile or askpass executable contract changes.

### MTP-03: Cold Start, Identity, And Stability

`down` must not use `--volumes`. Start from stopped containers while preserving
the explicit durable state directory:

```powershell
docker compose --env-file .\deploy\self-hosting\.env -f .\deploy\self-hosting\compose.yaml down
docker compose --env-file .\deploy\self-hosting\.env -f .\deploy\self-hosting\compose.yaml pull
docker compose --env-file .\deploy\self-hosting\.env -f .\deploy\self-hosting\compose.yaml up -d --force-recreate
Start-Sleep -Seconds 30
docker compose --env-file .\deploy\self-hosting\.env -f .\deploy\self-hosting\compose.yaml ps
docker inspect self-hosting-event-controller-1 self-hosting-resource-controller-1 self-hosting-workflow-runtime-1 self-hosting-agent-resolver-1 self-hosting-context-builder-1 self-hosting-tool-runtime-1 self-hosting-evaluation-engine-1 --format '{{.Name}} restarts={{.RestartCount}} status={{.State.Status}} health={{.State.Health.Status}}'
```

Query all service health endpoints and require identical identity fields:

```powershell
$health = 8081..8087 | ForEach-Object { Invoke-RestMethod "http://127.0.0.1:$_/healthz" }
$health | Select-Object service,status,repository,workspace,environment,resourceRevision | Format-Table -AutoSize
$identity = @($health | Group-Object repository,workspace,environment,resourceRevision)
if ($identity.Count -ne 1) { throw 'service identity drift' }
if (($health | Where-Object { $_.status -ne 'ready' }).Count -ne 0) { throw 'a service is not ready' }
```

Wait five minutes and rerun the `docker inspect` command:

```powershell
Start-Sleep -Seconds 300
docker inspect self-hosting-event-controller-1 self-hosting-resource-controller-1 self-hosting-workflow-runtime-1 self-hosting-agent-resolver-1 self-hosting-context-builder-1 self-hosting-tool-runtime-1 self-hosting-evaluation-engine-1 --format '{{.Name}} restarts={{.RestartCount}} status={{.State.Status}} health={{.State.Health.Status}}'
```

Pass only if all seven services remain `running/healthy`, restart counts do not
increase, every health response is `ready`, and the identity is
`github:yijiazho/agent-engineering-platform`,
`agent-engineering-platform:1.0.0`, `dogfood`, and the configured Resource
revision.

### MTP-04: Resource And Provider Readiness

Inspect the resolved Resource inventory without reading Resource bodies:

```powershell
$discovery = Invoke-RestMethod http://127.0.0.1:8082/v1/resources
$discovery.workspace
$discovery.resources | Group-Object { ($_ -split '/', 2)[0] } | Select-Object Name,Count | Sort-Object Name
```

Require one Workspace, one Event, one Workflow, six Tasks, four Agents, and all
versioned Prompt, Model, Tool, Policy, Evaluation, and KnowledgeBase references.
Confirm in `.ai/workflows/issue-to-pr.yaml` that the resolved DAG is:

```text
analyze-issue
  -> build-implementation-plan
  -> generate-patch
  -> run-validation
  -> evaluate-acceptance
  -> create-pull-request
```

Run credential-free readiness output from the live containers. GitHub readiness
performs the live installation lookup; OpenAI readiness proves only local URL
and key-file configuration, while a successful ModelInvocation in MTP-10 proves
the live model call. The shared image installs Debian's `docker-cli` package,
not the Docker daemon package: Workflow Runtime uses that client with the
host-mounted Docker socket. The image build verifies that the client binary is
available before publication.

```powershell
docker compose --env-file .\deploy\self-hosting\.env -f .\deploy\self-hosting\compose.yaml exec -T workflow-runtime python -c "import json,os; from aep.github_app_provider import github_app_provider_from_environment; print(json.dumps(github_app_provider_from_environment(os.environ).readiness(),sort_keys=True))"
docker compose --env-file .\deploy\self-hosting\.env -f .\deploy\self-hosting\compose.yaml exec -T workflow-runtime /usr/local/bin/python -m aep.github_app_provider askpass-readiness
docker compose --env-file .\deploy\self-hosting\.env -f .\deploy\self-hosting\compose.yaml exec -T workflow-runtime python -c "import json,os; from aep.openai_model_provider import openai_model_adapter_from_environment; print(json.dumps(openai_model_adapter_from_environment('openai',environ=os.environ).readiness(),sort_keys=True))"
docker compose --env-file .\deploy\self-hosting\.env -f .\deploy\self-hosting\compose.yaml exec -T workflow-runtime docker version --format '{{.Server.Version}}'
```

Pass only if GitHub reports `READY` for the configured repository, App,
installation, default branch, and authorized branch prefix; OpenAI reports
`READY` with the expected API URL and no key; and the nested Docker client
reports a server version. Askpass readiness must report `READY` with
`/usr/local/bin/python3.12`; `env python3`, a relative path, or any inherited
`PATH` dependency fails the generation. In the GitHub App UI, independently verify that it is
installed only on this repository with Metadata read, Issues read, Contents
read/write, Pull requests read/write, and only the Issues webhook subscription.
Verify that repository rules permit the App to push `aep/execution/*` branches.

### MTP-05: Cloudflare Public Ingress

From Administrator PowerShell, start the configured connector and keep it
running:

```powershell
cloudflared tunnel run --token-file C:\ProgramData\cloudflared\token
```

From a second, non-elevated PowerShell, send an intentionally unsigned request
to the exact configured public path:

```powershell
$publicWebhookUrl = 'https://<public-hostname>/v1/webhooks/github'
curl.exe --silent --show-error --output NUL --write-out "%{http_code}`n" -X POST -H "Content-Type: application/json" -H "X-GitHub-Event: issues" -H "X-GitHub-Delivery: aep-public-preflight" --data-binary "{}" $publicWebhookUrl
```

Pass only if the response is AEP `401 invalid_signature`. A Cloudflare `502`,
`1033`, timeout, TLS error, DNS failure, or `ECONNREFUSED` means ingress is not
ready. Confirm that only the webhook route is published and ports 8082-8087
remain bound to loopback.

### MTP-06: Isolated Authentication, Negative Cases, And Replay

Do not send the synthetic fixture to the live durable state while Workflow
Runtime is consuming it: a valid accepted Event is eligible for the complete
Workflow. Instead, stop the main stack and start only Event Controller under a
separate Compose project and isolated state directory:

```powershell
docker compose --env-file .\deploy\self-hosting\.env -f .\deploy\self-hosting\compose.yaml down
$env:AEP_STATE_DIRECTORY = 'C:/aep/state/agent-engineering-platform-ingress-test'
docker compose -p aep-ingress-test --env-file .\deploy\self-hosting\.env -f .\deploy\self-hosting\compose.yaml up -d event-controller
Start-Sleep -Seconds 20
docker compose -p aep-ingress-test --env-file .\deploy\self-hosting\.env -f .\deploy\self-hosting\compose.yaml ps
```

Run the signed fixture procedure in **Validate Authentication, Replay, And
Blocking** below against `http://127.0.0.1:8081/v1/webhooks/github`. Require the
first response to be `202 accepted`, the replay to be `200 duplicate`, and both
responses to contain the same Event ID. Then use the same prepared `$bodyBytes`,
`$headers`, and `$secret` variables for negative cases without printing them:

```powershell
function Invoke-WebhookStatus([hashtable]$requestHeaders, [byte[]]$requestBody) {
    try {
        $response = Invoke-WebRequest http://127.0.0.1:8081/v1/webhooks/github -Method Post -Headers $requestHeaders -ContentType application/json -Body $requestBody -UseBasicParsing
        return [int]$response.StatusCode
    } catch {
        if ($_.Exception.Response) { return [int]$_.Exception.Response.StatusCode }
        throw
    }
}

$badSignatureHeaders = $headers.Clone()
$badSignatureHeaders['X-GitHub-Delivery'] = 'aep-negative-bad-signature'
$badSignatureHeaders['X-Hub-Signature-256'] = 'sha256=' + ('0' * 64)
Invoke-WebhookStatus $badSignatureHeaders $bodyBytes

$wrongEventHeaders = $headers.Clone()
$wrongEventHeaders['X-GitHub-Delivery'] = 'aep-negative-wrong-event'
$wrongEventHeaders['X-GitHub-Event'] = 'push'
Invoke-WebhookStatus $wrongEventHeaders $bodyBytes

$oversizedBody = New-Object byte[] 1048577
$oversizedHmac = [Security.Cryptography.HMACSHA256]::new($secret)
$oversizedDigest = ([BitConverter]::ToString($oversizedHmac.ComputeHash($oversizedBody))).Replace('-', '').ToLowerInvariant()
$oversizedHeaders = @{
    'X-Hub-Signature-256' = "sha256=$oversizedDigest"
    'X-GitHub-Event' = 'issues'
    'X-GitHub-Delivery' = 'aep-negative-oversized'
}
Invoke-WebhookStatus $oversizedHeaders $oversizedBody
$oversizedHmac.Dispose()
```

Require `401`, `422`, and `413`, respectively. For repository mismatch and
unsupported action, alter only the fixture repository or `action`, reserialize
the body, recompute HMAC exactly as in the signed procedure, use a fresh
delivery ID, and require `422 repository_mismatch` or `422 unsupported_action`.

Inspect only outbox metadata, never `event_json` or `request_json`:

```powershell
docker compose -p aep-ingress-test --env-file .\deploy\self-hosting\.env -f .\deploy\self-hosting\compose.yaml exec -T event-controller python -c "import sqlite3; c=sqlite3.connect('/var/lib/aep/shared/github-webhook.sqlite3'); print({'events':c.execute('select count(*) from github_webhook_events').fetchone()[0],'outbox':c.execute('select status,count(*) from reconciliation_outbox group by status').fetchall(),'failures':c.execute('select count(*) from reconciliation_failures').fetchone()[0]})"
```

Pass only if the isolated database contains exactly one Event and one `PENDING`
outbox row from the accepted/replayed delivery; rejected requests add nothing.
Stop the isolated project, preserve its state as test evidence, clear the
process-level override, and restart the real stack:

```powershell
docker compose -p aep-ingress-test --env-file .\deploy\self-hosting\.env -f .\deploy\self-hosting\compose.yaml down
Remove-Item Env:AEP_STATE_DIRECTORY
docker compose --env-file .\deploy\self-hosting\.env -f .\deploy\self-hosting\compose.yaml up -d
```

### MTP-07: Emergency Disable

Count live admission rows before the test, create the exact durable marker, and
restart Event Controller:

```powershell
docker compose --env-file .\deploy\self-hosting\.env -f .\deploy\self-hosting\compose.yaml exec -T event-controller python -c "import sqlite3; c=sqlite3.connect('/var/lib/aep/shared/github-webhook.sqlite3'); print(c.execute('select count(*) from reconciliation_outbox').fetchone()[0])"
Set-Content -NoNewline C:\aep\state\agent-engineering-platform\control\EMERGENCY_DISABLE 'manual preflight'
docker compose --env-file .\deploy\self-hosting\.env -f .\deploy\self-hosting\compose.yaml restart event-controller
Start-Sleep -Seconds 20
curl.exe --silent --show-error --output NUL --write-out "%{http_code}`n" -X POST -H "Content-Type: application/json" -H "X-GitHub-Event: issues" -H "X-GitHub-Delivery: aep-disabled-preflight" --data-binary "{}" http://127.0.0.1:8081/v1/webhooks/github
```

Require HTTP `503` with code `emergency_disabled`, health status `disabled`,
and an unchanged outbox count. Confirm the exact target, remove only that marker,
restart Event Controller, and require `ready` health:

```powershell
$disableMarker = 'C:\aep\state\agent-engineering-platform\control\EMERGENCY_DISABLE'
if ((Resolve-Path $disableMarker).Path -ne $disableMarker) { throw 'unexpected disable marker target' }
Remove-Item -LiteralPath $disableMarker
docker compose --env-file .\deploy\self-hosting\.env -f .\deploy\self-hosting\compose.yaml restart event-controller
Start-Sleep -Seconds 20
Invoke-RestMethod http://127.0.0.1:8081/healthz
```

The deterministic suite in MTP-02 must also prove failed validation, policy
denial, repository mismatch, stale revision, and incomplete evidence prevent
publication. Do not edit the live pinned Resources to manufacture those cases.

### MTP-08: Publication Baseline, Backup, And Capacity

Record the live default-branch SHA and the pre-existing execution branches and
open PRs before opening the pilot issue:

```powershell
$baseSha = (git ls-remote https://github.com/yijiazho/agent-engineering-platform.git refs/heads/main).Split()[0]
$baseSha
gh api --paginate repos/yijiazho/agent-engineering-platform/git/matching-refs/heads/aep/execution/ --jq '.[].ref'
gh pr list --repo yijiazho/agent-engineering-platform --state open --json number,url,headRefName,baseRefName,isDraft
Get-PSDrive -Name C | Select-Object Name,Used,Free
Get-ChildItem C:\aep\state\agent-engineering-platform\execution-worktrees -ErrorAction SilentlyContinue
```

Create the consistent pre-pilot backup described in **Backup And Recovery** and
record its location, image digest, Resource revision, and restore owner. Pass
only if the backup target and state directory have sufficient capacity, no
unexplained pending outbox row or retained dirty worktree exists, and the
operator can distinguish every existing branch/PR from the forthcoming pilot.

### MTP-09: Controlled GitHub Trigger

In GitHub, create exactly one narrow issue in
`yijiazho/agent-engineering-platform`. Add the dogfood label at creation time;
the runtime trigger is `issues/opened`, so labeling an already-open issue does
not trigger the MVP. State concrete allowed paths and acceptance criteria that
the repository's offline validation can satisfy. Record the issue number and
the delivery ID shown in the GitHub App delivery UI.

Observe the GitHub delivery and local ingress:

```powershell
docker compose --env-file .\deploy\self-hosting\.env -f .\deploy\self-hosting\compose.yaml logs --since 10m --no-color event-controller workflow-runtime
docker compose --env-file .\deploy\self-hosting\.env -f .\deploy\self-hosting\compose.yaml exec -T event-controller python -c "import sqlite3; c=sqlite3.connect('/var/lib/aep/shared/github-webhook.sqlite3'); print(c.execute('select event_id,status from reconciliation_outbox order by rowid desc limit 5').fetchall()); print(c.execute('select event_id,failure_class,message from reconciliation_failures order by failed_at desc limit 5').fetchall())"
```

Require GitHub HTTP `202`, one new Event/outbox identity, and no terminal
failure. The Workflow may briefly remain `PENDING` while reconciliation runs.
Do not open another issue if it stalls; diagnose the existing delivery.

### MTP-10: Runtime And Six-Task Evidence

Poll until the WorkflowExecution becomes terminal. The following audit prints
only runtime metadata, never prompts or artifact bodies:

```powershell
$runtimeAudit = @'
import collections
import json
from pathlib import Path
from aep.runtime_store import DurableJsonRuntimeObjectStore

path = Path('/var/lib/aep/runtime/objects.json')
payload = json.loads(path.read_text(encoding='utf-8'))
workflows = [value for value in payload['objects'].values() if value.get('kind') == 'WorkflowExecution']
workflow = max(workflows, key=lambda value: value.get('createdAt', ''))
store = DurableJsonRuntimeObjectStore(path)
related = store.list_by_workflow_execution(workflow['id'])
counts = collections.Counter((value.get('kind'), value.get('status', '')) for value in related)
tasks = [value for value in related if value.get('kind') == 'TaskExecution']
contexts = [value for value in related if value.get('kind') == 'ContextPackage']
prs = [value for value in related if value.get('kind') == 'GeneratedArtifact' and value.get('artifactType') == 'PULL_REQUEST_DESCRIPTION']
policies = [value for value in related if value.get('kind') == 'PolicyDecision']
print({'workflowExecutionId': workflow['id'], 'traceId': workflow.get('traceId'), 'status': workflow.get('status'), 'repositoryRevision': workflow.get('repositoryRevision')})
print({'kindStatusCounts': sorted((kind, status, count) for (kind, status), count in counts.items())})
print({'tasks': [(value.get('taskRef', {}).get('name'), value.get('attempt'), value.get('status'), value.get('workingBranch')) for value in tasks]})
print({'contexts': [(value.get('taskRef', {}).get('name'), value.get('tokenBudget'), value.get('tokenCount'), value.get('truncation'), value.get('tokenEstimate', {}).get('breakdown')) for value in contexts]})
print({'pullRequests': [(value.get('pullRequestNumber'), value.get('pullRequestUrl'), value.get('headRevision')) for value in prs]})
print({'policyDecisions': [{'id': value.get('id'), 'gate': value.get('gate'), 'action': value.get('action'), 'decision': value.get('decision'), 'reason': value.get('reason'), 'policyRefs': value.get('policyRefs'), 'repositoryRevision': value.get('repositoryRevision'), 'matchedRules': value.get('matchedRules'), 'evaluatedRule': value.get('evaluatedRule'), 'evidence': value.get('evidence'), 'generatedArtifactIds': value.get('generatedArtifactIds'), 'evaluationResultIds': value.get('evaluationResultIds')} for value in policies]})
'@
docker compose --env-file .\deploy\self-hosting\.env -f .\deploy\self-hosting\compose.yaml exec -T workflow-runtime python -c $runtimeAudit
```

Require one `SUCCEEDED` WorkflowExecution at `$baseSha` and exactly six
successful TaskExecutions in this order:

1. `analyze-issue`
2. `build-implementation-plan`
3. `generate-patch`
4. `run-validation`
5. `evaluate-acceptance`
6. `create-pull-request`

For the same WorkflowExecution and trace, require ContextPackages,
ResolvedAgents, AgentInvocations, successful ModelInvocations, ToolInvocations,
GeneratedArtifacts, EvaluationResults, and PolicyDecisions. Specifically verify:

* `analyze-issue:1.2.0` produces one ContextPackage at or below its declarative
  32,000 input-token budget, with category-only count evidence; the package
  must not fail near 121,421 tokens or contain the complete repository inventory;
* the bounded package includes repository evidence for the issue's allowed
  paths and retains revision/snapshot provenance and merged selection reasons;
* `aep-repository:1.1.0` uses explicit per-source limits, while Model
  `tokenLimit` remains the independent OpenAI `max_output_tokens` value;
* `default-reasoning:1.1.0` retains `tokenLimit: 32000`, uses one provider
  attempt per Task attempt, and declares request/token admission capacities;
* shared provider requests show paced admission evidence rather than an
  immediate burst, and throttles record normalized reason/scope, attempt,
  delay/source, valid `Retry-After`, and `retryEligibleAt` evidence;
* `AEP_STATE_ROOT/model-rate-limits` contains only safe hashed-scope deadline
  checkpoints, and restarting the AgentInvocation worker does not admit work
  before an unexpired checkpoint;
* issue analysis and implementation plan artifacts passed schema evaluation;
* patch provenance and changed paths match the plan and base revision;
* Docker build and test commands both completed successfully with networking
  disabled and the pinned validation image;
* acceptance evaluation used only same-revision successful evidence;
* Publication Policy used `publication-evidence:1.1.0`, reported all six
  canonical boolean evidence fields as true and `failures: []`, matched rule
  zero, and allowed the candidate before any Git mutation;
* separate `git.push` and `github.create_pr` capability decisions allowed the
  two external mutations; and
* the final PR artifact records provider request identity, PR URL/number, pushed
  head revision, and the Git/GitHub ToolInvocation IDs.

Any failed ModelInvocation, validation, Evaluation, PolicyDecision, push, or PR
mutation is a failed pilot until explained. An `UNKNOWN` provider mutation must
be reconciled by owner/head/base before any retry.

For an `UNKNOWN` Git push, also reconcile the persisted committed head against
the exact remote execution branch before permitting later PR work. Query
`git ls-remote --heads` and `gh pr list --head`. Absence, exact match, and
conflict are distinct outcomes: absence confirms no branch but creates no PR;
only the exact expected head may be reused; a conflicting head blocks the
execution and requires isolation. Preserve the original ToolInvocation as
`UNKNOWN`; do not rewrite it or repeat it. Inspect only structured operation,
repository, branch, failure class, mutation state, timing, exit status, and
redacted log address.

For a Publication Policy denial, classify safe metadata before retrying. A
non-empty `evidence.failures` is an evidence-integrity denial and must be fixed
at the named persisted identity or revision. Empty failures with empty
`matchedRules` and no `evaluatedRule` is a Resource/runtime contract mismatch;
verify the exact Policy, Task, and Workflow versions. Do not print artifact
bodies, webhook payloads, credentials, or command output during this audit.

For a provider failure, do not immediately reopen or relabel the issue. A
temporary token/request throttle may retry after `retryEligibleAt`. `quota`,
`billing`, `authentication`, `authorization`, `invalid_request`, and
`unsupported_model` require operator action and must not repeat the unchanged
request. A valid long `Retry-After` may intentionally defer MTP-10 beyond the
current invocation deadline.

### MTP-11: Branch And Pull Request Verification

Copy the execution branch and PR number from the safe runtime audit, then query
GitHub:

```powershell
$executionBranch = 'aep/execution/<execution-hash>'
$prNumber = 123 # Replace with the recorded pull-request number.
$matches = gh pr list --repo yijiazho/agent-engineering-platform --state open --head $executionBranch --json number,url,state,isDraft,headRefName,headRefOid,baseRefName,body | ConvertFrom-Json
if ($matches.Count -ne 1) { throw 'expected exactly one open PR for the execution branch' }
$matches | Select-Object number,url,state,isDraft,headRefName,headRefOid,baseRefName
gh pr view $prNumber --repo yijiazho/agent-engineering-platform --json number,url,state,isDraft,headRefName,headRefOid,baseRefName,body
git ls-remote --heads https://github.com/yijiazho/agent-engineering-platform.git "refs/heads/$executionBranch"
```

Pass only if there is exactly one `aep/execution/` branch for this execution and
one open, unmerged PR targeting `main`; its head SHA equals the pushed and
artifact-recorded revision. Manually verify that the PR body links the issue and
contains the implementation plan, changed-file summary, and build/test evidence.
Confirm that AEP did not merge the PR or deploy the generated code.

### MTP-12: Restart And Delivery Replay Idempotency

Record the Event, WorkflowExecution, branch, PR, and restart counts. Restart the
stack without deleting state:

```powershell
docker compose --env-file .\deploy\self-hosting\.env -f .\deploy\self-hosting\compose.yaml restart
Start-Sleep -Seconds 30
docker compose --env-file .\deploy\self-hosting\.env -f .\deploy\self-hosting\compose.yaml ps
docker inspect self-hosting-event-controller-1 self-hosting-resource-controller-1 self-hosting-workflow-runtime-1 self-hosting-agent-resolver-1 self-hosting-context-builder-1 self-hosting-tool-runtime-1 self-hosting-evaluation-engine-1 --format '{{.Name}} restarts={{.RestartCount}} status={{.State.Status}} health={{.State.Health.Status}}'
```

In the GitHub App delivery UI, redeliver the same recorded delivery. Require
HTTP `200 duplicate` with the original Event ID. Rerun the outbox query, runtime
audit, and PR query. Pass only if the outbox identity remains unique and
completed, the original terminal WorkflowExecution is unchanged, and no second
execution branch, push, GitHub mutation, or PR appears.

### MTP-13: Final Evidence And Security Review

Review container health, safe runtime metadata, content-addressed directories,
and checkout ownership:

```powershell
docker compose --env-file .\deploy\self-hosting\.env -f .\deploy\self-hosting\compose.yaml ps
Get-ChildItem C:\aep\state\agent-engineering-platform\runtime
Get-ChildItem C:\aep\state\agent-engineering-platform\artifacts\objects
Get-ChildItem C:\aep\state\agent-engineering-platform\docker-logs
Get-ChildItem C:\aep\state\agent-engineering-platform\git-logs
Get-ChildItem C:\aep\state\agent-engineering-platform\execution-worktrees
docker inspect self-hosting-event-controller-1 self-hosting-workflow-runtime-1 self-hosting-tool-runtime-1 --format '{{.Name}} readOnly={{.HostConfig.ReadonlyRootfs}} security={{json .HostConfig.SecurityOpt}} mounts={{range .Mounts}}{{.Destination}}:{{.RW}} {{end}}'
```

Pass only if runtime evidence is durable and correlated, generated writes are
confined to execution/state storage, the Resource checkout and image remain
unchanged, and secret mounts match service scope. Review only redacted lifecycle
metadata. Do not print or export unrestricted environment variables, secret
files, webhook headers, request bodies, prompts, model bodies, or artifact
bodies. Record the final PR and evidence addresses, leave the PR unmerged, and
have a second operator review the change record before marking AEP-043 complete.

## Install And Verify Readiness

Validate the rendered configuration before starting, then require all seven
containers to become healthy:

```powershell
docker compose --env-file deploy/self-hosting/.env -f deploy/self-hosting/compose.yaml config --quiet
docker compose --env-file deploy/self-hosting/.env -f deploy/self-hosting/compose.yaml pull
docker compose --env-file deploy/self-hosting/.env -f deploy/self-hosting/compose.yaml up -d
docker compose --env-file deploy/self-hosting/.env -f deploy/self-hosting/compose.yaml ps
Invoke-RestMethod http://127.0.0.1:8081/healthz
Invoke-RestMethod http://127.0.0.1:8082/v1/resources
```

Every health response must report the same repository, Workspace, environment
`dogfood`, and `resourceRevision`. Startup fails on a different Git HEAD,
Workspace, repository, image digest format, missing service-scoped secret, or
overlapping mutable and immutable storage. Confirm the Resource inventory
contains one Event, one Workflow, six Tasks, four Agents, and all referenced
Prompts, Models, Tools, Policies, Evaluations, and KnowledgeBases.

Workflow Runtime must also remain running after its first poll. It consumes the
shared outbox, resolves the live default-branch SHA through the bound GitHub
App, provisions the execution checkout, and invokes the six handlers through
the deterministic scheduler. Runtime checkpoints are stored under
`runtime/objects.json`; GeneratedArtifact bodies, Docker logs, and redacted Git
logs are stored under their respective content-addressed state directories.
The shared image keeps `/opt/aep/src` on `PYTHONPATH` so runtime validators load
the schemas copied to `/opt/aep/schemas`; removing that image binding causes
reconciliation to fail before the first WorkflowExecution checkpoint.

Verify the live GitHub installation from Workflow Runtime before enabling the
webhook. The result must name only this repository and report `READY`; it must
not contain credentials. Run the deterministic allowed and blocked harness
tests on the exact release commit as the final credential-free smoke test:

```powershell
python -m pytest tests/test_mvp_harness.py tests/test_dogfood_deployment.py
python -m pytest tests/test_github_webhook.py tests/test_execution_checkout.py
```

Terminate TLS at a trusted reverse proxy and forward only the GitHub webhook
path to port 8081. Keep ports 8082-8087 bound to loopback. Enable GitHub webhook
delivery only after readiness and backup checks pass.

## Validate Authentication, Replay, And Blocking

Deliver a signed copy of `fixtures/github/issue-created.json` with a fixed test
delivery ID after changing only its repository identity to the bound repository.
The first response must be `202 accepted`; an identical replay must be
`200 duplicate` with the same Event ID. A bad signature, another repository,
an action other than `opened`, and an oversized request must be rejected.
Inspect `shared/github-webhook.sqlite3` and confirm exactly one Event and one
outbox identity for the replayed delivery. The row may remain pending while the
workflow runs, but must become completed only after terminal runtime evidence.

The following Windows PowerShell 5-compatible procedure reads the configured
secret file without printing the secret, signature, or request body. Run it
from the repository root and use a new fixed delivery ID for each intentional
test. Do not enable transcript logging for this session.

```powershell
$envLines = Get-Content .\deploy\self-hosting\.env
$entry = $envLines | Where-Object { $_ -like 'AEP_GITHUB_WEBHOOK_SECRET_FILE=*' } | Select-Object -First 1
$configuredPath = ($entry -split '=', 2)[1]
$secretPath = if ([IO.Path]::IsPathRooted($configuredPath)) {
    $configuredPath
} else {
    Join-Path (Resolve-Path .\deploy\self-hosting).Path $configuredPath
}
$secret = [IO.File]::ReadAllBytes($secretPath)
while ($secret.Length -gt 0 -and $secret[-1] -in 10, 13) {
    $secret = $secret[0..($secret.Length - 2)]
}
$payload = Get-Content -Raw .\fixtures\github\issue-created.json | ConvertFrom-Json
$payload.repository.name = 'agent-engineering-platform'
$payload.repository.full_name = 'yijiazho/agent-engineering-platform'
$payload.repository.owner.login = 'yijiazho'
$payload.repository.html_url = 'https://github.com/yijiazho/agent-engineering-platform'
$payload.repository.url = 'https://api.github.com/repos/yijiazho/agent-engineering-platform'
$bodyBytes = [Text.Encoding]::UTF8.GetBytes(($payload | ConvertTo-Json -Depth 20 -Compress))
$hmac = [Security.Cryptography.HMACSHA256]::new($secret)
$digest = ([BitConverter]::ToString($hmac.ComputeHash($bodyBytes))).Replace('-', '').ToLowerInvariant()
$headers = @{
    'X-Hub-Signature-256' = "sha256=$digest"
    'X-GitHub-Event' = 'issues'
    'X-GitHub-Delivery' = 'replace-with-fixed-test-delivery-id'
}
$first = Invoke-RestMethod http://127.0.0.1:8081/v1/webhooks/github -Method Post -Headers $headers -ContentType application/json -Body $bodyBytes
$replay = Invoke-RestMethod http://127.0.0.1:8081/v1/webhooks/github -Method Post -Headers $headers -ContentType application/json -Body $bodyBytes
$first | Select-Object status, eventId
$replay | Select-Object status, eventId
$hmac.Dispose()
```

Before a live publication, run a blocked path using a denied
`github.create_pr` policy or intentionally failing validation. Persist the
failure and confirm that GitHub has neither an `aep/execution/` branch nor a
pull request for it. Repository mismatch, stale base revision, failed build or
test, incomplete acceptance evidence, and the emergency marker are all
fail-closed publication conditions.

If Git push evidence is `CONFIRMED` but the later GitHub Tool rejects local
publication evidence, classify it as a confirmed push followed by a local,
pre-provider failure. Reconcile the exact repository, execution branch, and
committed head against the remote before recovery. Do not automatically replay
the push, overwrite or delete the branch, or create a PR from mismatched
evidence. Retain the terminal execution and invocations until an explicitly
authorized operator decision; the issue #67 branch/head and absence of a PR
remain retained evidence.

## Run The Controlled Pilot

Use one labeled, narrowly scoped issue in this repository. Record its issue
number, GitHub delivery ID, default-branch base SHA, image digest, Resource
revision, and start time in the operator change record. Do not retry by opening
a second issue; GitHub may replay the same delivery safely.

Observe the execution until terminal. Exactly six TaskExecutions must run in
this order: AnalyzeIssue, BuildImplementationPlan, GeneratePatch,
RunValidation, EvaluateAcceptance, and CreatePullRequest. Require correlated
Event, WorkflowExecution, ContextPackages, ResolvedAgent/AgentInvocation and
ModelInvocation evidence, ToolInvocations, GeneratedArtifacts,
EvaluationResults, PolicyDecisions, trace ID, repository/base revision, and
final PR URL. Logs may contain content addresses and redacted request IDs, but
not webhook signatures, keys, tokens, prompts, artifact bodies, or model bodies.

The resulting GitHub query must find exactly one open, unmerged PR for the
execution branch. Its body must link the issue and include the implementation
plan, changed-file summary, and build/test evidence. Record the PR number, URL,
head SHA, terminal execution status, and evidence export address. Leave the PR
unmerged for human review. A pilot is not complete until this live record is
reviewed; repository tests cannot substitute for it.

## Monitor And Inspect

Monitor container health/restarts, webhook rejection rate, pending outbox age,
Workflow and Task terminal status, provider timeout/rate-limit failures, disk
capacity, checkout lease expiry, retained dirty worktrees, validation duration,
and publication decisions. Correlate by trace ID and WorkflowExecution ID.
Recoverable reconciliation warnings expose only the Event ID, failure class,
stable failure code, and exception type; provider messages and request data are
intentionally omitted. The consumer logs only the first occurrence of an
Event's current failure signature and logs again when that safe signature
changes. Repeated two-second retries do not repeat an identical warning. Use
each transition warning as the next diagnostic boundary while an outbox row
remains pending.

```powershell
docker compose --env-file deploy/self-hosting/.env -f deploy/self-hosting/compose.yaml ps
docker compose --env-file deploy/self-hosting/.env -f deploy/self-hosting/compose.yaml logs --since 30m
Get-ChildItem C:\aep\state\agent-engineering-platform\execution-worktrees
```

Never paste unrestricted logs into an issue. Export only structured evidence
after checking that it contains no secret or artifact body. A dirty retained
worktree is evidence: inspect it before cleanup. A helper `STARTUP` failure with
`NOT_ATTEMPTED` is safe to retry only after image correction. An ambiguous push
or GitHub mutation must be reconciled by owner/head/base and exact head before
any retry.

## Backup And Recovery

Back up before every upgrade and at an interval shorter than the acceptable
evidence-loss window. To obtain a consistent MVP snapshot, disable admission,
stop all containers, copy the complete state directory (including SQLite WAL
files, repository cache, worktrees, artifacts, and control state), record the
image digest and Resource revision, then restart. Encrypt and access-control the
backup; credential lease remnants must be treated as secrets.

Recovery uses a clean replacement host. Keep admission disabled, restore the
complete state directory to the same absolute path, restore the exact detached
Resource revision and digest-pinned image, start the services, and verify
identity/readiness before removing the marker. Reconcile pending or ambiguous
GitHub operations before resuming workers. Never restore only a SQLite database
without its associated artifacts and worktrees.

## Rotate Credentials

Rotate the webhook secret by updating GitHub and the mounted file in one
maintenance window; deliveries signed with the other value fail closed. Rotate
the GitHub App key with the overlap procedure in its provider guide, restart
the two consumers, verify the same installation, then revoke the old key.
Atomically replace the OpenAI key file, restart its consumers, and perform one
bounded provider check before revoking the old key. Secret rotation never
changes `.ai/` Resources or persisted evidence.

## Upgrade And Roll Back

For an upgrade, merge and release through the normal human-reviewed process.
Build and publish a new immutable image, resolve its digest, create a detached
Resource checkout at the matching release commit, back up state, update both
pins in `.env`, pull, and recreate. Do not modify the running checkout or image.
Require schema/identity checks and smoke validation before enabling admission.

For rollback, create a backup of the failed generation, restore the prior
image digest and matching Resource revision, and restore the compatible
pre-upgrade state snapshot when the state format changed. Recreate and verify
before admission. Never pair code from one release with Resources from another.

## Emergency Disable, Shutdown, And Removal

Create the durable marker first whenever publication safety is uncertain:

```powershell
Set-Content C:\aep\state\agent-engineering-platform\control\EMERGENCY_DISABLE "disabled by operator"
docker compose --env-file deploy/self-hosting/.env -f deploy/self-hosting/compose.yaml restart
```

The Event Controller then rejects new deliveries, health reports `disabled`,
and the CreatePullRequest publication guard rejects before commit and rechecks
before Git push and PR creation. Stop workers next, inspect in-flight evidence,
and revoke or suspend the GitHub App if immediate provider-level isolation is
required. Remove the marker only after the incident is explained and all
ambiguous operations are reconciled.

For planned shutdown, disable admission, wait for or explicitly cancel active
executions, back up, then run Compose `down` without deleting state. To remove
registration permanently, uninstall the GitHub App from this repository,
remove the GitHub webhook, revoke provider keys, stop the stack, archive the
evidence retention copy, and then remove the dedicated Resource checkout and
state directory under the organization's retention policy. Do not use
`down --volumes`; the deployment uses explicit host directories so deletion
must be an intentional, separately reviewed operation.
* `generate-patch:1.2.0` uses `code-generator:1.2.0` and requires one exact
  revision-bound `editable-target` per evaluated plan path. Inspect the
  persisted package for content addresses and preimage digests, and inspect the
  patch EvaluationResult for required-file dispositions and change statistics
  before authorizing publication.
