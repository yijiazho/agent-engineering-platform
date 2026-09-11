# AEP-058: Implement Workflow Execution Resume

**Status:** Not Started

## Context

AEP workflows are durable DAG executions, but a failed or interrupted run currently lacks a first-class operator action to continue from previously successful work. Re-running the entire workflow wastes model invocations and can produce materially different reasoning outputs even when earlier TaskExecutions were valid.

Resume is not equivalent to replay. Resume must continue the same logical workflow attempt from durable checkpoints while proving that reused task outputs are still valid for the same repository revision, Resource graph, and upstream artifact identities.

## Deliverable

Implement workflow resume semantics and an `aep resume` command that can continue an interrupted or failed WorkflowExecution from the earliest eligible incomplete task or from an explicitly selected task boundary.

The scheduler must reuse only completed TaskExecutions whose immutable inputs remain valid and must invalidate downstream state when a requested resume boundary changes required upstream evidence.

## Dependencies

* AEP-004
* AEP-009
* AEP-010
* AEP-011
* AEP-018
* AEP-039
* AEP-051
* AEP-056

## Acceptance Criteria

* `aep resume <workflow-execution-id>` continues an eligible failed or interrupted execution without re-running successful tasks whose immutable inputs and outputs remain valid.
* `aep resume <id> --from <task>` supports an explicit task boundary and deterministically invalidates or supersedes affected downstream TaskExecutions.
* Resume verifies repository revision, resolved Resource references, upstream artifact digests, ContextPackage identities, and required policy state before reusing prior results.
* A resume request fails closed when the original execution workspace/revision cannot be reconstructed or when required upstream evidence is missing or inconsistent.
* Already completed external side effects are idempotently recognized; resume cannot create a duplicate pull request or repeat another publication action solely because a process restarted.
* Reused and newly executed TaskExecutions are distinguishable in runtime provenance, and the execution history records each resume attempt and selected boundary.
* Scheduler eligibility remains deterministic after restart and produces the same next runnable task set for identical persisted state.
* Resume honors retry classification, capability policy, approval state, and publication policy instead of bypassing those controls.
* Tests cover process interruption, failed-task resume, explicit resume boundary, downstream invalidation, artifact mismatch, duplicate-publication prevention, and repeated resume requests.
* Operational documentation explains when to use retry, resume, and replay and the guarantees of each mechanism.
