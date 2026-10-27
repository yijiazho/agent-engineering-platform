# Distributed Execution

**Project:** AI Agent Engineering Platform (AEP)

**Document:** Distributed Execution

**Status:** Draft

**Version:** 0.1

---

# 1. Overview

This document specifies the target architecture for running AEP on more than one
machine while sharing artifacts and evidence. It extends ADR-006 and composes
with repository registration (ADR-007).

The design is execution-affinity based: a WorkflowExecution is leased to one
worker at a time. The worker runs the complete Task DAG against a node-local
worktree. Runtime state and artifact metadata live in a transactional store;
artifact and evidence bytes live in a shared content-addressed object store.

The local single-host profile remains supported. Distribution changes adapters,
not workflow semantics.

## 1.1 Goals

* One explicit owner per WorkflowExecution.
* Shared artifacts readable by any authorized worker.
* Node-local mutable worktrees with failover by re-provisioning.
* Durable dispatch, leases, and terminal evidence.
* At-least-once transport with idempotent AEP contracts.
* Strong registration and repository isolation.
* No Agent access to storage, queues, leases, or repository discovery.

## 1.2 Non-Goals

* Active-active execution of one WorkflowExecution.
* Task-level microdistribution in the first increment.
* Cross-organization artifact sharing.
* Active-active cross-region control planes.
* Auto-scaling policy or Kubernetes manifests.
* Replacing Git, GitHub, or provider idempotency.

---

# 2. Current Boundary

The MVP is correctly single-host:

| Concern | Current implementation |
| --- | --- |
| Runtime objects | `DurableJsonRuntimeObjectStore` with a cross-process file fence |
| Artifact bytes | `FilesystemContentAddressedStore` under `AEP_STATE_ROOT` |
| Artifact metadata | Runtime store through `InMemoryGeneratedArtifactStore` |
| Checkout registry | SQLite database under the state root |
| Worktree paths | Absolute paths on the provisioning node |
| Webhook outbox | SQLite inbox/outbox with no claim or visibility timeout |
| Dispatch | In-process `WorkflowScheduler` and `TaskExecutor` |
| Docker | Local Docker socket and host path mapping |
| Model admission | Process-local coordinator; more than one worker replica rejected |
| Inspection | `aep` CLI reading one state file |

A shared state directory is not a supported substitute. SQLite WAL, `flock`,
directory sync, and atomic replace do not have portable semantics across NFS or
SMB. The target architecture replaces these adapters rather than weakening
their guarantees.

---

# 3. Target Topology

```text
                         GitHub App webhook
                                |
                                v
+-------------------------------------------------------------+
| Control plane                                               |
|                                                             |
|  Event Controller -> Registration Router -> Event + Outbox  |
|  Dispatcher       -> Dispatch Queue -> Execution Creator    |
|  Inspection API/CLI                                         |
+------------------------------+------------------------------+
                               |
                 +-------------+-------------+
                 |                           |
                 v                           v
      +---------------------+     +---------------------+
      | Transactional store |     | Object store        |
      |                     |     |                     |
      | runtime objects     |     | artifact bytes      |
      | execution events    |     | tool logs           |
      | deterministic keys  |     | model I/O evidence  |
      | dispatch queue      |     | validation reports  |
      | leases and fences   |     |                     |
      | artifact metadata   |     |                     |
      | registrations       |     |                     |
      | quota reservations  |     |                     |
      +----------+----------+     +----------+----------+
                 |                           ^
                 |                           |
      +----------+---------------------------+----------+
      |                                                     |
      v                                                     v
+---------------------------+                 +---------------------------+
| Execution worker A        |                 | Execution worker N        |
|                           |                 |                           |
| lease + fence             |                 | lease + fence             |
| source cache              |                 | source cache              |
| execution worktree        |                 | execution worktree        |
| local Docker daemon       |                 | local Docker daemon       |
| Git credential lease      |                 | Git credential lease      |
+---------------------------+                 +---------------------------+
```

Only the control plane writes shared state directly for admission and dispatch.
Workers write runtime evidence through the transactional adapter and bytes
through the object-store adapter.

---

# 4. Component Responsibilities

