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

## Key Documents

* [Product Requirements](docs/prd.md)
* [Architecture Overview](docs/architecture/overview.md)
* [Runtime Object Model](docs/adr/ADR-002-runtime-object-model.md)
* [MVP Vertical Slice](docs/adr/ADR-003-mvp-vertical-slice.md)
* [Structured Observability](docs/architecture/observability.md)
* [Execution Plan](docs/execution-plan.md)
* [Implementation Tasks](docs/implementation-tasks.md)

## Current Status

This repository is in active MVP implementation. The declarative and runtime
contracts are established, and 26 of the 37 implementation tasks are complete.

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
* Revision-bound repository scanning and repository-knowledge queries.
* Immutable GeneratedArtifact metadata with content-addressed content storage.
* Deterministic, budget-aware ContextPackage construction with provenance for
  repository knowledge, Resources, events, policies, and prior artifacts.
* Credential-free local composition of the seven MVP service boundaries with
  explicit ports, health checks, one repository and Workspace, and externalized
  local persistence.

Patch evaluation, publication policy, task handlers, observability, and the
end-to-end issue-to-pull-request harness remain to be implemented.

Repository-specific agent workflows live under [skills/](skills/):

* `implement-aep-feature` implements tasks from their contracts and acceptance criteria.
* `review-aep-pr` independently reviews implementations, local diffs, and pull requests.

## MVP Scope

The first vertical slice targets one repository, one workspace, one workflow, and one GitHub event type: `github.issue.created`.

The MVP intentionally excludes pull request merge, deployment, multi-tenant authentication, multi-repository workflows, workflow generation, and LLM-as-judge evaluation.

## License

See [LICENSE](LICENSE).
