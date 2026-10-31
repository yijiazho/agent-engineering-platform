# AEP-076: Add Credential-Owning Publication Service

**Status:** Not Started

## Context

The current CreatePullRequest handler performs Git commit, push, and GitHub
pull-request creation from the execution worker. A worker-side preflight check
cannot serialize a worker that pauses after the check but before the remote
mutation. A second owner could then publish again after lease expiry.

A replacement worktree also needs the accepted PATCH artifact restored and
verified before RunValidation or CreatePullRequest can continue.

ADR-006 requires a credential-owning publication service, durable publication
attempts, and accepted-patch reconstruction.

## Deliverable

Move external publication behind a publication service that owns Git and GitHub
mutation credentials, serializes one attempt per execution, reconstructs and
verifies the accepted PATCH artifact in a clean worktree, and reconciles
ambiguous outcomes before execution ownership transfers.

## Dependencies

* AEP-022
* AEP-024
* AEP-034
* AEP-049
* AEP-066
* AEP-073
* AEP-074
* AEP-075

## Acceptance Criteria

* The execution worker never receives `git.push` or `github.create_pr`
  credentials; only the publication service holds them.
* A publication request binds registration, execution, TaskExecution, lease
  fence, policy decisions, patch digest, repository, branch, head, base, and
  idempotency key.
* The service validates the current lease and fence, publication policy,
  capability policy, repository identity, and prior attempt before obtaining
  credentials.
* The service reconstructs a clean worktree at the recorded base revision from
  the accepted PATCH artifact and verifies the patch digest, changed paths, and
  postimage digests. It does not trust a worker-supplied worktree or commit.
* A missing, corrupt, or non-applicable PATCH fails closed. The service never
  silently re-runs the model under existing terminal evidence.
* Durable publication attempts move through `AUTHORIZED`, `IN_FLIGHT`,
  `CONFIRMED`, `FAILED`, or `UNKNOWN` and are serialized per execution.
* A second request while an attempt is `AUTHORIZED`, `IN_FLIGHT`, or `UNKNOWN`
  is rejected or reconciled; it never starts a second push or pull request.
* A new execution owner cannot acquire dispatch or a lease until non-terminal
  or `UNKNOWN` publication attempts are reconciled.
* A worker pause, lease expiry, or process exit after authorization does not
  authorize another publication.
* Duplicate branch, head, base, and pull-request identity reconciliation
  prevents duplicate remote effects.
* Tests cover lease loss before and after mutation, worker pause, duplicate
  requests, missing or corrupt PATCH, patch drift, partial push, duplicate PR,
  and a stale worker with valid credentials.
* Existing policy, evaluation, and approval gates remain independently
  enforced; publication does not bypass capability or publication policy.
* Operator documentation explains attempt states, reconciliation, and recovery.

## Out Of Scope

* Multi-registration publication scoping (AEP-080).
* A general-purpose external side-effect service for non-publication actions.

## Design References

* [ADR-006](../adr/ADR-006-distributed-execution-and-shared-artifacts.md)
* [Distributed Execution](../architecture/distributed-execution.md)
* [Small-Scale Deployment](../architecture/small-scale-deployment.md)