| Component | Responsibility | Must not |
| --- | --- | --- |
| Event Controller | Verify signature, route registration, commit Event and outbox | Trust the payload before signature verification |
| Registration Router | Map repository identity to an active registration | Create registrations from events |
| Dispatcher | Claim outbox rows, create idempotent executions, enqueue dispatch | Execute tasks |
| Scheduler | Resolve the Task DAG and advance one ready wave | Run on more than one owner for one execution |
| Execution Worker | Hold an execution lease, provision a local worktree, run handlers | Use another node's worktree or write without its fence |
| Checkout Manager | Resolve the source revision and own worktree lifecycle | Expose clone/fetch/revision selection to Agents |
| Artifact Catalog | Store artifact metadata and registration scope | Treat content addressing as authorization |
| Object Store | Store immutable content-addressed bytes | Serve bytes without catalog authorization |
| Model Admission | Reserve request and token capacity across workers | Store raw credentials or provider bodies |
| Publisher | Recheck lease, policy, and provider identity before mutation | Publish from a stale or lease-lost worker |
| Inspector | Read correlated evidence and explain outcomes | Materialize artifact bodies by default |

---

# 5. State And Ownership

| State | Backend | Lifetime | Authority |
| --- | --- | --- | --- |
| Resource checkout | Git plus read-only local mirror | Per registration generation | Immutable revision |
| Runtime objects | Transactional database | Retained per policy | Transaction and registration scope |
| ExecutionEvents | Transactional database | Append-only | Immutable |
| Deterministic keys and claims | Transactional database | Per identity | Unique constraint |
| Event outbox | Transactional database | Until terminal | Claim and visibility timeout |
| Dispatch queue | Transactional database | Until terminal | Lease and fence |
| Execution lease | Transactional database | Heartbeat and expiry | Fence token |
| Artifact metadata | Transactional database | Retained per policy | Registration scope |
| Artifact and evidence bytes | Object store | Retained or garbage-collected | Content address plus catalog |
| Source cache | Node-local filesystem | Node lifetime | Node-local |
| Execution worktree | Node-local filesystem | Execution lifetime | Execution owner |
| Docker container | Node-local daemon | Invocation lifetime | Tool Runtime boundary |
| Repository and PR truth | Git and GitHub | Provider lifetime | Provider identity |

Runtime object IDs remain deterministic. `registrationId`, `workerId`, and lease
identity are additional provenance and scope fields. `workspacePath` is node-local
evidence, not a portable key.

---

# 6. Execution Lifecycle

## 6.1 Admission

1. GitHub signs the delivery.
2. The Event Controller verifies the raw body with the App webhook secret.
3. The router resolves `repository.id` to an `ACTIVE` registration generation.
4. The controller rejects an unregistered, suspended, mismatched, or malformed
   delivery before persistence.
5. One transaction commits the normalized Event, deduplication key, and outbox
   row scoped by `registrationId`.

Event Controllers are stateless with respect to ingress. Multiple replicas may
run behind a load balancer without sticky sessions: signature verification is
local, and the unique deduplication constraint plus transactional outbox make
concurrent delivery and GitHub redelivery safe. Only the registration lookup and
Webhook secret are shared configuration.

## 6.2 Dispatch

1. The dispatcher claims an outbox row with a visibility timeout.
2. It resolves the Event to a versioned Workflow and immutable Task DAG.
3. It creates or claims one idempotent WorkflowExecution at an immutable
   repository revision.
4. It commits one dispatch row with required capabilities:
   registration, repository, validation profile, and publication eligibility.
5. The outbox row is marked complete only when the execution reaches terminal
   evidence.

## 6.3 Worker Claim

1. A worker advertises its worker ID, registration allowlist, Docker
   capability, and available capacity.
2. It claims one dispatch row with a compare-and-swap lease.
3. The lease records worker ID, execution ID, registration, fence token,
   heartbeat time, and expiry.
4. The worker verifies the recorded WorkflowExecution revision and provisions a
   local source cache and worktree.
5. The worker renews and verifies the lease before every durable mutation and
   before every external effect.

## 6.4 Task Execution

