# ADR-006: Distributed Execution And Shared Artifacts

**Status:** Proposed

**Authors:** Project Team

**Date:** 2026-10-27

---

# Context

The MVP runs one AEP workflow worker against one repository and one Workspace.
It deliberately uses local, durable adapters:

* `DurableJsonRuntimeObjectStore` checkpoints the complete runtime object graph
  to one `objects.json` file under a cross-process writer fence.
* `FilesystemContentAddressedStore` stores GeneratedArtifact bytes under
  `AEP_STATE_ROOT/artifacts/objects`.
* `SqliteCheckoutRegistry` records execution worktrees under the shared state
  root.
* Each execution receives an absolute worktree path on the machine that
  provisioned it.
* Docker validation uses the local Docker socket and a host path mapping.
* `ProcessLocalModelAdmissionCoordinator` paces provider requests inside one
  process, and startup rejects more than one configured Model worker replica.
* Webhook deduplication and reconciliation use a SQLite inbox/outbox on the
  state volume; pending rows are listed, not claimed.

These choices are correct for a single-host MVP. They do not form a
multi-machine execution system. Sharing a state directory over NFS or SMB does
not fix that: SQLite WAL, `flock`, directory sync, and atomic replace do not have
portable semantics across network filesystems, and a full-snapshot JSON writer
has no cross-node transaction or fencing contract.

The next capability is not "a shared folder". It is a distributed control plane
with explicit ownership of each execution, shared durable evidence, node-local
mutable worktrees, and fail-closed behavior on lease loss or storage failure.

This ADR defines the target contract. It does not change the current MVP
boundary, does not block the AEP-043 controlled pilot, and does not mark any
implementation task complete.

## Requirements

1. At most one worker owns a WorkflowExecution at a time.
2. Every accepted Event, lifecycle transition, artifact publication, and
   terminal outcome remains durably recorded.
3. Any authorized worker can read shared artifacts and evidence by immutable
   identity without depending on another worker's local filesystem.
4. Execution worktrees remain node-local. A worktree is never concurrently
   mutated by two machines.
5. Delivery, dispatch, and external effects are at-least-once at the transport
   layer and idempotent at the AEP contract layer.
6. The local, credential-free composition remains available for tests and
   development.
7. No new Agent capability or direct repository access is introduced.
8. The design composes with explicit repository registration (ADR-007).

## Non-Goals

* Active-active execution of the same WorkflowExecution on multiple workers.
* Task-level microdistribution in the first implementation.
* Cross-organization artifact sharing.
* Implicit artifact sharing between independent AEP control planes.
* Active-active cross-region control planes.
* Kubernetes-specific deployment mechanics.
* Replacing Git, GitHub, or provider-native idempotency with an AEP-owned copy.

---

# Decision

AEP will separate the control plane from replaceable execution workers and move
shared state and evidence behind transactional and object-storage boundaries.

## 1. Control Plane And Execution Workers

The target topology is:

```text
GitHub
  |
  v
Event Controller ---- transactional state ---- Object store
  |                        |                     (artifacts,
  v                        |                      logs, evidence)
Dispatch Queue ------------+
  |
  v
Execution Worker A --- local worktree --- local Docker
Execution Worker B --- local worktree --- local Docker
Execution Worker N --- local worktree --- local Docker
```

* The **control plane** owns ingress, registration routing, execution creation,
  dispatch, leases, inspection, and policy evidence.
* An **execution worker** owns one leased WorkflowExecution at a time, its
  node-local source cache and worktree, and its local Docker daemon.
* The **transactional store** owns runtime objects, events, deterministic keys,
  queue rows, leases, artifact metadata, registration records, and quota
  reservations.
* The **object store** owns immutable content-addressed bytes.
* A node never treats another node's worktree path as authoritative.

The separately addressable service names from the MVP remain logical
boundaries. Distribution is introduced behind the Workflow Runtime and Tool
Runtime boundaries, not by granting Agents network access to storage.

## 2. Transactional Runtime State

`RuntimeObjectStore` remains the contract. A new
`TransactionalRuntimeObjectStore` is the shared implementation. PostgreSQL is
the reference backend; another ACID database may be substituted if it preserves
the same guarantees.

The adapter must provide:

* one transaction for an object creation and its deterministic key;
* one transaction for a lifecycle transition, ownership attachment, and
  required ExecutionEvent;
* compare-and-swap status transitions;
* atomic dispatch claims and lease renewal;
* registration-scoped queries and uniqueness constraints;
* durable ordered indexes for execution inspection;
* no mutation of terminal evidence.

`DurableJsonRuntimeObjectStore` remains the local and test adapter. There is no
multi-writer JSON mode. A shared deployment never mounts `objects.json` across
machines.

## 3. Execution-Affinity Dispatch

