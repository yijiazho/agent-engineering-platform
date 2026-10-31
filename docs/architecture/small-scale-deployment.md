# Small-Scale Deployment

**Project:** AI Agent Engineering Platform (AEP)

**Document:** Small-Scale Deployment

**Status:** Draft

**Version:** 0.1

---

# 1. Overview

This document specifies the first production-like AEP deployment:

* one Hetzner Cloud host in one region;
* PostgreSQL for transactional runtime state;
* Cloudflare R2 for artifacts, evidence, logs, and PostgreSQL backups;
* Cloudflare Tunnel for GitHub webhook ingress;
* Docker Compose for physical composition;
* one execution worker with a local Docker daemon.

It implements the first profile of ADR-008 and uses the contracts from ADR-006
and ADR-007. It is a design; the current repository still runs the JSON,
filesystem, and SQLite adapters.

The profile is deliberately not highly available. It is designed to lose a host
without losing accepted evidence, within a documented recovery point.

## 1.1 Goals

* Durable shared artifacts and evidence independent of one worker disk.
* Transactional runtime state and dispatch that can survive a second worker.
* HTTPS webhook ingress without a public origin port.
* Docker validation on a host controlled by AEP.
* Predictable small monthly cost.
* A clear path to a second worker and additional repositories without changing
  the storage contracts.

## 1.2 Non-Goals

* Multi-region or active-active operation.
* Zero-downtime upgrades.
* Kubernetes.
* Multi-tenant SaaS authorization.
* Managed container execution for the validation worker.
* Replacing PostgreSQL with an object store or event log.

---

# 2. Topology

```text
GitHub App webhook
        |
        v
Cloudflare edge (TLS, Tunnel)
        |
        v
+-------------------------------------------------------------+
| Hetzner Cloud private network                               |
|                                                             |
|  +-------------------------------------------------------+  |
|  | aep-core (CPX22 or CPX32)                             |  |
|  |                                                       |  |
|  |  cloudflared                                          |  |
|  |  event-controller                                     |  |
|  |  control plane (dispatch, registration, inspection)   |  |
|  |  publication service                                  |  |
|  |  execution worker (Docker socket)                     |  |
|  |  postgres                                             |  |
|  |  backup job (pgBackRest / WAL-G)                      |  |
|  |                                                       |  |
|  |  /srv/aep/postgres      -> Hetzner Volume             |  |
|  |  /srv/aep/state         -> local disk                 |  |
|  |  /srv/aep/worktrees     -> local disk                 |  |
|  |  /srv/aep/cache         -> local disk                 |  |
|  +--------------------------+----------------------------+  |
|                             |                               |
+-----------------------------|-------------------------------+
                              |
                              v
                   Cloudflare R2 (private)
                     artifacts / evidence
                     logs / postgres backups
```

A future worker joins the same private network:

```text
aep-worker-1 (Docker) ---- private network ---- aep-core
                                                  |
                                                  +-- PostgreSQL
                                                  +-- R2 over HTTPS
```

Workers never share a worktree. They share the database, artifact catalog, and
R2 object namespace.

---

# 3. Hetzner Host And Network

## 3.1 Location

Default to an EU location (Falkenstein or Helsinki) unless:

* the primary repository or operators are US-based;
* model provider latency is materially better in a US region;
* data residency requires a specific jurisdiction.

One region only. Do not span locations in phase 1.

## 3.2 Host Sizing

| Workload | Reference host | Notes |
| --- | --- | --- |
| Low concurrency, one validation at a time | CPX22-class, 2 vCPU / 4 GB / 80 GB | Minimum practical |
| Concurrent validation or larger builds | CPX32-class, 4 vCPU / 8 GB / 160 GB | Recommended if memory pressure appears |
| Future worker | CPX22 or CPX32 | No database |
| Future dedicated database | CPX22 + volume | Only when measured |

PostgreSQL and Docker builds compete for memory. A 4 GB host with one 1 GB
validation container, the worker, and PostgreSQL is tight but workable. Start at
4 GB only if concurrency is capped at one validation.

## 3.3 Storage Layout