1. The scheduler loads the authoritative WorkflowExecution and plan.
2. It creates one idempotent attempt per ready Task.
3. It transitions and emits lifecycle events in one transaction.
4. Context Builder reads Resources, prior artifacts, and revision-bound
   knowledge through its existing contracts.
5. Task handlers write only through the transactional store and object store.
6. GeneratedArtifact bytes are written first; metadata and evaluation evidence
   commit afterward.
7. The scheduler stops at configuration, evaluation, policy, or exhausted
   retry failures and leaves dependents blocked.

## 6.5 Publication

1. EvaluateAcceptance completes.
2. Publication Policy and capability policy allow the action.
3. The publisher rechecks the execution lease and fence.
4. Git commit and push use the captured execution branch and repository
   identity.
5. GitHub pull-request creation uses idempotent branch/head/base identity.
6. Ambiguous remote outcomes are recorded as `UNKNOWN` and reconciled before
   retry.
7. Terminal evidence commits before the dispatch row is retired.

## 6.6 Release And Cleanup

1. The worker releases the execution lease only after terminal evidence is
   durable.
2. Clean worktrees are removed idempotently.
3. Dirty worktrees and failed removals remain recorded on the owning node for
   operator recovery.
4. A lost node's worktree is re-provisioned on the next owner from the recorded
   revision; no live worktree is copied between nodes.

---

# 7. Artifact And Evidence Model

## 7.1 Metadata

`GeneratedArtifact` metadata remains a runtime object with:

* artifact ID and content address;
* media type and producer TaskExecution;
* WorkflowExecution, registration, repository revision, and trace;
* evaluation and publication references;
* creation and retention timestamps.

Catalogue queries are registration-scoped. A worker cannot list another
registration's artifacts.

## 7.2 Bytes

Artifact and evidence bytes are immutable and content-addressed:

```text
<s3-prefix>/<registrationId>/sha256/<first-two>/<remaining-hex>
```

Rules:

* `put` is idempotent for an existing identical digest.
* A conflicting digest is an integrity failure.
* A write uses a unique temporary key, then an atomic object-store commit.
* Bytes are written before metadata.
* A read verifies the digest before returning content.
* Orphaned objects are collected only after a grace period and only when no
  metadata references them.
* Cross-registration deduplication is disabled by default.

## 7.3 Evidence Classes

The same store should eventually hold:

* GeneratedArtifact bodies;
* Git and Docker command logs referenced by ToolInvocation;
* normalized Model input and output payloads referenced by ModelInvocation;
* validation reports and bounded diagnostic bundles;
* registration validation reports.

Lifecycle logs remain structured records, not object bodies. They may contain
content addresses but never prompt, source, event, or artifact content.

## 7.4 Provenance

Content addressing proves byte identity, not authority. Before bytes are
returned to Context Builder or a publication handler, the catalog must confirm:

* the artifact belongs to the same registration;
* the producer TaskExecution belongs to the expected WorkflowExecution;
* the repository revision and trace match;
* the producer reached the required terminal state;
* the artifact is not superseded or retained as a rejected proposal.

## 7.5 Deployment Boundary

The shared artifact catalog belongs to one AEP control plane. Multiple
deployments may point at the same object-store product, but they do not share
catalog authority, registration scope, or provenance. Cross-deployment reuse
requires an explicit export/import operation that creates a new catalog record
in the destination and preserves the original content digest as a reference.

This boundary prevents a deployment from treating the mere existence of a
content-addressed object as authorization to read it.

---

# 8. Leases, Fencing, And Idempotency

## 8.1 Lease State Machine

```text
QUEUED -> LEASED -> RUNNING -> SUCCEEDED
              |        |
              |        +-> FAILED
              +-> EXPIRED -> QUEUED (new owner, new fence)
```

A lease owns one WorkflowExecution. A fence token increases monotonically on
every ownership change. A worker must present its current fence for every write
and external effect. A stale fence is rejected before any mutation.

## 8.2 Heartbeats

* The worker heartbeats at a bounded interval.
* Lease expiry is longer than the heartbeat interval and shorter than the
  operator-visible stuck threshold.
* Renewal verifies ownership; renewal failure stops dispatch of new work.
* Long-running local operations renew through a heartbeat scope, not a
  process-local lock.

## 8.3 Idempotency

