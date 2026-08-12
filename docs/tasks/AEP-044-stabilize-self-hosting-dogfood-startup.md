# AEP-044: Stabilize Self-Hosting Dogfood Startup

**Status:** Completed

## Context

The digest-pinned self-hosting profile can pull and create all seven containers,
but one or more services may enter a restart loop before their HTTP listeners
remain available. The externally observed symptom is an ingress failure such as
`connect ECONNREFUSED 127.0.0.1:8081`; the Compose process list may concurrently
show Event Controller, Workflow Runtime, Agent Resolver, or Tool Runtime
restarting.

`src/aep/dogfood_deployment.py` is a suspected startup boundary because it
validates every service before dispatching to the service adapter, but this task
must not assume that it is the only source of failure. Diagnose the first causal
error from container output and continue iterating across deployment validation,
Compose configuration, Windows bind mounts and paths, provider construction,
service startup, and runtime wiring until no blocking issue remains. Do not
weaken immutable-revision, clean-checkout, secret isolation, provider binding,
emergency-disable, read-only filesystem, or publication-policy safeguards merely
to make the containers start.

The Compose inputs are configured in `deploy/self-hosting/.env`. Secret values
remain in the files referenced by that environment file and must not be printed,
copied into task evidence, committed, or embedded in tests. The Cloudflare
connector is a separately managed Windows service/process and requires an
elevated PowerShell session on this host.

## Deliverable

Identify and resolve every blocking defect that prevents the published
self-hosting image from running reliably by:

* establishing a repeatable, evidence-driven debugging loop that records the
  first startup or runtime failure for each restarting or unhealthy service;
* adding a focused regression test for each confirmed defect before or with its
  fix, including interactions that unit-only environment dictionaries currently
  fail to represent;
* correcting the narrowest responsible implementation in
  `src/aep/dogfood_deployment.py`, the deployment/runtime adapters, Compose
  configuration, or related tests and documentation without relaxing security
  and identity invariants;
* rebuilding and publishing a uniquely tagged candidate image after code
  changes, pinning the registry-reported digest and exact Resource commit in
  `deploy/self-hosting/.env`, and testing the pulled digest rather than relying
  on an unpinned local tag;
* repeating cold start, restart, health, Resource discovery, authenticated
  webhook replay, and Cloudflare ingress checks until the complete stack remains
  stable; and
* updating the self-hosting runbook with any corrected setup, diagnostic,
  recovery, or verification commands discovered during implementation.

Use the following PowerShell sequence as the reproducible operator loop. Run it
from the repository root unless a command says otherwise. Replace placeholders
without recording tokens or secret contents in command output.

```powershell
# Establish the candidate source and Resource identity.
git status --short
git rev-parse HEAD
git -C C:\aep\resources\agent-engineering-platform rev-parse HEAD
git -C C:\aep\resources\agent-engineering-platform status --short

# Run focused checks before building a candidate image.
.\.venv\Scripts\python.exe -m pytest tests/test_dogfood_deployment.py
.\.venv\Scripts\python.exe -m pytest tests/test_github_webhook.py tests/test_execution_checkout.py

# Build and publish a uniquely tagged candidate with the shared image definition.
docker build -f .\deploy\local\Dockerfile -t ghcr.io/yijiazho/agent-engineering-platform:<candidate-tag> .
docker push ghcr.io/yijiazho/agent-engineering-platform:<candidate-tag>
docker image inspect ghcr.io/yijiazho/agent-engineering-platform:<candidate-tag> --format '{{index .RepoDigests 0}}'

# Put the registry digest without "sha256:" in AEP_IMAGE_DIGEST and the exact
# detached Resource commit in AEP_RESOURCE_REVISION in deploy/self-hosting/.env.
docker compose --env-file .\deploy\self-hosting\.env -f .\deploy\self-hosting\compose.yaml config --quiet
docker compose --env-file .\deploy\self-hosting\.env -f .\deploy\self-hosting\compose.yaml pull
docker compose --env-file .\deploy\self-hosting\.env -f .\deploy\self-hosting\compose.yaml up -d

# Reproduce and capture the first causal failures without exposing secrets.
docker compose --env-file .\deploy\self-hosting\.env -f .\deploy\self-hosting\compose.yaml ps
docker compose --env-file .\deploy\self-hosting\.env -f .\deploy\self-hosting\compose.yaml logs --no-color --tail=200 event-controller
docker compose --env-file .\deploy\self-hosting\.env -f .\deploy\self-hosting\compose.yaml logs --no-color --tail=200 workflow-runtime
docker compose --env-file .\deploy\self-hosting\.env -f .\deploy\self-hosting\compose.yaml logs --no-color --tail=200 agent-resolver
docker compose --env-file .\deploy\self-hosting\.env -f .\deploy\self-hosting\compose.yaml logs --no-color --tail=200 tool-runtime
docker compose --env-file .\deploy\self-hosting\.env -f .\deploy\self-hosting\compose.yaml logs --no-color --tail=200
docker inspect self-hosting-event-controller-1 --format '{{json .State}}'
docker inspect self-hosting-workflow-runtime-1 --format '{{json .State}}'

# Verify local readiness once the restart blockers are removed.
Test-NetConnection -ComputerName 127.0.0.1 -Port 8081
Invoke-RestMethod http://127.0.0.1:8081/healthz
Invoke-RestMethod http://127.0.0.1:8082/v1/resources

# Exercise process restart and cold recreation while preserving durable state.
docker compose --env-file .\deploy\self-hosting\.env -f .\deploy\self-hosting\compose.yaml restart
docker compose --env-file .\deploy\self-hosting\.env -f .\deploy\self-hosting\compose.yaml ps
docker compose --env-file .\deploy\self-hosting\.env -f .\deploy\self-hosting\compose.yaml down
docker compose --env-file .\deploy\self-hosting\.env -f .\deploy\self-hosting\compose.yaml up -d --force-recreate
docker compose --env-file .\deploy\self-hosting\.env -f .\deploy\self-hosting\compose.yaml ps
```

