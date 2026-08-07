# AI Agent Engineering Platform

AI Agent Engineering Platform (AEP) is a declarative, event-driven control plane for software engineering automation.

The project explores a Kubernetes-inspired architecture where AI workflows, agents, prompts, models, tools, policies, evaluations, knowledge sources, and runtime evidence are managed through explicit contracts rather than ad hoc chat sessions.

The initial MVP focuses on a GitHub-centered engineering loop:

1. Receive a GitHub Issue event.
2. Normalize the event.
3. Resolve a declarative Workflow and Task DAG.
4. Build deterministic ContextPackages.
5. Resolve Agents into ResolvedAgent runtime objects.
6. Generate a plan and patch.
7. Run validation.
8. Evaluate outputs.
9. Apply policy.
10. Open a pull request.
11. Persist execution history and GeneratedArtifacts.

## Design Principles

* Declarative Resources define desired AI behavior.
* Runtime objects capture observed execution state.
* Workflows orchestrate; Agents reason.
* Agents never retrieve repository knowledge directly.
* Context construction is deterministic and provenance-rich.
* Model providers are represented by Model resources, not Tools.
* Tools are non-model external capabilities governed by policy.
* GeneratedArtifacts are runtime outputs, not declarative Resources.

## Repository Layout

```text
docs/
  prd.md
  execution-plan.md
  implementation-tasks.md
  adr/
  architecture/
  tasks/
schemas/
  resources/
  runtime/
fixtures/
  resources/
  runtime/
skills/
  implement-aep-feature/
  review-aep-pr/
src/
  aep/
tests/
```

## Local Development

Use a repository-local virtual environment for Python development. This isolates local test and tooling dependencies from the host interpreter and does not conflict with Docker or Kubernetes; containers remain the planned runtime and Tool execution isolation boundary.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,yaml]"
python -m pytest
```

`PyYAML` is optional for full YAML parsing in local `.ai/` resource files. JSON content in `.yaml` files works without that extra dependency.

Installed development package versions are captured in [requirements-dev.lock](requirements-dev.lock). Refresh it after dependency changes with `.\.venv\Scripts\python.exe -m pip list --format=freeze`.

## Local MVP Composition

The credential-free local composition starts the seven MVP control/execution
service adapters as separate containers. Git-backed Resources are mounted
read-only from this repository, while service readiness and future local
runtime state are externalized to the `aep-state` volume.

```powershell
Copy-Item deploy/local/.env.example deploy/local/.env
docker compose --env-file deploy/local/.env -f deploy/local/compose.yaml up --build -d
docker compose --env-file deploy/local/.env -f deploy/local/compose.yaml ps
Invoke-RestMethod http://localhost:8081/healthz
Invoke-RestMethod http://localhost:8082/v1/resources
docker compose --env-file deploy/local/.env -f deploy/local/compose.yaml down
```

The ports are event controller `8081`, resource controller `8082`, workflow
runtime `8083`, Agent Resolver `8084`, Context Builder `8085`, Tool Runtime
`8086`, and Evaluation Engine `8087`. Every service exposes `/healthz`; only
the Resource Controller exposes the read-only `/v1/resources` discovery
endpoint. Override the repository, Workspace, and execution-environment values
in `deploy/local/.env`. They must match the single repository-local
`.ai/workspace.yaml`, or every service fails fast. No external credentials are
used for startup or Resource discovery. The image explicitly binds Resource
validation to `/opt/aep/schemas/resources/v1`, independently of the read-only
repository mount and the installed Python package location.

`docker compose ... down` keeps the local state volume. To intentionally reset
that recoverable local state, run the same command with `--volumes`.

## Integrating A GitHub Repository

The MVP integration model is repository-bound. One AEP deployment processes
events for one configured GitHub repository and one immutable Workspace
version. An event naming an arbitrary repository does not cause AEP to clone or
onboard it. Before accepting events, the deployment must already have the
repository configuration, a revision-pinned checkout, provider credentials,
and the repository's versioned `.ai/` Resources.

> **Current implementation status:** this section defines the integration
> contract that the completed MVP must satisfy. The repository currently
> implements the underlying event, workflow, context, Agent, Tool, evaluation,
> policy, artifact, and observability components, plus the `AnalyzeIssue`,
> `BuildImplementationPlan`, `GeneratePatch`, `RunValidation`, and
> `EvaluateAcceptance` Task handlers. The local Event Controller does not yet
> expose a webhook POST endpoint, while `CreatePullRequest` and the end-to-end
> harness remain unimplemented. The current Compose stack is therefore a credential-free
> service-topology smoke test, not yet a deployable issue-to-PR integration.

### 1. Add Repository-Local Resources

Commit AEP configuration to the target repository under `.ai/`. Git is the
source of truth for desired AI behavior, so every reference must name an
explicit version; floating references such as `latest` are rejected.

The complete `issue-to-pr` configuration is expected to include:

```text
.ai/
  workspace.yaml
  events/
    github-issue-created.yaml
  workflows/
    issue-to-pr.yaml
  tasks/
    analyze-issue.yaml
    build-implementation-plan.yaml
    generate-patch.yaml
    run-validation.yaml
    evaluate-acceptance.yaml
    create-pull-request.yaml
  agents/
    issue-analyzer.yaml
    planner.yaml
    code-generator.yaml
    pr-writer.yaml
  prompts/
  models/
  tools/
  policies/
  evaluations/
  knowledge/