* Event IDs are deterministic from registration and delivery identity.
* WorkflowExecution IDs are deterministic from Event and Workflow version.
* TaskExecution attempts are deterministic from execution, Task, and attempt
  number.
* Artifact IDs are deterministic from producer and contract.
* Git branch and pull-request identities are deterministic from execution.
* Provider reconciliation checks branch, head, base, and existing PR before
  creation.

Exactly-once external effects are not promised. The contract promises no
duplicate identity and explicit ambiguity evidence.

## 8.4 Worker Lifecycle

```text
REGISTERING -> ACTIVE -> DRAINING -> OFFLINE
                   |
                   +-> UNHEALTHY -> OFFLINE
```

* A worker registers a stable ID, supported registrations, available
  capabilities (for example Docker and validation), and capacity.
* `ACTIVE` workers may claim dispatch rows for allowlisted registrations.
* `DRAINING` workers stop claiming new executions and finish their current
  leases.
* `UNHEALTHY` workers lose eligibility immediately; existing leases expire.
* `OFFLINE` workers have no claims and no leases.
* Worker registration does not grant repository credentials. Credentials are
  issued per execution lease and registration.

The control plane must not route a WorkflowExecution to a worker that cannot
provision its repository, run its validation profile, or reach its publication
boundary.

---

# 9. Failure And Recovery

| Failure | Detection | Recovery | Forbidden |
| --- | --- | --- | --- |
| Worker process crash | Lease heartbeat expires | New worker claims, re-provisions, retries recoverable work | Reusing unknown worktree state |
| Worker network partition | Lease renewal fails | Worker stops before next mutation; new owner after expiry | Publishing on a stale lease |
| Duplicate delivery | Dedup unique constraint | Return original Event | Creating a second execution |
| Duplicate dispatch claim | Compare-and-swap loser | Observe existing owner or queue position | Concurrent execution |
| Database outage | Transaction failure | Fail closed for admission and publication; retry when durable | In-memory-only progress |
| Object store outage | Put/get failure | Recoverable task failure; no metadata commit | Metadata pointing at missing bytes |
| Corrupt bytes | Digest mismatch | Quarantine; terminal integrity failure | Returning unverified content |
| Source cache corruption | Checkout verification | Rebuild cache at pinned revision | Using an unverified cache |
| Dirty worktree after failure | Worktree status | Retain on owning node for operator | Silent deletion |
| Lost node | Worker and lease expiry | Re-provision at recorded revision | Copying a live worktree across nodes |
| Partial Git push | Git evidence | Reconcile branch and head | Blind retry |
| Duplicate pull request | Provider lookup | Reuse existing PR | Creating another PR |
| Approval pending | Durable approval state | Release lease; wake a new owner after decision | Creating another execution |
| Registration suspended | Registration status | Reject new events; disable publication by default | Continuing publication |

---

# 10. Security And Isolation

* Workers authenticate with a stable service identity. The control plane
  authorizes registration, repository, and artifact scopes.
* Database credentials are least privilege. Workers cannot alter registrations
  or quota policy unless explicitly authorized.
* Object-store credentials are scoped by registration prefix. Signed URLs are
  short-lived and single-object.
* Artifact metadata and bytes are registration-scoped. Cross-registration reads
  are denied by default.
* Secrets are mounted only into the components that need them. They never enter
  runtime objects, logs, object metadata, or artifact bodies.
* Lease fences are authorization evidence, not just scheduling hints.
* A worker that loses its lease cannot publish even if provider credentials are
  still valid.
* Registration suspension can disable publication independently of provider
  availability.

---

# 11. Deployment Profiles

## 11.1 Local

* Durable JSON runtime store.
* Filesystem artifact store.
* One execution worker in one process.
* Local Docker socket.
* SQLite checkout registry.
* Process-local model admission with one replica.

This profile is for tests, development, and credential-free verification.

## 11.2 Shared

* Transactional runtime store.
* Shared object store.
* One or more execution workers.
* Central dispatch and lease service.
* Durable model admission.
* Registration-scoped artifact prefixes.

## 11.3 High Availability

* Database replication and tested failover.
* Object-store versioning and lifecycle policy.
* Worker pool with health checks and drain.
* Backup and restore for runtime objects, events, registrations, and artifact
  metadata.
