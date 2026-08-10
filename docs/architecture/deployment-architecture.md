# Deployment Architecture

**Project:** AI Agent Engineering Platform (AEP)

**Document:** Deployment Architecture

**Status:** Draft

**Version:** 0.1

---

# 1. Overview

The AEP deployment architecture separates the platform into three independent planes:

* Control Plane
* Execution Plane
* Storage Plane

Each plane owns a distinct responsibility and may scale independently.

The deployment model is Kubernetes-native.

All platform components execute as containerized services.

---

# 2. Design Goals

The deployment architecture should provide:

* deterministic execution
* horizontal scalability
* fault isolation
* reproducibility
* observability
* incremental evolution

---

# 3. High-Level Deployment

```text
                      Kubernetes Cluster

┌──────────────────────────────────────────────────────────────┐

                 CONTROL PLANE

┌──────────────────────────────────────────────────────────┐
│                                                          │
│ Resource Registry                                        │
│ Workflow Controller                                      │
│ Event Controller                                         │
│ Knowledge Compiler                                       │
│ Policy Controller                                        │
│ Version Manager                                          │
│                                                          │
└──────────────────────────────────────────────────────────┘

──────────────────────────────────────────────────────────────

                 EXECUTION PLANE

┌──────────────────────────────────────────────────────────┐
│                                                          │
│ Workflow Runtime                                         │
│ Agent Resolver                                           │
│ Context Builder                                          │
│ Tool Runtime                                             │
│ Evaluation Engine                                        │
│                                                          │
└──────────────────────────────────────────────────────────┘

──────────────────────────────────────────────────────────────

                 STORAGE PLANE

┌──────────────────────────────────────────────────────────┐
│                                                          │
│ Git                                                      │
│ PostgreSQL                                               │
│ Object Storage                                           │
│ Graph Store                                              │
│ Redis                                                    │
│                                                          │
└──────────────────────────────────────────────────────────┘

└──────────────────────────────────────────────────────────────┘
```

---

# 4. Control Plane

The Control Plane manages Resources.

Responsibilities include:

* resource lifecycle
* reconciliation
* dependency resolution
* version discovery
* repository synchronization
* knowledge compilation

The Control Plane never executes workflows.

---

# 5. Execution Plane

The Execution Plane executes runtime objects.

Responsibilities include:

* workflow execution
* task scheduling
* context construction
* agent resolution
* tool execution
* evaluation

Execution services are stateless.

Execution state is externalized.

The Agent Resolver is a stateless execution-plane component, not a runtime service with durable state.

It loads Agent, Prompt, Model, Tool, and Policy resources and produces ResolvedAgent runtime objects for Workflow Runtime use.

---

# 6. Storage Plane

The Storage Plane persists durable platform state.

The platform distinguishes between:

## Systems of Record

Authoritative sources.

Examples:

* Git repositories
* GitHub

---

## Operational State

Mutable runtime state.

Examples:

* WorkflowExecution
* TaskExecution
* leases
* queues

---

## Derived State

Can always be regenerated.

Examples:

* Repository Knowledge Graph
* indexes
* caches

---

## Durable Artifacts

Produced by workflows.

Examples:

* plans
* reports
* patches
* evaluations

---

# 7. Storage Components

## Git

Source of truth for:

* source code
* workflows
* prompts
* policies
* resources

Nothing supersedes Git.

---

## PostgreSQL

Stores operational metadata.

Examples:

* execution state
* workflow history
* approvals
* runtime metadata

Does not store repository knowledge.

---

## Graph Store

Stores Repository Knowledge Graph versions.

The graph is a compiled artifact.

It is never manually edited.

---

## Object Storage

Stores immutable artifacts.

Examples:

* reports
* logs
* patches
* documentation

Objects are content-addressable.

---

## Redis

Provides:

* distributed locks
* execution queues
* caching
* rate limiting

Redis contains no authoritative data.

---

# 8. Kubernetes Topology

Each subsystem executes independently.

Example deployment:

```text
namespace

aep-system

    resource-controller

    workflow-runtime

    agent-resolver

    context-builder

    knowledge-compiler

    tool-runtime

    evaluation-engine

    observability
```

Each deployment scales independently.

---

# 9. Workflow Execution

