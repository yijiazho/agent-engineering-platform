# ADR-008: Small-Scale Deployment On Hetzner With PostgreSQL And Cloudflare R2

**Status:** Accepted

**Authors:** Project Team

**Date:** 2026-10-31

**Implementation Status:** Design only; no deployment task is complete.

---

# Context

ADR-006 defines the target distributed execution architecture. ADR-007 defines
explicit repository registration and isolation. Neither requires a full
Kubernetes platform on day one.

The first operational step must:

* run AEP for one repository, with a path to a second worker and a second
  repository;
* keep artifacts and evidence durable and shared outside the worker host;
* preserve Docker validation isolation;
* remain operable by a small team;
* avoid a large fixed cloud bill before usage is proven.

The current MVP uses a durable JSON runtime store, a local filesystem
content-addressed store, a local SQLite checkout registry, and one local Docker
worker. That is appropriate for development. The first deployment should replace
the storage boundaries that block shared artifacts and machine replacement,
without introducing a service mesh or Kubernetes.

The chosen stack is:

* **Hetzner Cloud** for compute and block storage;
* **PostgreSQL** for transactional runtime state, dispatch, leases, artifact
  metadata, and registrations;
* **Cloudflare R2** for artifact and evidence bytes and PostgreSQL backups;
* **Cloudflare Tunnel** for HTTPS ingress without exposing an origin port;
* **Docker Compose** for the first physical composition.

---

# Decision

AEP will use a single-region, small-scale Hetzner deployment as the first
production-like profile.

## 1. Compute: Hetzner Cloud

The default region is an EU location (Falkenstein or Helsinki) unless the
repository, model provider, or operators require a US location.

Phase 1 uses one `aep-core` Cloud server:

* reference size: 2 vCPU / 4 GB RAM / 80 GB local disk for low concurrency;
* preferred size when Docker validation runs concurrently: 4 vCPU / 8 GB RAM /
  160 GB;
* one Hetzner Cloud Volume for PostgreSQL data;
* local NVMe for source caches, execution worktrees, and temporary files;
* a private Hetzner network for future workers and an optional database host;
* no public database or Docker API port;
* SSH restricted to operator addresses;
* outbound HTTPS allowed to GitHub, OpenAI, Cloudflare R2, and the Cloudflare
  Tunnel endpoints;
* outbound TCP/UDP 7844 for Cloudflare Tunnel transport;

A second Hetzner Cloud server may join the private network as an execution
worker. It runs Docker and a local worktree root. It does not run the database.

## 2. Transactional State: PostgreSQL

PostgreSQL is the authoritative runtime store for:

* runtime objects and execution events;
* deterministic keys and claims;
* dispatch queue and execution leases;
* publication attempts;
* artifact and evidence metadata;
* content reservations and collection tombstones;
* repository registrations;
* model quota reservations.

Phase 1 runs PostgreSQL on the `aep-core` volume. It is pinned to a supported
major version, configured with TLS for non-local connections, `scram-sha-256`
authentication, per-service roles, and a separate migration role.

If Hetzner Managed PostgreSQL is available in the selected location and meets
the backup, TLS, and networking requirements, it may replace the self-managed
instance without changing the application contract. The decision is deferred
until the operational baseline is measured.

The JSON runtime store remains a local development and test adapter. It is not
used in this deployment.

## 3. Artifacts And Evidence: Cloudflare R2

R2 is the shared immutable content-addressed store for:

* GeneratedArtifact bodies;
* Tool and Docker command logs;
* normalized Model input and output evidence;
* validation reports;
* PostgreSQL backups;
* compressed service logs.

Object keys are scoped by deployment and registration, for example:

```text
aep-prod-artifacts/artifacts/<registrationId>/sha256/<first-two>/<remaining-hex>
aep-prod-artifacts/evidence/<registrationId>/<workflowExecutionId>/...
aep-prod-artifacts/logs/<service>/<date>/...
aep-prod-backups/postgres/<deployment>/...
```

Two buckets are used so the backup token can be scoped independently:

* `aep-prod-artifacts` for artifacts, evidence, and logs;
* `aep-prod-backups` for PostgreSQL backups.

R2 is not a database. It is not a queue, lease store, or source of workflow
truth. Artifact reads require the PostgreSQL catalog and provenance checks.

Both buckets are private. Separate R2 API tokens are issued for:

* worker artifact/evidence read/write;
* control-plane artifact/evidence read/write;
* publication artifact/evidence read;
* backup read/write on the backup bucket;
* optional read-only inspection export.

R2 tokens can be scoped to a bucket and operation, not to a key prefix. Prefix
isolation is enforced by the application; use separate buckets when hard tenant
isolation is required. No token has account administration or bucket deletion.