| Path | Backing | Purpose |
| --- | --- | --- |
| `/srv/aep/postgres` | Hetzner Volume | PostgreSQL data directory |
| `/srv/aep/state` | Local disk | Runtime service state that is not in PostgreSQL |
| `/srv/aep/worktrees` | Local disk | Execution worktrees |
| `/srv/aep/cache` | Local disk | Bare source cache |
| `/srv/aep/logs` | Local disk | Local structured logs before archive |
| `/etc/aep/secrets` | Local disk, root-only | Mounted secret files |

The PostgreSQL volume is the only state that must survive host replacement. The
worktree and cache are rebuildable. Logs are archived to R2.

## 3.4 Firewall And Network

Inbound:

* SSH only from operator addresses;
* no public 80/443;
* no public PostgreSQL, Docker, or worker ports.

Outbound:

* HTTPS to GitHub, OpenAI, Cloudflare R2, and Cloudflare Tunnel;
* outbound TCP/UDP 7844 for Cloudflare Tunnel;
* DNS;
* package repositories during provisioning or controlled upgrades.

Private network:

* control plane to PostgreSQL;
* worker to PostgreSQL;
* worker to control plane for publication requests;
* no database exposure to the public internet.

Cloudflare Tunnel uses outbound connections; no inbound tunnel port is required.

---

# 4. Container Composition

Phase 1 uses Docker Compose with these roles:

| Service | Image | Published port | Volumes | Notes |
| --- | --- | --- | --- | --- |
| `cloudflared` | pinned Cloudflare image | none | tunnel token | Outbound tunnel |
| `event-controller` | AEP image digest | loopback only | Resource checkout read-only, webhook secret | Webhook ingress |
| `control-plane` | AEP image digest | loopback only | Resource checkout, state, secrets | Dispatch, registration, inspection |
| `publication` | AEP image digest | private only | Resource checkout, publication secrets, cache | Owns Git/GitHub mutation credentials |
| `worker` | AEP image digest | none | Docker socket, worktrees, cache, state, model/source secrets | Runs Task handlers |
| `postgres` | pinned PostgreSQL image | private only | `/srv/aep/postgres` | Runtime state |
| `backup` | pgBackRest/WAL-G image | none | PostgreSQL data, R2 credentials | Scheduled backup |

The seven logical service boundaries remain. Phase 1 physically co-locates the
non-scaling control-plane services. The execution worker is a separate process
from the publication service so publication credentials do not enter the worker.

## 4.1 Docker Socket

Only the `worker` mounts `/var/run/docker.sock`. The socket is root-equivalent.
The worker host must be treated as a privileged trust boundary.

Validation containers:

* use a digest-pinned image;
* run with networking disabled;
* have CPU, memory, and timeout limits;
* mount only the execution worktree;
* never receive repository credentials or model credentials.

If a second repository is untrusted, give it a separate worker host rather than
sharing one Docker daemon.

---

# 5. PostgreSQL Design

## 5.1 Version And Extensions

* Pin a supported PostgreSQL major version.
* Use the distribution or managed-provider security updates.
* Required extensions: none beyond core. `pg_stat_statements` is optional for
  diagnostics; `pgcrypto` is optional if database-generated identifiers are
  introduced.
* Do not use unlogged tables for runtime evidence.

## 5.2 Roles

| Role | Purpose | Privileges |
| --- | --- | --- |
| `aep_migrator` | Schema migrations | DDL on the AEP schema |
| `aep_app` | Control plane and workers | DML on runtime tables, no DDL |
| `aep_readonly` | Inspection CLI / reporting | Read-only views or tables |
| `aep_backup` | pgBackRest/WAL-G | Replication or backup privileges only |

Application roles use `scram-sha-256`. Local Unix-socket access is restricted to
the database container. Remote access requires TLS.

## 5.3 Schema Sketch

All tables carry `registration_id` where the object is registration-scoped.