The unit of distribution is the **WorkflowExecution**, not an individual Task.
A worker leases one execution and runs its scheduler and Task handlers locally
for the duration of that lease.

This preserves:

* one worktree owner for GeneratePatch and CreatePullRequest;
* one context identity and revision for the complete DAG;
* one approval and publication authority path;
* deterministic lifecycle ordering without remote Task RPC.

Task-level distribution is deferred. It requires per-Task input and output
materialization, remote Tool execution, and a second ownership protocol. It
must not be introduced by letting multiple workers race the existing scheduler.

A worker lease records `workerId`, `registrationId`, `executionId`,
`fenceToken`, `expiresAt`, and heartbeat time. Renewal is required before every
mutation. A worker that loses its lease must stop before any external effect.

## 4. Durable Dispatch Queue

Dispatch uses the transactional outbox pattern in the same database as runtime
state:

* Event acceptance commits the normalized Event and one outbox row.
* A dispatcher claims an outbox row with a bounded visibility timeout.
* An execution dispatch row is claimed by one worker with `FOR UPDATE SKIP
  LOCKED` or an equivalent compare-and-swap primitive.
* Attempts, lease expiry, and terminal failure are recorded.
* A row is marked complete only in the same transaction that commits terminal
  execution evidence.

A separate broker may be added for throughput, but only through the outbox
pattern. A dual write to a broker and the runtime store without a recoverable
outbox is rejected.

## 5. Shared Artifacts And Evidence

Artifact and evidence storage is split into metadata and bytes:

* **Metadata** (`GeneratedArtifact`, provenance, TaskExecution binding,
  content address, media type) lives in the transactional store.
* **Bytes** live in a shared, immutable, content-addressed object store behind
  `ContentAddressedStore`. S3-compatible storage is the reference
  implementation.
* Tool command logs, Docker logs, Model input/output evidence, and validation
  reports use the same content-addressed evidence boundary where their current
  contracts expose a content address.
* Bytes are written before metadata is committed. An interrupted write leaves
  an unreferenced object, not a metadata record pointing at missing content.
* Reads verify the content digest. A mismatch is a terminal integrity failure,
  not a retryable read error.
* Object keys are scoped by deployment and registration. Cross-registration
  deduplication is disabled by default so object existence is not a cross-repo
  side channel. Within one registration, identical bytes deduplicate.
* Orphaned objects are removed by a bounded garbage collector only after a
  configurable grace period and only when no metadata record references them.
* Retention and legal-hold policy is configuration, not an Agent decision.

Content addressing is not provenance. A worker may read bytes only after the
transactional catalog confirms the artifact's registration, repository
revision, producer TaskExecution, and workflow lineage.

The artifact catalog is owned by one AEP control plane. Two independent
deployments may use the same object-store product, but they do not share
catalog authority. Moving artifacts between independent deployments requires an
explicit export/import operation that records new provenance; it is never an
implicit read.

## 6. Node-Local Worktrees

Source caches and execution worktrees are node-local mutable state:

* The central checkout registry owns execution identity, repository identity,
  branch, fence token, lease, and state.
* The worktree path is recorded as node-local evidence and is never used as
  cross-node authority.
* Failover re-provisions a worktree from the pinned source revision and the
  recorded execution branch on the new owner. It does not copy a live worktree
  between machines.
* Dirty worktrees on a live node are retained for operator recovery. A lost
  node is a recoverable loss of uncommitted work, not a reason to reuse an
  unknown working tree.
* Branch uniqueness is enforced per registration and repository.
* Cleanup requires terminal execution evidence and verifies worktree identity
  before removal.

The existing execution checkout manager and fencing-token model are retained.
The registry backend becomes transactional and node-aware instead of assuming
one local SQLite file.

## 7. Distributed Model Admission

Provider admission becomes a durable, shared reservation service keyed by
provider endpoint and credential scope:

* request and token reservations are transactional;
* reservations expire and are reclaimed with fencing;
* worker restarts restore active deadlines;
* provider throttle state is rechecked before dispatch;
* usage reconciles against the owning reservation only.

Until this exists, the current `AEP_MODEL_WORKER_REPLICAS=1` restriction remains
authoritative. Running multiple workers with per-process coordinators is not an
acceptable workaround.

## 8. Publication Ownership

Only the current execution owner may perform `git.push` or
`github.create_pr`.

* The publication path rechecks the lease fence immediately before mutation.
* A lost lease before mutation aborts with no remote effect.
* A lost lease after an ambiguous mutation records `UNKNOWN` and requires
  provider reconciliation before retry.
* Duplicate pull-request reconciliation remains authoritative.
* Terminal evidence is committed before the dispatch row is retired.

Exactly-once external effects are not promised. Idempotent external identity and
explicit ambiguity evidence are.

## 9. Deployment Profiles

