# AEP-075: Add Durable Dispatch, Execution Leases, And Worker Identity

**Status:** Not Started

## Context

The current workflow runtime reconciles work in one process. The webhook
outbox lists pending rows without claiming them, and a WorkflowExecution has no
durable owner across machines. A second worker could duplicate work or mutate a
worktree without ownership evidence.

ADR-006 requires a durable dispatch queue, execution-affinity leases, fencing,
and explicit worker identity before AEP can run on more than one machine.

## Deliverable

Add the PostgreSQL-backed dispatch queue, execution leases, worker registration,
and scheduler integration required for one worker to own one WorkflowExecution
at a time, with safe redispatch after failure, approval, or lease expiry.

## Dependencies

* AEP-010
* AEP-011
* AEP-039
* AEP-073

## Acceptance Criteria

* Event acceptance commits one outbox row; dispatch claims it with a visibility
  timeout, bounded attempts, and a terminal or dead-letter state.
* A dispatch row is claimed with an atomic compare-and-swap or
  `FOR UPDATE SKIP LOCKED` equivalent; only one worker owns it.
* An execution lease records worker ID, registration, execution ID, fence
  token, heartbeat time, and expiry.
* Checkout registry claims, lease fields, and branch/path uniqueness are
  transactionally stored and registration-scoped; a worktree path remains
  node-local evidence.
* The worker renews and verifies its lease before every durable mutation and
  before every publication request.
* A stale or expired owner cannot write runtime evidence or request
  publication; its fence is rejected.
* A worker crash or lease expiry permits a new owner to reclaim the execution
  without creating a second WorkflowExecution.
* Worker registration records identity, supported registrations, Docker and
  validation capabilities, capacity, and `ACTIVE`/`DRAINING`/`OFFLINE` state.
* `DRAINING` registrations reject new Events and new WorkflowExecutions while
  permitting redispatch of already accepted executions for approval wake-up or
  crash recovery.
* Terminal evidence and dispatch completion commit in one transaction; a
  completed dispatch row is never redelivered.
* Duplicate webhook delivery and duplicate dispatch claims are tested
  deterministically.
* Execution ownership remains execution-affinity: one worker leases the whole
  WorkflowExecution. Tasks are not independently distributed.
* Tests cover lease heartbeat, expiry, fencing, restart, worker drain, approval
  wake-up, duplicate claims, and terminal retirement.
* Health and readiness expose worker identity, lease state, queue age, and
  registration allowlists without secrets.

## Out Of Scope

* Credential-owning publication (AEP-076).
* Multi-registration ingress routing (AEP-080).
* Task-level distribution.

## Design References

* [ADR-006](../adr/ADR-006-distributed-execution-and-shared-artifacts.md)
* [Distributed Execution](../architecture/distributed-execution.md)
* [Small-Scale Deployment](../architecture/small-scale-deployment.md)