```text
registrations
  registration_id, generation, repository_id, workspace_ref,
  resource_revision, status, state_namespace, created_at, updated_at

runtime_objects
  id, registration_id, kind, payload jsonb,
  workflow_execution_id, task_execution_id, status,
  repository_revision, created_at, updated_at

execution_events
  id, registration_id, workflow_execution_id, task_execution_id,
  sequence, event_type, payload jsonb, emitted_at

deterministic_keys
  registration_id, key, object_id, created_at

dispatch_queue
  id, registration_id, execution_id, status,
  available_at, locked_by, locked_until, attempts, last_error

execution_leases
  execution_id, worker_id, fence_token, expires_at, heartbeat_at

publication_attempts
  id, execution_id, registration_id, state, patch_digest,
  idempotency_key, provider_identity, created_at, updated_at

artifact_catalog
  artifact_id, registration_id, content_address, media_type,
  producer_task_execution_id, workflow_execution_id,
  repository_revision, created_at, retention_until

content_reservations
  registration_id, digest, state, reservation_id, expires_at

quota_reservations
  scope, reservation_id, requests, tokens, expires_at
```

Design rules:

* Runtime payloads are JSONB; frequently queried identity, status, revision, and
  lineage fields are generated or duplicated into indexed columns.
* ExecutionEvents are append-only. Updates are prohibited by the application and
  by database permissions where practical.
* Unique constraints enforce `(registration_id, deterministic_key)`,
  `(registration_id, repository_id, branch)`, and artifact identity.
* Terminal status transitions use compare-and-swap predicates in one
  transaction.
* The dispatch queue uses `FOR UPDATE SKIP LOCKED`, a visibility timeout, an
  attempt count, and a dead-letter state.
* Execution leases carry a monotonically increasing fence token.
* Registration-scoped queries are mandatory. Row-level security is optional in
  phase 1; application predicates and tests remain authoritative.

## 5.4 Connections

Start with an application pool of 5-10 connections per process. PostgreSQL's
default connection limit is sufficient for one control plane, one publication
service, and one worker.

Add PgBouncer only when worker count or connection churn makes direct pooling
insufficient. Do not put PgBouncer in front of migrations without transaction
mode review.

## 5.5 Migrations

* Migrations are forward-only, versioned, and checked into the AEP image.
* A one-shot migration container runs before the control plane and worker start.
* A backup is taken before a migration.
* Schema changes are additive where possible.
* Destructive changes require an explicit two-release deprecation.
* The migration role is not available to the running application.

## 5.6 Backup And Restore

Use pgBackRest or WAL-G:

* full backup on a schedule (daily or every 12 hours);
* continuous WAL archiving to R2;
* retention: 14 days WAL, 30 days full backups for phase 1;
* encryption before upload; keys stored outside R2;
* restore test at least monthly;
* restore timing recorded as an operational metric.

Target:

* RPO: 15 minutes or better with WAL archiving.
* RTO: 2 hours for host replacement and database restore.

Hetzner snapshots are supplementary and do not replace database-consistent
backups.

---

# 6. Cloudflare R2 Design

## 6.1 Buckets And Prefixes

Two private buckets:

```text
aep-prod-artifacts/
  artifacts/<registrationId>/sha256/<first-two>/<remaining-hex>
  evidence/<registrationId>/<workflowExecutionId>/...
  logs/<service>/<date>/...

aep-prod-backups/
  postgres/<deployment>/...
```

The split lets the backup writer use a token that cannot read or mutate runtime
artifacts. A separate artifact bucket per registration is a future option for
hard tenant isolation; prefix isolation is application-enforced.

## 6.2 Access

Separate API tokens:

| Token | Bucket scope | Operation |
| --- | --- | --- |
| `aep-worker-rw` | artifacts | Read/write; application enforces registration prefix |
| `aep-control-rw` | artifacts | Read/write; catalog and lifecycle support |
| `aep-publication-ro` | artifacts | Read for publication reconstruction |
| `aep-backup-rw` | backups | Read/write |
| `aep-inspection-ro` | artifacts | Optional read-only export |

R2 tokens can be scoped to a bucket and operation, not to a key prefix. The
application and database catalog enforce registration prefixes. Use separate
buckets when hard tenant isolation is required.

No token has account administration, bucket deletion, or cross-environment
access.

## 6.3 Immutability And Lifecycle

* Object keys are content-addressed; writes are immutable and idempotent.
* Bucket versioning is enabled on the artifact/evidence and backup buckets.
* Lifecycle rules move old artifacts/logs to cheaper storage or expire them
  after the retention window.