| Profile | State | Artifacts | Workers | Purpose |
| --- | --- | --- | --- | --- |
| `local` | Durable JSON | Local filesystem | One process | Tests and credential-free development |
| `shared` | Transactional database | Shared object store | One or more workers | Multi-machine execution |
| `ha` | Transactional database with replication/backup | Shared object store with versioning | Worker pool with leases | Operational high availability |

All profiles use the same Resource and runtime-object schemas. Profile selection
changes adapters, not workflow semantics.

---

# State And Ownership

| State | Owner | Backend | Portable across nodes |
| --- | --- | --- | --- |
| Resources and Workspace | Git; pinned read-only checkout | Git object store / local checkout | Yes, by revision |
| Runtime objects and ExecutionEvents | Control plane | Transactional database | Yes |
| Deterministic keys and claims | Control plane | Transactional database | Yes |
| Dispatch outbox and queue | Control plane | Transactional database | Yes |
| Execution and checkout leases | Control plane | Transactional database | Yes |
| Artifact and evidence metadata | Control plane | Transactional database | Yes |
| Artifact and evidence bytes | Object store | S3-compatible store | Yes |
| Source cache | Execution node | Local filesystem | No |
| Execution worktree | Execution node | Local filesystem | No |
| Docker daemon and validation containers | Execution node | Local runtime | No |
| Model provider credentials | Execution node secret mount | Secret store / file | No |
| Repository and pull-request truth | GitHub and Git | External provider | Yes, by provider identity |

---

# Execution Flow

1. GitHub delivers an issue event to the registered webhook route.
2. Ingress verifies the signature, delivery identity, event type, and
   registration, then commits the Event and one outbox row in one transaction.
3. The dispatcher claims the outbox row and creates or claims one idempotent
   WorkflowExecution at an immutable repository revision.
4. A dispatch row carries the execution identity, registration, and required
   capabilities.
5. A worker with a matching capability set claims the execution lease.
6. The worker verifies the recorded revision and provisions a local source cache
   and worktree.
7. The worker runs the scheduler and the six Task handlers. Every state change
   commits through the transactional store with the lease fence.
8. GeneratedArtifact bytes are written to the object store, then metadata and
   evaluation evidence are committed.
9. CreatePullRequest rechecks policy, ownership, and lease before Git and GitHub
   mutations.
10. Terminal evidence and dispatch completion commit together.
11. Cleanup removes the clean worktree; dirty or failed cleanup remains recorded
    for the owning node's operator.

---

# Failure And Recovery

| Failure | Required behavior |
| --- | --- |
| Worker crash during a Task | Lease expires. A new worker re-provisions from the recorded revision and retries only recoverable work. Side effects in `UNKNOWN` state require reconciliation. |
| Worker loses lease but remains alive | Fence check rejects every subsequent write and external effect. The worker exits the execution. |
| Duplicate webhook delivery | Deduplication by registration and delivery ID returns the original Event; no new execution. |
| Duplicate dispatch claim | Compare-and-swap allows one owner; the loser observes the existing owner and does not execute. |
| Database unavailable | No new admission or dispatch. Running workers fail closed before publication if durable evidence cannot be written. |
| Object store unavailable | Artifact publication fails recoverably. No metadata record points at missing content. |
| Corrupt artifact bytes | Digest verification fails terminally; the artifact is quarantined and the execution cannot publish. |
| Node-local worktree lost | The execution is re-provisioned at the recorded revision. Uncommitted work is lost and must be regenerated. |
| Partial Git push or pull request | Provider reconciliation uses branch, head, and PR identity before any retry. |
| Approval pending | The execution lease may be released. The approval is durable and wakes a new eligible worker without creating a second execution. |
| Registration suspended | New events are rejected. In-flight publication follows the registration's suspension policy, which defaults to no new external effects. |

---

# Security And Isolation

* Workers authenticate to the control plane and present a stable worker
  identity. Lease rows bind execution, worker, registration, and fence.
* Database and object-store credentials are service-scoped. A worker receives
  only the registration and artifact scopes it is executing.
* Artifact metadata and bytes are registration-scoped. Cross-registration reads
  are denied by default; any future import is an explicit operator action.
* Secrets, prompts, event bodies, and artifact bodies remain excluded from
  lifecycle logs. Content addresses may be logged; content may not.
* Object storage uses encryption at rest, TLS in transit, and bounded signed
  access if direct worker access is used.
* A stale worker cannot publish even if it still has valid provider credentials;
  the control-plane lease fence is authoritative.
* Repository credentials remain short-lived and scoped to the registered
  repository.

---

# Migration

1. Extract the runtime-store, artifact-store, queue, and lease contracts that
   already exist behind interfaces.
2. Add the transactional adapter and schema migrations while retaining the JSON
   adapter for local development and tests.