Every WorkflowExecution creates runtime workers.

```text
Workflow

↓

WorkflowExecution

↓

TaskExecution

↓

AgentInvocation

↓

ToolInvocation
```

Runtime workers remain stateless.

---

# 10. Tool Execution

Tool execution occurs in isolated containers.

```text
Tool Runtime

↓

Sandbox

↓

Container

↓

External System
```

Containers are destroyed after execution.

No Tool persists state locally.

---

# 11. Knowledge Compilation

Repository synchronization pipeline:

```text
Git Push

↓

Repository Sync

↓

Knowledge Compiler

↓

Repository Knowledge Graph

↓

Publish Graph Version
```

Compilation occurs asynchronously.

Workflow execution always references published graph versions.

---

# 12. Scheduling

Workflow Runtime schedules Tasks.

Kubernetes schedules containers.

These responsibilities remain separate.

Workflow Runtime never manages Pods directly.

---

# 13. Networking

Internal communication occurs through platform APIs.

Examples:

Workflow Runtime

↓

Context Builder API

↓

Knowledge Query API

↓

Tool Runtime API

Services remain loosely coupled.

---

# 14. Scaling Strategy

Control Plane

Scale for repository count.

Execution Plane

Scale for concurrent workflow executions.

Knowledge Compiler

Scale for repository analysis throughput.

Tool Runtime

Scale for external workload.

Evaluation Engine

Scale for CI demand.

Each subsystem scales independently.

---

# 15. Fault Isolation

Failures remain isolated.

Examples:

Tool crash

↓

Restart Tool Runtime

Workflow continues

Knowledge compilation failure

↓

Repository marked stale

Workflow uses previous graph

Agent timeout

↓

Retry Task

Controller failure

↓

Execution unaffected

---

# 16. Observability

Every service emits:

* logs
* metrics
* traces
* events

Each runtime object receives a unique execution identifier.

Observability follows the complete execution path.

```text
WorkflowExecution

↓

TaskExecution

↓

ContextPackage

↓

AgentInvocation

↓

ToolInvocation

↓

EvaluationResult
```

Every runtime object shares the same trace identifier.

---

# 17. Metrics

Example platform metrics:

Control Plane

* reconciliation latency
* graph compilation duration
* controller queue depth

Execution Plane

* workflow duration
* task latency
* retry count
* agent latency

Tool Runtime

* execution time
* timeout rate
* resource consumption

Evaluation

* pass rate
* failure categories

Platform

* execution cost
* token usage
* artifact count

---

# 18. Logging

Every runtime object emits structured logs.

Logs always include:

* execution ID
* task ID when the event is within a TaskExecution
* trace ID
* workflow version
* task version
* agent version
* repository revision
* status, timing, and failure classification

Logs are immutable.

The provider-neutral field contract, lifecycle event names, service-boundary
propagation rules, and redaction requirements are defined in
[Structured Observability](observability.md). Secrets and artifact bodies are
never lifecycle-log fields; logs retain only safe identifiers and content
addresses for large evidence.

---

# 19. Disaster Recovery

Recovery priorities:

Git

↓

Resources

↓

Knowledge Graph

↓

Workflow History

↓

Artifacts

Derived state may be regenerated.

Operational state may be replayed.

Git remains the ultimate source of truth.

---

# 20. Local MVP Composition

Before Kubernetes deployment, `deploy/local/compose.yaml` runs the ADR-003 MVP
topology as seven independently configured containers:

* Event Controller
* Resource Controller
* Workflow Runtime
* Agent Resolver
* Context Builder
* Tool Runtime
* Evaluation Engine

The composition binds exactly one Git repository, one immutable Workspace
version, and one execution-environment name. Git Resources are mounted
read-only. The image's Resource schema directory is configured explicitly and
does not depend on the Python installation path. Mutable local adapter state is externalized to a named volume, so
service processes retain no hidden global state. Every service exposes an
independent health endpoint and validates the configured repository and
Workspace identity before reporting ready.

The shared local HTTP adapter is a composition seam, not a transfer of
architectural ownership: Docker supplies independent process and network
boundaries, and each logical service remains separately addressable. The local
Resource Controller additionally provides read-only Resource discovery for
credential-free smoke verification. Production storage services and
Kubernetes manifests remain future deployment work.