* Object Lock or compliance retention is optional; enable it only after the
  operator retention policy is defined.
* Garbage collection is catalog-driven. A bucket-wide delete or lifecycle rule
  must not remove referenced artifacts.
* Content reservations and collection tombstones from ADR-006 prevent the
  publication/GC race.

## 6.4 R2 Is Not Runtime State

R2 does not hold:

* workflow status;
* queue rows;
* leases;
* registration state;
* authoritative artifact authorization.

Those live in PostgreSQL. R2 stores bytes and backup objects only.

## 6.5 Backup Of Artifact Evidence

R2 versioning plus object lock/retention is the first protection. A periodic
inventory export to PostgreSQL records artifact IDs, digests, sizes, and
retention state. Cross-bucket replication is a phase-2 decision.

---

# 7. Ingress And Cloudflare Tunnel

## 7.1 Tunnel

A `cloudflared` container runs with a tunnel token. Cloudflare DNS routes:

```text
aep.example.com -> /v1/webhooks/github
```

Only the webhook path is public. The origin publishes no inbound port.

If operator inspection is exposed through Cloudflare, protect it with
Cloudflare Access and a separate hostname. The webhook path must bypass Access
so GitHub can deliver.

## 7.2 Webhook Contract

* GitHub sends `X-Hub-Signature-256`, `X-GitHub-Event`, and
  `X-GitHub-Delivery`.
* Cloudflare must not transform the request body.
* AEP verifies HMAC-SHA256 over the raw body before JSON decoding.
* The repository identity is checked after signature verification.
* Deduplication uses stable repository lineage plus delivery ID.
* Replays return the original Event and registration generation.

## 7.3 Failure Behavior

| Failure | Behavior |
| --- | --- |
| Cloudflare edge/Tunnel down | GitHub retries; no Event is accepted |
| Origin down | Cloudflare returns an error; GitHub retries |
| Event Controller down | Tunnel returns 502/503; GitHub retries |
| PostgreSQL unavailable | Ingress fails closed; no Event is accepted |
| R2 unavailable | Existing control-plane work may continue; artifact writes fail recoverably |

No webhook response claims acceptance before the Event and outbox row commit.

---

# 8. Secrets And Identity

## 8.1 Secret Files

Mounted read-only, root-owned, mode 0400/0440:

```text
github-app-private-key.pem
github-webhook-secret
openai-api-key
r2-access-key-id
r2-secret-access-key
postgres-app-password
postgres-migration-password
cloudflare-tunnel-token
```

The publication service receives GitHub App and Git credentials. The worker
receives source-fetch credentials only. The worker does not receive publication
credentials.

## 8.2 Rotation

| Secret | Rotation |
| --- | --- |
| GitHub App private key | Per provider guidance; overlap, deploy, revoke |
| Webhook secret | Overlap only if the provider supports two secrets; otherwise controlled downtime |
| OpenAI key | On schedule or incident |
| R2 token | Per role, on schedule or incident |
| PostgreSQL passwords | On schedule; coordinated restart |
| Tunnel token | On incident or provider recommendation |

Rotation evidence records the secret reference and time, never the value.

---

# 9. Execution And Artifact Flow

1. GitHub delivers a signed issue event through Cloudflare Tunnel.
2. Event Controller verifies the signature and repository registration.
3. It commits the Event, deduplication key, and outbox row in PostgreSQL.
4. The dispatcher claims the outbox row and creates one idempotent
   WorkflowExecution.
5. The worker claims an execution lease and provisions a local source cache and
   worktree.
6. If GeneratePatch already succeeded, the worker reconstructs the accepted
   patch from R2 and verifies its digest and postimages.
7. Tasks run. Context Builder reads Resources, prior artifacts, and repository
   evidence.
8. GeneratedArtifact bytes are written to R2, then metadata is committed to
   PostgreSQL.
9. CreatePullRequest submits a publication request. The publication service
   verifies the lease, reconstructs the patch in a clean worktree, and performs
   Git/GitHub mutations with its own credentials.
10. Terminal evidence commits and the dispatch row is retired.
11. Logs and evidence are archived to R2; local worktrees are cleaned.