3. Add registration scope (`registrationId`) to runtime objects, events,
   deterministic keys, and artifact metadata. Backfill the existing deployment
   as its first registration.
4. Move artifact, log, and evidence bytes behind a shared object-store adapter.
   Keep the filesystem adapter for local use.
5. Replace outbox listing with claim, visibility timeout, and completion in the
   terminal transaction.
6. Add execution leases, worker identity, and fence checks to every mutation.
7. Add durable model admission. Only then allow more than one Model worker.
8. Enable worker pools with the shared profile.
9. Remove the single-host assumptions from operators' runbooks while keeping a
   supported single-host profile.

Runtime object IDs and Resource versions remain stable. Schema changes are
additive. `workspacePath` becomes explicitly node-local evidence rather than a
portable execution key.

---

# Schema And Contract Impact

* Runtime objects gain a `registrationId` scope. Existing single-repository
  evidence receives a default registration during migration.
* New control-plane records represent dispatch items, execution leases, worker
  identity, and quota reservations. They are not declarative Resources and are
  not visible to Agents.
* `GeneratedArtifact` keeps its identity, content address, and provenance
  fields. Registration scope and artifact-catalog lookup are additive.
* `ExecutionCheckout` keeps its execution, repository, revision, branch, and
  fence fields. Worker identity and lease ownership become explicit;
  `workspacePath` is documented as node-local evidence.
* Resource and Agent contracts do not change. Model providers remain Model
  resources, not Tools.
* `RuntimeObjectStore`, `ContentAddressedStore`, and `CheckoutRegistry` remain
  the provider-neutral boundaries. The distributed profile adds adapters, not a
  second workflow model.

---

# Consequences

## Benefits

* A WorkflowExecution has one explicit owner and can fail over without two
  workers mutating one worktree.
* Shared artifacts become usable by any authorized worker without copying
  another node's filesystem.
* The control plane can scale workers independently of ingress and inspection.
* The local profile remains available, so tests do not require a database or
  object store.
* Registration isolation (ADR-007) composes with artifact and execution
  scoping.

## Trade-offs

* A database, object store, queue semantics, lease protocol, and migration
  tooling become operational dependencies.
* Execution-affinity limits work stealing to whole executions. A long execution
  cannot be split across idle workers.
* Node loss loses uncommitted work. Model-backed Tasks may produce different
  output on the replacement worker.
* Artifact garbage collection and retention become shared-storage concerns.
* The number of runtime schema fields and indexes grows.

## Risks

* A lease implementation with a clock or fencing defect could allow two owners.
* Object-store consistency and signed-URL authorization could expose artifacts
  across registrations if scoping is wrong.
* A shared database becomes a single point of failure unless backup, restore,
  and failover are operated deliberately.
* Moving work across nodes changes model and tool latency and may expose
  previously hidden provider variability.

---

# Rejected Alternatives

## Mount `AEP_STATE_ROOT` On A Shared Network Filesystem

Rejected because SQLite WAL, `flock`, directory sync, and atomic-replace
semantics are not portable across NFS/SMB. A shared directory also provides no
execution ownership and no cross-node fencing.

## Allow Multiple Writers To The JSON Runtime Store

Rejected because a full-snapshot writer cannot provide transactional object,
event, key, and claim updates across machines. The current writer fence is a
single-host safety mechanism, not a distributed lock.

## Distribute Individual Tasks First

Rejected for the first increment because GeneratePatch and CreatePullRequest
depend on one worktree and one publication authority. Task-level distribution
would multiply ownership and evidence problems before execution-level
distribution is proven.

## Share Execution Worktrees Between Workers

Rejected because concurrent mutation of one working tree is not safe, and a
worktree path is not a durable identity. Execution identity and repository
revision are durable; the path is node-local.

## Store Artifact Bytes Without A Shared Catalog

Rejected because content addressing proves bytes, not authority. Artifact
authorization, provenance, retention, and lineage require metadata in the
transactional store.

## Use A Separate Message Broker With A Dual Write

Rejected unless the broker dispatch is fed by a recoverable outbox. A dual write
can lose an event or dispatch an execution whose creation did not commit.

## Make Workers Stateless

Rejected as a description of the first implementation. Workers hold leases and
node-local worktrees. They are replaceable, not stateless.

---

# Open Questions

* Should a failed long-running execution be resumed from Task checkpoints or
  replayed from the last successful Task boundary?
* Does high-throughput image validation require a shared build cache, or is
  per-node caching sufficient?
* Which object-store provider is the supported reference implementation?
* Is database-native queueing sufficient, or is a broker required above a
  documented throughput threshold?
* How long should orphaned artifact bytes and superseded registrations be
  retained?
* Are Model quota reservations shared across registrations that use the same
  provider credential?
* Should approval wake-up dispatch a new worker lease immediately or wait for a
  bounded scheduler interval?
