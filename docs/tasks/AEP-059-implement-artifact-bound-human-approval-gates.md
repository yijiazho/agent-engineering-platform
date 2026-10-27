# AEP-059: Implement Artifact-Bound Human Approval Gates

**Status:** Completed

## Context

AEP runtime schemas already model Approval and PolicyDecision supports `REQUIRE_APPROVAL`, but approval is not yet a complete workflow primitive. Governance-sensitive actions need a durable state in which deterministic execution can pause until an authorized human approves the exact artifact or action that was evaluated.

Approval must not become a generic bypass around policy. It must be bound to immutable evidence so that changing a patch, target, repository revision, or publication request invalidates the prior approval.

## Deliverable

Implement end-to-end approval-gate handling for policy decisions that require approval, including pending workflow state, authorization checks, approval/rejection recording, scheduler wake-up, artifact/action binding, and inspection support.

Define the declarative Resource contract needed to specify approval requirements and authorized reviewer identities or groups without embedding provider-specific identity logic in Task handlers.

Approval requests are durable runtime objects, decisions are identity-checked,
and the approved digest must still match the current action, target, repository
revision, and artifact digests. Approval policies declare reviewer identities
and/or groups under `spec.approval`; group resolution uses an injected,
provider-neutral reviewer resolver. The scheduler persists the pending state,
repairs interrupted approval wake-ups, and dispatches only after the handler
rechecks capability and publication policy.

## Dependencies

* AEP-002
* AEP-010
* AEP-011
* AEP-020
* AEP-028
* AEP-036
* AEP-051
* AEP-056

## Acceptance Criteria

* A `REQUIRE_APPROVAL` PolicyDecision transitions the affected task/workflow into a durable approval-pending state rather than failure or publication.
* Every Approval records approver identity, decision, timestamp, governing Policy version, target action, repository revision, and immutable artifact/request digest where applicable.
* Approval of one artifact or publication request cannot authorize a changed artifact, changed target branch/repository, changed revision, or materially different requested capability.
* Rejection creates durable evidence and prevents the gated action from executing unless a later policy-defined workflow path explicitly permits reconsideration.
* Only identities authorized by the resolved approval policy can approve or reject; unauthorized attempts are recorded safely and have no effect on execution eligibility.
* Scheduler restart preserves pending approvals and resumes the gated task exactly once after a valid approval becomes available.
* Approval handling does not bypass pre-execution capability policy or publication policy; all applicable controls remain independently enforceable.
* AEP-056 inspection and explanation surfaces who approved or rejected, what immutable object was approved, and why the workflow is pending or permitted to continue.
* Tests cover approval, rejection, unauthorized identity, artifact mutation after approval, duplicate approval submissions, restart while pending, and post-approval single execution.
* Resource, runtime-schema, operator, and security documentation describe the approval contract and its invalidation rules.

## Implementation Notes

`src/aep/approval.py` owns the durable `Approval` request and decision contract.
A request is bound to the governing Policy versions, target action, explicit
target, repository revision, artifact digests, and a canonical
action/target/revision/artifact-digest SHA-256 digest. `ApprovalGate.decide`
rejects an unauthorized identity, a stale digest, an expired request, or a
second conflicting decision, and records unauthorized attempts as
`ApprovalAuthorizationDenied` events without changing eligibility.

The scheduler persists `TaskExecutionAwaitingApproval`, creates or repairs the
request, wakes only the task's current approval binding after `APPROVED`, and
fails a rejected or expired wait as a non-retryable `POLICY` failure. The owning
`WorkflowExecution` remains `RUNNING` by design so the same scheduler can resume
the gated attempt after a valid decision. The durable pending condition is
evidenced by the `AWAITING_APPROVAL` TaskExecution, the `Approval` request, and
the `REQUIRE_APPROVAL` PolicyDecision, and `aep explain` reports it as
`APPROVAL_PENDING`. The handler rechecks capability and publication policy
before performing the gated action.

Operators discover pending requests with `aep approvals list --status PENDING`
and record a decision with:

```sh
aep approvals decide approval-... --actor alice --decision APPROVED \
  --reason "Reviewed the exact target and digest."
```

`--digest` optionally supplies an independently verified current digest; when
omitted, the digest bound to the persisted request is used and the handler still
rechecks the current action before publication. Rejection and expiry remain
durable evidence. The operator and security contract is documented in
[`docs/operations/approval-gates.md`](../operations/approval-gates.md).

Tests cover artifact and target binding, unauthorized and stale decisions,
rejection, expiration, duplicate submissions, interrupted wake-up recovery,
restart while pending, post-approval single execution, and the operator CLI.
