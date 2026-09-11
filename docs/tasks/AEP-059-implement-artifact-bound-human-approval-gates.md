# AEP-059: Implement Artifact-Bound Human Approval Gates

**Status:** Not Started

## Context

AEP runtime schemas already model Approval and PolicyDecision supports `REQUIRE_APPROVAL`, but approval is not yet a complete workflow primitive. Governance-sensitive actions need a durable state in which deterministic execution can pause until an authorized human approves the exact artifact or action that was evaluated.

Approval must not become a generic bypass around policy. It must be bound to immutable evidence so that changing a patch, target, repository revision, or publication request invalidates the prior approval.

## Deliverable

Implement end-to-end approval-gate handling for policy decisions that require approval, including pending workflow state, authorization checks, approval/rejection recording, scheduler wake-up, artifact/action binding, and inspection support.

Define the declarative Resource contract needed to specify approval requirements and authorized reviewer identities or groups without embedding provider-specific identity logic in Task handlers.

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