No worker-local path is treated as durable evidence. The PATCH artifact and the
PostgreSQL provenance are authoritative.

---

# 10. Failure And Recovery

| Failure | Impact | Recovery |
| --- | --- | --- |
| Worker crash | Lease expires; execution stalls | New worker lease; reconstruct patch; retry recoverable work |
| `aep-core` host loss | Control plane, database, and worker unavailable | Rebuild host, attach/restore volume, restore PostgreSQL from R2, start Compose |
| PostgreSQL corruption | Runtime state unavailable | Restore full backup plus WAL to target time |
| PostgreSQL volume loss | Runtime state lost | Restore from R2 backup; RPO applies |
| R2 outage | Artifact writes/reads fail | Retry; do not publish without artifacts |
| Cloudflare Tunnel outage | Webhooks fail | GitHub retries; no data loss |
| Docker daemon failure | Validation fails | Restart worker host; execution retries or fails closed |
| Disk full | Writes fail | Alert; prune logs/cache; expand volume |
| Accidental artifact deletion | Evidence missing | Restore from R2 versioning; verify catalog |
| Secret compromise | Provider access risk | Rotate secret, revoke old credential, audit evidence |

## 10.1 Host Replacement Runbook Outline

1. Provision a replacement Hetzner server with the same private network.
2. Attach or recreate the PostgreSQL volume.
3. Install Docker and restore the Compose deployment.
4. Restore PostgreSQL from the latest full backup and WAL to the target time.
5. Restore secrets from the secure backup.
6. Start PostgreSQL, run migrations only if the image requires them, then start
   the control plane, publication service, and worker.
7. Verify `/healthz`, registration identity, resource revision, and one
   read-only execution inspection.
8. Re-enable ingress and confirm a signed fixture delivery.
9. Record RTO, RPO, and any evidence gap.

---

# 11. Deployment And Operations

## 11.1 Provisioning

Use Terraform or Hetzner Cloud CLI plus cloud-init. Provision:

* server;
* private network;
* firewall;
* volume;
* DNS record and Tunnel route;
* optional backups.

The repository should eventually contain `deploy/hetzner/` with Terraform,
Compose, and `.env.example`. This document does not add those files.

## 11.2 First Boot

1. Install Docker Engine and Compose plugin.
2. Create `/srv/aep` and `/etc/aep/secrets`.
3. Mount the PostgreSQL volume.
4. Load secret files with restrictive permissions.
5. Pull pinned image digests.
6. Run the migration job.
7. Start PostgreSQL, control plane, publication service, worker, and
   `cloudflared`.
8. Verify health and registration identity.
9. Configure the GitHub App webhook.
10. Send a signed fixture delivery and verify deduplication.
11. Run one controlled issue and inspect the resulting evidence.

## 11.3 Upgrades

1. Take a backup.
2. Pull the new pinned image digest.
3. Review and run additive migrations.
4. Stop the control plane, publication service, and worker.
5. Start the new versions.
6. Verify health, registration identity, and one execution.
7. Roll back the image if verification fails. Database rollback is limited to
   additive migrations; destructive changes require a restore.

Single-host upgrades have a short ingress outage. Accept it in phase 1 or drain
webhooks at GitHub/Cloudflare before upgrading.

## 11.4 Monitoring

Minimum:

* host CPU, memory, disk, and volume usage;
* PostgreSQL up, connections, replication/backup age, disk;
* queue depth and oldest pending age;
* worker heartbeat and lease expiry;
* publication attempts in `UNKNOWN`;
* R2 write/read errors;
* webhook 4xx/5xx rates;
* health endpoint failures;
* model budget and rate-limit deferrals.

Logs are JSON, redacted, rotated locally, and archived to R2. Add Grafana Cloud,
Better Stack, or a self-hosted Prometheus stack only when the basic alerts are
not enough.

## 11.5 Backup Verification

Monthly:

* restore the latest full backup and WAL into a temporary PostgreSQL instance;
* verify a known WorkflowExecution and artifact metadata;
* fetch one artifact from R2 and verify its digest;
* record restore duration and any gap;
* delete the temporary instance.

## 11.6 Implementation Slices

The deployment should be implemented in reviewable slices:

1. **AEP-073, transactional store:** add the PostgreSQL adapter and migrations
   behind `RuntimeObjectStore`; keep the JSON adapter for local tests. Verify all
   existing runtime-store, scheduler, and CLI tests against both adapters.
2. **AEP-074, object store:** add the S3-compatible `ContentAddressedStore`
   adapter, digest reservations, and catalog integration. Keep the filesystem
   adapter for local tests.
3. **AEP-075, execution ownership:** add dispatch claims, execution leases,
   worker identity, and lease fencing.
4. **AEP-076, publication:** move credential-owning publication and accepted-
   patch reconstruction behind the publication service.
5. **AEP-077, Hetzner packaging:** add Terraform/cloud-init, Compose, secrets
   layout, Cloudflare Tunnel configuration, and backup jobs under
   `deploy/hetzner/`.
6. **AEP-078, recovery verification:** restore, host replacement, worker loss,
   R2/Tunnel outage, and RPO/RTO evidence.
7. **Scale-out:** add a second worker, move PostgreSQL off the worker host or to
   a managed service, and exercise patch reconstruction and failover.
8. **AEP-079 and AEP-080, registration:** enable additional repositories only
   after the single-registration profile is stable.

No slice should change Resource or runtime-object semantics.

---

# 12. Scaling Path

## 12.1 Phase 1

One `aep-core` host, one worker, self-managed PostgreSQL, R2, Tunnel.

## 12.2 Phase 2

Add `aep-worker-1` on the private network. Move PostgreSQL to a dedicated host or
Hetzner Managed PostgreSQL. Keep one control plane.

## 12.3 Phase 3

Separate the control plane and publication service; add worker capacity; add
PostgreSQL HA if the availability target requires it.

## 12.4 Phase 4

Kubernetes only if:

* worker count and operational complexity exceed what Compose and systemd can
  manage;
* there is a platform team;
* multi-region or tenant isolation requires it.

Adding a worker must not introduce shared worktrees. Each worker uses its own
source cache and worktrees and reconstructs patches from R2.

---

# 13. Cost Envelope

At 100 executions per month and one host:

| Item | Approx. monthly |
| --- | ---: |
| Hetzner CPX22 | EUR 20-25 |
| 100 GB volume | EUR 4-6 |
| IPv4/backups | EUR 1-5 |
| R2 (50-100 GB) | USD 1-5 |
| Cloudflare Tunnel/DNS | USD 0 |
| GitHub Actions/GHCR | USD 0-10 |
| Observability (optional) | USD 0-20 |
| LLM (100 executions) | USD 15-40 |
| **Total** | **USD/EUR 45-140** |

At 500 executions per month, expect LLM cost of USD 75-200 and a likely second
worker or larger host. At 2,000 executions per month, expect USD 300-800 in LLM
usage and a managed database or dedicated database host.

Verify Hetzner and R2 list prices at purchase; both have changed during 2026.

---

# 14. Open Questions

* Is Hetzner Managed PostgreSQL available in the chosen location with the
  required TLS, backup, and restore guarantees?
* Is 4 GB RAM sufficient with one validation container, or should the first host
  start at 8 GB?
* Should PostgreSQL use the Hetzner private network from day one, even on the
  same host?
* Should the publication service be a separate container in phase 1?
* Which R2 jurisdiction and retention policy apply?
* Is Cloudflare Access required for inspection, or is SSH plus the CLI enough?
* How often must the restore test run to satisfy the operator?
* Does the first repository need a different validation image or offline
  dependency set?

---

# 15. Related Documents

* [ADR-008: Small-Scale Deployment On Hetzner With PostgreSQL And Cloudflare R2](../adr/ADR-008-hetzner-postgresql-r2-small-scale.md)
* [ADR-006: Distributed Execution And Shared Artifacts](../adr/ADR-006-distributed-execution-and-shared-artifacts.md)
* [ADR-007: Repository Registration And Multi-Repository Isolation](../adr/ADR-007-repository-registration-and-multi-repository-isolation.md)
* [Distributed Execution](distributed-execution.md)
* [Repository Registration](repository-registration.md)
* [Deployment Architecture](deployment-architecture.md)
* [Workflow Runtime](workflow-runtime.md)