* Alerting on lease expiry, queue age, artifact integrity failure, and
  publication ambiguity.

Proposed runtime inputs, subject to implementation:

```text
AEP_RUNTIME_STORE_MODE=transactional
AEP_DATABASE_URL_FILE=/run/secrets/database-url
AEP_ARTIFACT_STORE=s3
AEP_OBJECT_STORE_ENDPOINT=https://...
AEP_OBJECT_STORE_BUCKET=...
AEP_OBJECT_STORE_PREFIX=aep
AEP_WORKER_ID=worker-a
AEP_WORKER_REGISTRATIONS=registration-...
AEP_WORKER_MAX_EXECUTIONS=4
AEP_LEASE_TTL_SECONDS=120
AEP_HEARTBEAT_SECONDS=20
AEP_DISPATCH_VISIBILITY_SECONDS=300
```

Secrets remain references. Names are not a compatibility promise until the
implementation task is accepted.

---

# 12. Migration And Compatibility

1. Keep `RuntimeObjectStore`, `ContentAddressedStore`, and `CheckoutRegistry` as
   the provider-neutral boundaries.
2. Add the transactional adapter and schema migration tooling.
3. Add registration scope to runtime objects, events, deterministic keys,
   artifact metadata, checkout records, and queue rows.
4. Move artifact and evidence bytes behind the object-store adapter.
5. Replace outbox listing with claim and visibility timeout.
6. Add execution leases, worker identity, and fence checks.
7. Add durable model admission and remove the single-replica restriction.
8. Enable shared workers and validate failover, duplicate delivery, and
   publication ambiguity.
9. Update the inspection CLI to read the shared store without materializing
   artifact bodies.

Compatibility rules:

* Resource schemas and versioning do not change.
* Runtime object IDs remain stable.
* New runtime fields are additive.
* `workspacePath` becomes explicitly node-local.
* The local profile remains the default for tests.
* An operator can roll back to a single writer without losing committed
  evidence.

---

# 13. Observability And Operations

Required evidence and metrics:

* queue depth, oldest pending age, claim latency;
* lease acquisition, renewal, expiry, and fencing rejections;
* worker capacity, active executions, and drain state;
* per-registration dispatch and publication counts;
* artifact put/get latency, bytes, integrity failures, and orphan count;
* database transaction latency and failures;
* model admission reservations, deferrals, and quota rejections;
* publication ambiguity and reconciliation outcomes;
* backup age and restore verification.

Every lifecycle record carries `registrationId`, `workflowExecutionId`,
`taskExecutionId` when applicable, trace ID, worker ID, and lease fence. Logs
remain redacted by default.

---

# 14. Capacity And Limits

The first distributed implementation should declare:

* maximum executions per worker;
* maximum concurrent validation containers per node;
* maximum worktree and source-cache bytes per node;
* maximum artifact bytes per registration and retention window;
* maximum queue age before alerting;
* maximum lease TTL and heartbeat interval;
* maximum model request and token reservations per quota scope.

Limits are control-plane policy, not Agent choices. Exhaustion produces a stable
blocked or deferred reason.

---

# 15. Open Questions

* Is execution-affinity sufficient, or is Task-level distribution required for
  long-running validation?
* Should a node advertise repository affinity to avoid re-cloning large caches?
* Which object store is the first supported implementation?
* Is a separate broker required, or is database-backed claiming sufficient?
* How much artifact and evidence retention is required by default?
* Should a lost node's dirty worktree be recoverable from a periodic snapshot?
* How are model quota reservations shared across registrations?
* What is the operator workflow for restoring a shared database and object
  store to a consistent point in time?

---

# 16. Related Documents

* [ADR-006: Distributed Execution And Shared Artifacts](../adr/ADR-006-distributed-execution-and-shared-artifacts.md)
* [ADR-007: Repository Registration And Multi-Repository Isolation](../adr/ADR-007-repository-registration-and-multi-repository-isolation.md)
* [Deployment Architecture](deployment-architecture.md)
* [Workflow Runtime](workflow-runtime.md)
* [Repository Registration](repository-registration.md)
* [Observability](observability.md)