Run the Cloudflare connector from an **Administrator PowerShell** and keep that
session running while testing public ingress:

```powershell
cloudflared tunnel run --token-file C:\ProgramData\cloudflared\token
```

Then exercise the configured public hostname and webhook route. An unsigned or
method-invalid request may receive an intentional AEP `4xx`; it must reach AEP
rather than fail with a Cloudflare origin error, timeout, or connection refusal.
Run this from a second PowerShell session while the connector remains active:

```powershell
curl.exe -i https://<public-hostname>/v1/webhooks/github
```

Use the signed fixture procedure from
`docs/operations/self-hosting-dogfood.md` for the accepted and duplicate replay
checks.

After each confirmed fix, rerun the focused regression test, publish a new
candidate digest, recreate the stack, and repeat the diagnostic sequence. Do
not declare the task complete after fixing only the first exception: inspect
all seven services after cold start and after webhook reconciliation for the
next blocking failure.

## Dependencies

* AEP-035
* AEP-038
* AEP-039
* AEP-040
* AEP-041
* AEP-042

## Acceptance Criteria

* `docker compose config --quiet`, `pull`, and `up -d` succeed using only the
  inputs referenced by `deploy/self-hosting/.env`, a published digest-pinned
  image, and an exact detached Resource revision.
* All seven self-hosting containers reach `healthy`, remain running without
  restart-loop growth through at least five minutes and one authenticated
  webhook reconciliation cycle, and recover cleanly after a deliberate Compose
  restart.
* `http://127.0.0.1:8081/healthz` and
  `http://127.0.0.1:8082/v1/resources` succeed and report the configured
  repository, Workspace, dogfood environment, and Resource revision.
* With `cloudflared tunnel run --token-file
  C:\ProgramData\cloudflared\token` running from Administrator PowerShell, the
  configured public webhook hostname reaches the Event Controller without
  `ECONNREFUSED`, timeout, Cloudflare `502`, or origin-unavailable errors.
* A correctly signed fixture delivery returns `202 accepted`; replaying its
  delivery ID returns `200 duplicate` with the same Event identity, and neither
  delivery causes any service to restart.
* Every confirmed blocker has a deterministic regression test that fails on the
  prior behavior and passes after the fix. Targeted deployment, webhook,
  checkout, provider, and runtime tests pass, followed by the complete
  `python -m pytest` suite.
* Logs, exceptions, health responses, test fixtures, and operator evidence do
  not expose the Cloudflare token, webhook secret, GitHub App private key,
  installation tokens, OpenAI key, request signature, or request body.
* The final implementation preserves the pinned-image, detached clean Resource
  checkout, repository/Workspace binding, service-scoped secrets, read-only
  filesystem, durable-state separation, emergency-disable, and policy-gated
  publication guarantees.
* `README.md`, `docs/operations/self-hosting-dogfood.md`, deployment examples,
  and task/execution-plan status remain synchronized with any corrected
  behavior or commands.

## Implementation Evidence

Completed on 2026-08-12 against Resource revision
`a5653fe45529a00b205cf114bca6b3f5c3c3f91b` and published candidate
`ghcr.io/yijiazho/agent-engineering-platform@sha256:bb4ee0fd8fbcc6a3d09bf3d766fe352d3e073cb2ce97d3b1b0c3f6f1b23d7e39`.
The confirmed blockers were Windows CRLF normalization during Linux-container
checkout verification, concurrent read-only Git probe contention and timeout,
the Workflow Runtime's second verification omitting the same normalization,
silent recoverable reconciliation failures, and installed-package runtime
schema lookup outside the image's copied schema bundle.

The final pulled digest cold-started all seven services healthy with zero
restart growth. A signed fixture delivery returned `202 accepted`, its replay
returned `200 duplicate` with the same Event identity, the durable
reconciliation reached a terminal WorkflowExecution and retired its outbox row,
and no service restarted. A deliberate Compose restart recovered all seven
services. Local health and Resource discovery reported the pinned repository,
Workspace, `dogfood` environment, Resource revision, and 35 Resources. Public
Cloudflare ingress reached AEP and returned its intentional `404` for an
unsigned GET rather than an origin error.