## 4. Ingress: Cloudflare Tunnel

A `cloudflared` container connects the origin to Cloudflare. The public
hostname exposes the GitHub webhook route only:

```text
https://aep.example.com/v1/webhooks/github
```

TLS terminates at Cloudflare. The webhook body is forwarded unchanged, and AEP
verifies the GitHub HMAC over the raw bytes before JSON decoding.

No origin port 80/443 is published. SSH remains restricted and separate.
Operator inspection uses SSH or a Cloudflare Access-protected route, not a
public API.

## 5. Physical Composition: Docker Compose

Phase 1 uses Docker Compose on `aep-core`:

| Role | Process boundary | Notes |
| --- | --- | --- |
| Ingress | `cloudflared` | Tunnel only |
| Event Controller | AEP event-controller | Public webhook route |
| Control Plane | Dispatcher, leases, inspection API, registration router | Co-located but separate from the worker |
| Publication Service | Credential-owning Git/GitHub mutation boundary | Co-located in phase 1, but not inside the execution worker |
| Execution Worker | Workflow runtime plus Task handlers | Mounts the Docker socket |
| PostgreSQL | Database | Private network or local socket |
| Backup | `pgBackRest` or WAL-G job | Writes encrypted backups to R2 |
| Observability | Optional node/Postgres exporter or Grafana agent | Not required for the first boot |

The seven logical service boundaries from ADR-003 remain. Non-scaling services
may be physically co-located in one control-plane process. The documentation
must not claim independent process isolation that the physical composition does
not provide.

The Docker socket is mounted only into the execution worker. The control plane
and publication service do not receive it.

## 6. Secrets And Identity

Secrets are mounted as read-only files under a root-only directory:

```text
/etc/aep/secrets/github-app-private-key.pem
/etc/aep/secrets/github-webhook-secret
/etc/aep/secrets/openai-api-key
/etc/aep/secrets/r2-access-key-id
/etc/aep/secrets/r2-secret-access-key
/etc/aep/secrets/postgres-app-password
/etc/aep/secrets/postgres-migration-password
```

Rules:

* no secret is committed to `.ai`, Git, Compose environment literals, logs, or
  artifacts;
* PostgreSQL roles are least privilege;
* R2 tokens are per-role and scoped to the deployment bucket and operation;
  prefix isolation is enforced by the application, and a separate bucket is
  required when hard tenant isolation is necessary;
* GitHub App installation tokens remain repository-scoped;
* publication credentials are owned by the publication service, not workers;
* rotation is documented and tested for GitHub App keys, webhook secret,
  OpenAI key, R2 tokens, and PostgreSQL passwords.

## 7. Backup And Recovery Target

The first profile accepts single-host availability but not silent data loss.

* PostgreSQL uses continuous WAL archiving plus a periodic full backup to R2.
* Backup files are encrypted before upload; keys are held outside R2.
* Retention: 14 days of WAL and 30 days of full backups for phase 1.
* A restore test runs at least monthly.
* R2 bucket versioning and lifecycle rules protect artifacts and backups from
  accidental deletion.
* Target RPO: 15 minutes or better with WAL archiving.
* Target RTO: 2 hours for a single-host replacement and database restore.

Hetzner snapshots are supplementary. They are not a substitute for
database-consistent PostgreSQL backups.

## 8. Cost Envelope

Reference list prices observed in October 2026, excluding VAT and LLM usage:

| Item | Approx. monthly |
| --- | ---: |
| Hetzner CPX22-class server (2 vCPU / 4 GB / 80 GB) | EUR 20-25 |
| Hetzner CPX32-class server (4 vCPU / 8 GB / 160 GB) | EUR 35-50 |
| 100 GB Hetzner Volume | EUR 4-6 |
| IPv4 address and optional backups | EUR 1-5 |
| R2 storage and operations at 50-100 GB | USD 1-5 |
| Cloudflare Tunnel and DNS | USD 0 |
| GitHub Actions / GHCR | USD 0-10 |
| Optional observability | USD 0-20 |
| **Infrastructure subtotal** | **USD/EUR 30-100** |
| LLM at 100 executions/month | USD 15-40 |
| **Expected starting total** | **USD/EUR 45-140** |

Engineering setup is estimated at 3-5 days, with 2-4 hours of monthly
operations. At this scale, engineering time is usually larger than the
infrastructure bill.

## 9. Scale-Out Path

1. **Phase 1 (AEP-073 through AEP-078):** one `aep-core` VM, self-managed
   PostgreSQL, one worker, R2, Cloudflare Tunnel.
2. **Phase 2:** move PostgreSQL to a dedicated VM or Hetzner Managed PostgreSQL;
   add a second worker on the private network.