```

`workspace.yaml` binds the configuration to the exact GitHub repository:

```yaml
apiVersion: aep.dev/v1alpha1
kind: Workspace
metadata:
  name: widgets
  version: 1.0.0
spec:
  repository:
    provider: github
    owner: acme
    name: widgets
    defaultBranch: main
  resourceDiscovery:
    root: .ai
```

The Workflow must reference `Event/github-issue-created:1.0.0` and the six
Tasks in this dependency order:

```text
AnalyzeIssue
  -> BuildImplementationPlan
  -> GeneratePatch
  -> RunValidation
  -> EvaluateAcceptance
  -> CreatePullRequest
```

Tasks declare their required context, structured outputs, Agent when cognitive
work is required, Tool allowlist, Evaluations, and Policies. Agents reference
versioned Prompts and Models; model providers are Model Resources, not Tools.
Repository knowledge must be requested through the Context Builder and must
not be retrieved directly by an Agent. See `fixtures/resources/valid/` for
minimal Resource shapes and `schemas/resources/v1/` for the authoritative
contracts. Those fixtures demonstrate individual schemas; they are not a
complete ready-to-copy `issue-to-pr` Resource set.

### 2. Bind AEP To The Repository

Configure every service with the same repository and Workspace identity:

```text
AEP_REPOSITORY_PROVIDER=github
AEP_REPOSITORY_OWNER=acme
AEP_REPOSITORY_NAME=widgets
AEP_WORKSPACE_NAME=widgets
AEP_WORKSPACE_VERSION=1.0.0
AEP_EXECUTION_ENVIRONMENT=production
```

Mount or synchronize the target repository at `AEP_REPOSITORY_ROOT`. Startup
fails if the configured identity differs from `.ai/workspace.yaml`. The local
Compose file currently hard-codes this repository as the read-only `/workspace`
mount; changing only `deploy/local/.env` does not switch the mounted repository.
An integration deployment must replace that mount or provide an equivalent
repository synchronization mechanism.

Resource discovery may use a read-only checkout, but patch generation requires
a separate clean, writable Git worktree for each `WorkflowExecution`. Before
execution, the repository integration must:

1. Resolve the target default-branch commit to an immutable 40-character SHA.
2. Make that exact revision available in a clean Git worktree with `origin`
   configured.
3. Assign a unique working branch to the execution.
4. Scan repository knowledge at the same revision.
5. Retain the revision on every runtime object, artifact, evaluation, and
   policy decision.

The Git Tool intentionally does not clone repositories. It accepts an existing
worktree, expected revision, and working branch, and rejects revision drift,
the wrong branch, or an unexpectedly dirty checkout.

### 3. Configure GitHub Delivery And Credentials

Install a GitHub App, or configure an equivalent webhook and credential
provider, on the target repository. Subscribe to the GitHub **Issues** event.
The MVP accepts only the `opened` action and normalizes it as
`github.issue.created`.

The ingress boundary must:

1. receive an `issues` webhook as JSON;
2. verify `X-Hub-Signature-256` before trusting the payload;
3. pass `X-GitHub-Delivery` as the delivery ID used for deduplication;
4. reject events whose repository identity differs from the bound Workspace;
5. call the issue-created normalizer and persist the accepted Event; and
6. submit only the first accepted delivery for Workflow resolution.

GitHub retries are deduplicated using
`github:delivery:<X-GitHub-Delivery>`. Signature verification and the HTTP
route belong to the deployment ingress and are not implemented by the current
`aep.local_service` adapter. Do not point a webhook at `/healthz` or
`/v1/resources`; neither endpoint accepts events.

The GitHub identity used by the execution environment needs the least
privileges necessary to:

* read repository metadata and issues;
* read and push repository contents/branches; and
* create pull requests.

Provide Git push credentials through the Git credential-provider boundary and
GitHub API credentials through the GitHub client boundary. Provide model
credentials through the selected Model adapter. Do not commit tokens, webhook
secrets, or provider credentials to `.ai/` Resources. Credentials should be
short-lived where the provider supports it and must remain outside Tool input,
output, logs, and GeneratedArtifact bodies.

### 4. Process The Issue Into A Pull Request

For an accepted event, the controllers perform the following bounded flow:

1. Resolve the explicitly versioned `issue-to-pr` Workflow and deterministic
   Task DAG.
2. Create one idempotent `WorkflowExecution` bound to the Event, repository
   revision, knowledge snapshot, and trace.
3. Build provenance-rich `ContextPackages`; Agents receive these packages and
   cannot query the repository directly.
4. Analyze the issue and persist a schema-valid `ISSUE_ANALYSIS` artifact.
5. Build and persist a schema-valid `IMPLEMENTATION_PLAN` artifact.
6. Authorize scoped Filesystem and Git capabilities, generate changes in the
   execution worktree, persist a content-addressed patch, and verify that it
   applies and changes only allowed paths.
7. Authorize Docker execution, run the configured build and test commands with
   networking disabled, and persist separate build and test evidence.
8. Aggregate required artifacts and EvaluationResults without an LLM and fail
   closed if evidence is missing, failed, or from another revision.
9. Evaluate Publication Policy before any external publication, then separately
   authorize `git.push` and `github.create_pr` through Pre-Execution Capability
   Policy.
10. Push the execution branch, create one idempotent pull request against the
    configured default branch, link the triggering issue, and include the plan
    and validation summary. The MVP never merges the pull request.

A denial, missing approval, failed patch check, failed build or test, stale
revision, or incomplete evidence stops publication. Retrying a GitHub delivery
or external operation must reuse persisted identity and evidence rather than
creating another WorkflowExecution, branch mutation, or pull request.

### 5. Verify The Integration

Before enabling a real webhook, verify the deployment in this order:

1. Start every service and require `/healthz` to report the same repository and
   Workspace identity.
2. Query the Resource Controller's `/v1/resources` endpoint and confirm that it
   discovers the Workspace, Event, Workflow, all six Tasks, four Agents, and
   every referenced Prompt, Model, Tool, Policy, Evaluation, and KnowledgeBase.
3. Scan a known repository revision and confirm the knowledge snapshot records
   that exact SHA.
4. Replay `fixtures/github/issue-created.json` through the authenticated ingress
   with a fixed delivery ID, then replay it again and confirm deduplication.
5. Run the deterministic end-to-end harness with fake model and GitHub clients;
   confirm all TaskExecutions, GeneratedArtifacts, EvaluationResults,
   PolicyDecisions, lifecycle events, and the final PR URL.
6. Exercise at least one blocked-publication path, such as a failed test or
   denied `github.create_pr`, and confirm that no branch is pushed and no pull
   request is created.

Step 5 becomes available when AEP-037 is implemented. Until the webhook ingress,
remaining Task handlers, provider wiring, and that harness are complete, use
the component tests and local composition only for contract and readiness
validation—not for a live repository integration.

## Key Documents

* [Product Requirements](docs/prd.md)
* [Architecture Overview](docs/architecture/overview.md)
* [Runtime Object Model](docs/adr/ADR-002-runtime-object-model.md)
* [MVP Vertical Slice](docs/adr/ADR-003-mvp-vertical-slice.md)
* [Self-Hosting Repository Integration](docs/adr/ADR-004-self-hosting-repository-integration.md)
* [Structured Observability](docs/architecture/observability.md)
* [Execution Plan](docs/execution-plan.md)
* [Implementation Tasks](docs/implementation-tasks.md)

## Current Status

This repository is in active MVP implementation. The declarative and runtime
contracts are established, and 35 of the 43 implementation tasks are complete.

The implementation plan is split into independent task files under [docs/tasks](docs/tasks/). Each task includes context, dependencies, deliverable, and acceptance criteria.

Task status is tracked in [docs/execution-plan.md](docs/execution-plan.md).

Implemented foundations currently include:

* Resource and runtime-object JSON Schemas, fixtures, and validation.
* Repository-local Resource loading with immutable version enforcement.
* In-memory runtime persistence, idempotent claims, and immutable terminal evidence.
* GitHub issue-created event normalization and deduplication.
* Deterministic Event-to-Workflow resolution with explicit versioned references.
* Deterministic Task DAG resolution with dependency metadata and parallel-ready groups.
* Retry-safe Workflow scheduling with dependency blocking, numbered attempts,
  provider-neutral Task executors, and append-only lifecycle events.
* Provider-neutral structured lifecycle logging with shared trace correlation,
  service-boundary propagation, Resource/revision fields, and recursive secret
  and artifact-body redaction.
* Stateless Agent resolution into immutable, execution-scoped ResolvedAgent
  inputs with explicit Prompt, Model, non-model Tool, and Policy versions.
* Bounded AgentInvocation coordination with deterministic Prompt and
  ContextPackage assembly, provider evidence, and structured-output validation.
* Idempotent WorkflowExecution trace-root creation with provenance events.
* TaskExecution lifecycle and retry semantics.
* Provider-neutral ModelInvocation and Tool Runtime contracts.
* Deterministic pre-execution capability policy with persisted decisions.
* Workspace-confined Filesystem Tool reads and policy-authorized writes with
  trusted control-plane reads, race-safe handle confinement, and idempotent
  persisted invocation evidence.
* Repository-bound Git Tool operations for branch creation, status, diff, and
  capability-authorized push through an injected isolated sandbox with
  short-lived credentials, explicit remote-mutation state, and redacted command
  evidence.
* Policy-gated, workspace-scoped Docker validation with digest-pinned images,
  disabled networking, bounded execution, and per-command execution evidence.
* Deterministic build and test evaluation with separate immutable outcomes,
  durations, logs addresses, and timeout or missing-output evidence.
* A policy-gated GitHub Tool adapter for issue reads and pull-request creation.
* Deterministic JSON Schema evaluation.
* Deterministic patch applicability and allowed-path evaluation with immutable
  changed-file and Git diagnostic evidence.
* Deterministic Publication Policy with fail-closed evidence checks,
  restrictive versioned-rule composition, and immutable explainable decisions.
* AnalyzeIssue Task handling that composes ContextPackage construction,
  versioned Agent resolution, bounded model invocation, schema Evaluation, and
  immutable `ISSUE_ANALYSIS` GeneratedArtifact publication.
* BuildImplementationPlan Task handling that consumes the successful prior
  issue analysis through deterministic ContextPackage construction, invokes
  the versioned Planner, validates every required plan section, and publishes
  an immutable `IMPLEMENTATION_PLAN` GeneratedArtifact without checkout writes.
* Revision-bound repository scanning and repository-knowledge queries.
* Non-cognitive RunValidation Task handling with versioned Docker configuration,
  retry-safe ToolInvocation evidence, separate build/test outcomes, and an
  immutable validation report for pass and failure paths.
* Non-cognitive EvaluateAcceptance Task handling that walks prior execution
  evidence, verifies artifact and evaluation completeness, identity,
  provenance, revision, and outcomes, and persists an immutable final summary.
* Immutable GeneratedArtifact metadata with content-addressed content storage.
* Deterministic, budget-aware ContextPackage construction with provenance for
  repository knowledge, Resources, events, policies, and prior artifacts.
* Credential-free local composition of the seven MVP service boundaries with
  explicit ports, health checks, one repository and Workspace, and externalized
  local persistence.


The remaining work includes the downstream CreatePullRequest Task handler and
deterministic end-to-end issue-to-pull-request harness, plus the authenticated ingress,
execution-checkout provisioning, complete self-hosting Resource bundle, live
GitHub and Model provider integrations, and dogfood deployment required to
register this repository with a running AEP control plane. See
[ADR-004](docs/adr/ADR-004-self-hosting-repository-integration.md) for the
repository-bound, generational self-hosting decision.

Repository-specific agent workflows live under [skills/](skills/):

* `implement-aep-feature` implements tasks from their contracts and acceptance criteria.
* `review-aep-pr` independently reviews implementations, local diffs, and pull requests.

## MVP Scope

The first vertical slice targets one repository, one workspace, one workflow, and one GitHub event type: `github.issue.created`.

The MVP intentionally excludes pull request merge, deployment, multi-tenant authentication, multi-repository workflows, workflow generation, and LLM-as-judge evaluation.

## License

See [LICENSE](LICENSE).
