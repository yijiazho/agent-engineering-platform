# AEP-057: Implement Deterministic Execution Replay

**Status:** Completed

## Context

AEP records immutable Resource references, repository revisions, ContextPackages, invocation records, evaluation results, policy decisions, and generated artifacts so that AI engineering work has durable provenance. The platform does not yet expose a first-class way to reconstruct a historical execution from that evidence.

Replay is required to debug model behavior, verify reproducibility, compare configuration changes, and build regression evaluation workflows. A replay must never silently resolve floating or current Resource versions, current repository HEAD, or newly assembled context when the caller requests an exact replay.

## Deliverable

Implement a replay service and CLI command that creates a new WorkflowExecution derived from a prior execution while preserving an explicit lineage link to the source execution.

Support an exact replay mode that reuses the historical repository revision and immutable Resource/configuration inputs, plus explicit override flags for selected versioned resources such as Agent, Prompt, Model, Evaluation, or Policy. Overrides must be recorded as part of replay provenance.

## Dependencies

* AEP-004
* AEP-008
* AEP-010
* AEP-013
* AEP-017
* AEP-039
* AEP-051
* AEP-056

## Acceptance Criteria

* `aep replay <workflow-execution-id>` creates a distinct WorkflowExecution with a durable reference to the source execution.
* Exact replay resolves the same repository revision and immutable Resource versions recorded by the source execution and fails closed if any required historical dependency cannot be reconstructed.
* Exact replay does not resolve repository HEAD, `latest`, or current Resource versions in place of missing historical versions.
* Replay records whether ContextPackages are reused verbatim or deterministically rebuilt, and exact replay verifies that rebuilt context has the expected identity before invocation proceeds.
* Explicit resource overrides are accepted only through versioned identifiers and are recorded in replay provenance without mutating the source execution.
* Replaying an execution does not overwrite, append to, or otherwise alter source WorkflowExecution, TaskExecution, invocation, evaluation, policy, or artifact records.
* External side effects such as pull-request creation are disabled by default for replay and require an explicit publication mode governed by normal capability and publication policies.
* Replay output is inspectable through AEP-056 and clearly distinguishes inherited inputs from overridden inputs and newly produced runtime evidence.
* Tests cover exact replay, model/prompt override, missing historical dependency, context identity mismatch, source immutability, and side-effect suppression.
* Documentation defines the reproducibility guarantees and the expected limits of determinism for model-provider outputs.

## Implementation Notes

The replay service first writes a distinct `PENDING` WorkflowExecution linked
to its source and emits `WorkflowExecutionReplayCreated` atomically. The normal
CLI then dispatches that root through the Dogfood Workflow Runtime; `--plan-only`
is the explicit option for creating a pending record without dispatch. Replay
checks out the source repository revision, loads Resource versions from that
checkout, verifies every effective inherited or overridden version, and fails closed when the Event,
repository knowledge snapshot, or any required Resource cannot be reconstructed.
Explicit Prompt, Agent, Model, Evaluation, and Policy overrides must be
versioned and are applied to a copied Resource collection; source records remain
unchanged.

The default `--context reuse` path passes the source ContextPackage to the new
Task invocation without changing that package. Inspection accepts this
cross-execution reference only when the source execution, source Task, replay
lineage, Task reference, and stable context identity agree. `--context rebuild`
uses the normal ContextBuilder and checks each rebuilt package identity before
the handler can invoke its Agent. Missing historical context evidence fails
closed. Rebuilt contexts can diverge after an earlier model output changes; the
replay then stops before the next Agent call. Provider outputs are not promised
to be byte-for-byte deterministic across provider versions or sampling.

Replay publication is disabled by default. The publication Task is denied before
Git push or pull-request creation unless `--publication` is explicitly supplied;
enabled publication still passes the normal emergency guard, capability
policies, and publication policy. Workflow status transitions and lifecycle
events are committed atomically. AEP-056 inspection distinguishes inherited
Resources and ContextPackages, overrides, and newly created runtime evidence.
Inspection uses the normal CLI redaction policy, and supplied timestamps must
be RFC3339 values.
