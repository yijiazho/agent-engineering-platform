# AEP-077: Package The Hetzner Small-Scale Deployment

**Status:** Not Started

## Context

ADR-008 selects one Hetzner Cloud host, PostgreSQL, Cloudflare R2, and a
Cloudflare Tunnel as the first production-like profile. The repository
currently contains local and self-hosting Compose profiles but no reproducible
Hetzner deployment assets, PostgreSQL volume layout, R2 configuration, backup
job, or small-scale operator runbook.

Deployment must be reproducible from an empty account and must not expose
PostgreSQL, Docker, or worker ports publicly.

## Deliverable

Add a `deploy/hetzner/` deployment package containing infrastructure
provisioning, cloud-init, Docker Compose, PostgreSQL configuration, R2
configuration, Cloudflare Tunnel configuration, secret layout, backup jobs,
health checks, and an operator runbook for the single-host small-scale profile.

## Dependencies

* AEP-047
* AEP-073
* AEP-074
* AEP-075
* AEP-076

## Acceptance Criteria

* A new Hetzner project can be provisioned from the documented steps and
  infrastructure definitions without manual undocumented edits.
* Images, PostgreSQL, cloudflared, pgBackRest/WAL-G, and validation images are
  pinned by digest or exact version.
* The host uses a private Hetzner network, restricted SSH, no public
  PostgreSQL/Docker/worker ports, and outbound-only Cloudflare Tunnel transport.
* PostgreSQL data lives on a dedicated Hetzner Volume; worktrees and caches use
  local disk and remain non-authoritative.
* PostgreSQL roles, TLS for non-local connections, migration job, and
  connection bounds are configured.
* R2 artifact and backup buckets, prefixes, lifecycle rules, versioning, and
  bucket-scoped tokens are configured. No bucket is public.
* A Cloudflare Tunnel exposes only the GitHub webhook route; the webhook secret
  and HMAC verification are configured.
* Secret files are root-only and mounted only into the components that need
  them. No secret is committed or printed.
* Docker Compose runs ingress, control plane, publication service, worker,
  PostgreSQL, and backup roles. Only the worker mounts the Docker socket.
* Validation runs with networking disabled and bounded CPU, memory, and timeout.
* A signed fixture delivery is accepted, a duplicate is deduplicated, and the
  repository, Workspace, resource revision, and health endpoints verify.
* The deployment passes the deterministic end-to-end harness in the deployed
  environment without live provider credentials where the harness permits it.
* Backup, restore, upgrade, secret rotation, shutdown, and host-replacement
  procedures are documented.
* The operator runbook includes the cost envelope, DNS/Tunnel setup, GitHub App
  setup, and first controlled issue procedure.
* Deployment configuration tests validate Compose, environment references,
  firewall assumptions, and the absence of committed secrets.

## Out Of Scope

* Live controlled issue-to-PR pilot evidence, which remains AEP-043.
* A second worker, which is a phase-2 deployment step.
* Multi-registration ingress (AEP-080).

## Design References

* [ADR-008](../adr/ADR-008-hetzner-postgresql-r2-small-scale.md)
* [Small-Scale Deployment](../architecture/small-scale-deployment.md)
* [Deployment Architecture](../architecture/deployment-architecture.md)