3. **Phase 3:** separate control-plane and publication processes; add worker
   capacity; introduce a managed database with HA if the availability target
   requires it.
4. **Phase 4:** Kubernetes only if worker count, multi-region, or platform-team
   requirements justify it.

Adding a worker must not require a shared worktree or a shared host filesystem.
Workers use local worktrees and reconstruct accepted patches from R2.

---

# Consequences

## Benefits

* Artifacts and evidence survive a worker replacement.
* PostgreSQL provides the transactional and queue primitives ADR-006 requires.
* R2 removes artifact egress cost and avoids running another stateful service.
* Cloudflare Tunnel avoids exposing the origin and does not require a load
  balancer.
* A single VM is enough to exercise the real storage and artifact boundaries
  before paying for distribution.
* The same PostgreSQL and R2 contracts support later worker and registration
  scale-out.

## Trade-offs

* Phase 1 has a single VM and a single PostgreSQL instance. A host failure is a
  real outage.
* The database and worker share a failure domain until phase 2.
* Self-managed PostgreSQL requires backup, restore testing, patching, and
  credential rotation.
* Docker socket access remains a privileged boundary on the worker.
* R2 is an external dependency on the artifact and backup path.
* Cloudflare Tunnel is an external dependency on webhook ingress.

## Risks

* A lost Hetzner volume without a valid R2 backup loses runtime evidence.
* A mis-scoped R2 token or bucket policy could expose artifacts across
  registrations.
* A stale or untested restore procedure can turn a small incident into data
  loss.
* Docker validation runs untrusted repository code; the worker must remain
  isolated and network-disabled during validation.
* Hetzner list prices have changed repeatedly; the cost envelope must be
  rechecked at purchase and renewal.

---

# Rejected Alternatives

## Kubernetes On Day One

Rejected because the small profile has one worker and no independent scaling
need. Kubernetes adds control-plane cost, privileged Docker-runner complexity,
and operational work before the storage and registration contracts are proven.

## Managed Application Platform For The Worker

Rejected for the first worker because AEP validation needs a Docker daemon.
Fly.io, Railway, Render, and ECS Fargate do not expose a general Docker socket.
Privileged Docker-in-Docker is a weaker boundary than a dedicated VM.

## JSON Runtime Store On A Shared Volume

Rejected for the same reasons as ADR-006: no portable multi-writer transaction,
no cross-node fencing, and no safe NFS/SMB semantics.

## MinIO On The Same VM

Rejected for phase 1 because R2 removes another stateful service, backup
obligation, and disk-capacity failure mode. MinIO remains an option if data must
stay entirely inside a private network.

## S3 Instead Of R2

Rejected as the default because R2 has no egress charge and is S3-compatible.
S3 remains valid when workers run in the same AWS region and the deployment is
already AWS-native.

## Managed PostgreSQL From Another Cloud As The First Step

Rejected because it adds cross-cloud networking, latency, and a second provider
before the single-host baseline is measured. It becomes a valid phase-2 choice
when database operations or availability justify it.

## Multi-Region Or Active-Active

Rejected because there is no current availability requirement that justifies
the cost and consistency complexity.

---

# Implementation Tasks

* AEP-073: PostgreSQL transactional runtime store.
* AEP-074: Shared content-addressed evidence store.
* AEP-075: Durable dispatch, execution leases, and worker identity.
* AEP-076: Credential-owning publication service.
* AEP-077: Hetzner small-scale deployment package.
* AEP-078: Backup, restore, and failover verification.
* AEP-079: Repository registration control plane.
* AEP-080: Multi-registration routing and isolation.

---

# Open Questions

* Does Hetzner Managed PostgreSQL meet the required backup, TLS, and restore
  guarantees in the selected location?
* Should the first deployment use an EU or US region, based on GitHub, model,
  and operator latency?
* Should the publication service be a separate container in phase 1, or a
  distinct process inside the control-plane container?
* What is the acceptable RPO if WAL archiving is delayed?
* Which R2 jurisdiction and retention rules are required?
* Do operators need a Cloudflare Access-protected inspection UI, or is SSH plus
  the `aep` CLI sufficient?
* Is 4 GB RAM sufficient for the expected Docker validation workload, or should
  the first host start at 8 GB?

---

# Related Documents

* [ADR-006: Distributed Execution And Shared Artifacts](ADR-006-distributed-execution-and-shared-artifacts.md)
* [ADR-007: Repository Registration And Multi-Repository Isolation](ADR-007-repository-registration-and-multi-repository-isolation.md)
* [Small-Scale Deployment Architecture](../architecture/small-scale-deployment.md)
* [Distributed Execution](../architecture/distributed-execution.md)
* [Deployment Architecture](../architecture/deployment-architecture.md)