---

# 21. Repository Registration And Self-Hosting

Repository registration is an explicit deployment operation, not a consequence
of receiving an event. For the MVP, one deployment binds exactly one GitHub
repository and one immutable Workspace version. The webhook payload must match
that identity; it cannot select an arbitrary repository or cause dynamic
onboarding.

The first registered repository is
`github:yijiazho/agent-engineering-platform`. Its self-hosting deployment follows
ADR-004 and separates three repository views:

* a pinned, read-only control-plane release and Resource checkout;
* a trusted repository source/cache used to resolve immutable revisions; and
* one clean, writable, revision-bound worktree per WorkflowExecution.

Authenticated GitHub ingress verifies the raw delivery signature, delivery ID,
event type, action, and repository before normalization and deduplication. A
trusted checkout manager provisions execution worktrees; Agents cannot clone,
fetch, choose revisions, or retrieve repository knowledge directly. GitHub App
and Model provider credentials enter only through runtime secret and injected
provider boundaries.

The live MVP Model boundary selects the OpenAI adapter from the immutable
`Model.spec.provider`. The adapter translates the already assembled Prompt,
ContextPackage, and output schema to one strict structured Responses API
request. Model Resource parameters, output-token limit, timeout, and retry
policy remain the effective invocation bounds and are recorded on
ResolvedAgent and ModelInvocation evidence. The API key is read from
`AEP_OPENAI_API_KEY_FILE`; endpoint configuration comes from
`AEP_OPENAI_API_URL`, never from a Resource. Fixed failure diagnostics and
content addresses keep credentials and request/output bodies out of lifecycle
logs. Local selection and endpoint verification requires neither credentials
nor network access; live startup fails closed when the secret is missing.
The provider transport applies one remaining deadline to connection, headers,
and bounded incremental response reads, and cancels the caller-visible
operation when that deadline expires. It never follows HTTP redirects, which
prevents the Authorization header from crossing origins or an HTTPS-to-HTTP
downgrade.
Only finite, stateless generation parameters cross this provider boundary;
provider conversation handles are rejected. Evidence records the Model
Resource's requested identity separately from the bounded provider-resolved
identity so aliases can resolve to snapshots without changing the request.
The adapter maps the versioned Prompt system and formatting content to the
provider instruction channel, while the ContextPackage remains user content.
This preserves provider-level priority for self-hosting guardrails over
potentially adversarial issue and repository text.
Content-filtered and output-token-exhausted incomplete results are terminal for
an unchanged bounded request. Decoder recursion limits and non-finite retry
hints are normalized, and structural schema projection distinguishes schema
keywords from identically named fields in property maps.

The GitHub provider resolves the installation from the bound owner/name and
uses a repository-restricted, short-lived installation token. Token refresh is
single-flight under concurrency and occurs before expiry. The provider adapts
the existing GitHub Tool client operations, reconciles an owner/head/base PR
before creation, and maps authentication, authorization, validation, rate
limit, retryable service, timeout, and unknown mutation outcomes to stable Tool
evidence. Its Git credential provider supplies one-use askpass environments to
both source fetch and execution-branch push and removes the lease material in a
`finally`-guarded close. Readiness proves App, installation, repository, base,
and branch-prefix identity without returning credentials.
Each provider operation carries one monotonic deadline across authentication,
reconciliation, and mutation calls. Readiness always revalidates the current
repository installation, and authentication or installation lookup failures
invalidate cached installation state. Mutation evidence becomes `UNKNOWN` only
after the pull-request POST has actually begun, including when a successful
response cannot be parsed or otherwise confirmed. Authentication transport
timeouts retain timeout classification through the Tool execution boundary.
The Git Tool passes its remaining deadline into Git credential acquisition,
and credential timeouts remain Tool `TIMED_OUT` results before push begins.
Both primary limit evidence and secondary-limit `403` responses with
`Retry-After` produce bounded rate-limit retry metadata.

