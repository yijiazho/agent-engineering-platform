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
host access and allow only the Tool Runtime container to mount it.

Create a detached, read-only Resource checkout at the release commit. Do not
run the control plane from a branch checkout.

```powershell
git clone --filter=blob:none https://github.com/yijiazho/agent-engineering-platform C:\aep\resources\agent-engineering-platform
git -C C:\aep\resources\agent-engineering-platform checkout --detach <40-character-release-commit>
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
outbox identity for the replayed delivery.

Before a live publication, run a blocked path using a denied
`github.create_pr` policy or intentionally failing validation. Persist the
failure and confirm that GitHub has neither an `aep/execution/` branch nor a
pull request for it. Repository mismatch, stale base revision, failed build or
test, incomplete acceptance evidence, and the emergency marker are all
fail-closed publication conditions.

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

```powershell
docker compose --env-file deploy/self-hosting/.env -f deploy/self-hosting/compose.yaml ps
docker compose --env-file deploy/self-hosting/.env -f deploy/self-hosting/compose.yaml logs --since 30m
Get-ChildItem C:\aep\state\agent-engineering-platform\execution-worktrees
```

Never paste unrestricted logs into an issue. Export only structured evidence
after checking that it contains no secret or artifact body. A dirty retained
worktree is evidence: inspect it before cleanup. An ambiguous GitHub mutation
must be reconciled by owner/head/base before any retry.

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
