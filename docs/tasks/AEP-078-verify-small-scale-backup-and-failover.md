# AEP-078: Verify Small-Scale Backup, Restore, And Failover

**Status:** Not Started

## Context

The small-scale profile accepts a single-host failure domain but not silent
data loss. A PostgreSQL volume, the host, or a worker can fail. ADR-008 targets
an RPO of 15 minutes or better and an RTO of 2 hours for the first profile.

Backup configuration alone is not evidence. The deployment needs deterministic
failure tests and a measured restore procedure.

## Deliverable

Add automated backup and restore verification, a host-replacement procedure, and
fault-injection tests for worker loss, database loss, R2 outage, Cloudflare
Tunnel outage, and patch reconstruction.

## Dependencies

* AEP-077

## Acceptance Criteria

* A full PostgreSQL backup plus WAL archive restores to a target time in a
  clean instance.
* A known WorkflowExecution, TaskExecution, EvaluationResult, PolicyDecision,
  and GeneratedArtifact identity survive restore with matching provenance.
* One restored artifact is fetched from R2 and its digest is verified against
  the restored metadata.
* Restore duration and achieved RPO are recorded as evidence; the measured
  result meets or explicitly documents a deviation from the ADR-008 target.
* A lost `aep-core` host is rebuilt from the infrastructure package, and the
  control plane, database, worker, and Tunnel return to health.
* A second worker joins the private network, shares PostgreSQL and R2, and takes
  over an execution after lease expiry without a shared worktree or duplicate
  external effect.
* A worker kill during GeneratePatch or RunValidation leads to lease expiry,
  patch reconstruction, and completion without a duplicate external effect.
* A worker pause after a publication request does not produce a second push or
  pull request.
* PostgreSQL unavailability fails ingress closed and prevents publication.
* R2 unavailability prevents metadata commits and publication; retry succeeds
  after recovery without orphaned metadata.
* Cloudflare Tunnel unavailability leaves no accepted Event; GitHub redelivery
  is deduplicated after recovery.
* The monthly restore test is scripted and produces bounded, redacted evidence.
* Runbook documentation covers detection, decision, recovery, verification, and
  data-gap recording.

## Out Of Scope

* Multi-region recovery.
* Automatic database failover.
* Multi-repository recovery.

## Design References

* [ADR-008](../adr/ADR-008-hetzner-postgresql-r2-small-scale.md)
* [Small-Scale Deployment](../architecture/small-scale-deployment.md)
* [Distributed Execution](../architecture/distributed-execution.md)