The checkout manager uses separate, non-overlapping roots for the trusted bare
source cache and execution worktrees. A provider-neutral Repository Source
materializes the configured default branch with ephemeral credential leases,
then the manager verifies the WorkflowExecution's exact commit before creating
a deterministic execution branch. An atomic registry owns each derived path
and branch, rejects cross-execution reuse, and leases in-progress claims so an
interrupted worker can recover without racing a live worker. Ready checkout
metadata is the shared repository, revision, branch, and path binding supplied
to Repository Knowledge, Filesystem, Git, and Docker boundaries.

The local persistence adapter implements the registry with SQLite under the
shared state volume. Transactions provide compare-and-swap ownership and
database-enforced uniqueness for execution paths and repository branches.
Execution and repository-cache claims carry independent, monotonically
increasing fencing tokens. The manager renews and verifies those claims before
and throughout every source-cache or worktree mutation with an active heartbeat.
This prevents takeover while a bounded mutation is still running and prevents
an expired worker from starting another mutation after a genuine takeover.
Credential and Repository Source failures, even adapter-supplied classified
failures, are reduced to fixed safe diagnostics; provider exception text and
credential values are excluded from errors, exception cause or context chains,
formatted tracebacks, and persisted runtime evidence. HTTP(S) source URLs with
user information, queries, or fragments are invalid configuration.

`CheckoutBoundOrchestration` is the production handoff from provisioning to
execution. It creates the revision-bound Repository Knowledge snapshot and
Context Builder provider, Filesystem workspace, Git adapter, Docker authorized
workspace, and publication inputs from the same immutable binding. Existing
runtime fields are checked rather than overwritten silently. The binding writes
the checkout branch to the TaskExecution `workingBranch` field consumed by
`CreatePullRequest`, ensuring its Git commit and push operations and GitHub
pull-request head use the bound branch. Any independently supplied path,
revision, repository, branch, or execution identity mismatch is a configuration
failure.

Source-cache and worktree storage must survive worker restart and be sized for
the repository plus maximum concurrent builds. Credentials must never be
embedded in cache remotes or Git configuration. Cleanup starts only after
terminal evidence is durable. Dirty worktrees or removal failures are retained
with classified recovery metadata for operator inspection; clean removal is
idempotent, and expired provisioning claims may be retried safely.
The Event Controller implements this edge at `POST /v1/webhooks/github`. It
loads the HMAC secret from runtime configuration, verifies the raw body before
JSON decoding, limits request size, and accepts only `issues/opened` for the
deployment's bound repository. Its provider-neutral dispatch boundary uses a
SQLite database on the shared state volume to commit the accepted Event and one
pending reconciliation-outbox row atomically. Concurrent controller instances
serialize on that transaction, and restarts observe the same Event and outbox
identity. A transaction failure rolls back both records so an authenticated
retry can submit again; a committed replay returns the original Event without
another outbox row. Edge evidence records the stable trace and outcome but
excludes request bodies, headers, and secrets.

The deployed version may generate an unmerged pull request proposing its
successor, but it cannot modify its running image or read-only Resource
checkout, merge the pull request, or deploy the result. A human-reviewed release
creates the next pinned control-plane version. AEP-038 through AEP-043 track the
ingress, checkout, Resource, provider, and pilot work needed to make this
deployment operational.

---

# 22. Future Evolution

The deployment architecture intentionally supports:

* multiple Kubernetes clusters
* remote execution workers
* GPU scheduling
* distributed Tool Runtimes
* cross-region execution
* organization-wide Knowledge Graphs
* multi-repository orchestration

No architectural changes should be required.

---

# 23. Design Principles

## Kubernetes Native

The platform should leverage Kubernetes rather than replacing it.

---

## Stateless Services

Business logic remains stateless.

Persistent state belongs to the Storage Plane.

---

## Git Is the Source of Truth

Resources originate from Git.

Derived systems may be rebuilt.

---

## Immutable Artifacts

Artifacts never change after publication.

---

## Independent Scaling

Every subsystem scales independently.

---

## Recoverable State

Everything except Git repositories and workflow history should be reproducible.

---

# 24. Summary

The AEP deployment architecture separates resource management, workflow execution, and persistence into independent Control, Execution, and Storage planes.

By treating Git as the authoritative source of truth, Repository Knowledge Graphs as compiled artifacts, runtime objects as ephemeral state, and execution services as stateless workers, the platform achieves reproducibility, fault isolation, and horizontal scalability while remaining aligned with Kubernetes' architectural principles.
