# AEP-010: Implement Workflow Scheduler

**Status:** Completed

## Context

The Workflow Runtime advances a WorkflowExecution by scheduling Tasks only when their declared dependencies have reached the required state. Scheduling remains separate from Agent reasoning, context retrieval, and task-specific handler logic.

The scheduler consumes a validated DAG and persisted runtime state. Its decisions must be retry-safe, observable through ExecutionEvents, and testable without real models or external systems.

## Deliverable

Implement an MVP scheduler that:

* computes ready Tasks from the DAG and current TaskExecution states;
* creates one idempotent TaskExecution attempt per ready Task;
* blocks dependents until prerequisites succeed and numbers retries;
* records queued, started, succeeded, and failed events; and
* tests parallel readiness, dependency blocking, retries, and failures with fake executors.

## Dependencies

* AEP-004
* AEP-008
* AEP-009
* AEP-011

## Acceptance Criteria

* Scheduler creates one TaskExecution per ready Task.
* Scheduler waits for dependencies before scheduling dependent Tasks.
* Scheduler supports retry attempt numbering.
* Scheduler emits ExecutionEvents for queued, started, succeeded, and failed states.
* Scheduler can be tested with fake Task executors.

## Implementation

`aep.workflow_scheduler.WorkflowScheduler` reconciles one deterministic
parallel-ready wave at a time from a resolved `TaskDagPlan` and persisted
runtime state. It creates every ready `TaskExecution` before invoking the
provider-neutral `TaskExecutor`, binds dependents to the successful prerequisite
attempt identifiers, and leaves blocked dependents unscheduled.

Attempt and lifecycle-event identifiers are deterministic. The atomic
`PENDING` to `RUNNING` transition prevents concurrent reconcilers from invoking
the same attempt twice. The MVP does not reclaim a `RUNNING` attempt based on
elapsed time because timestamps do not prove that an executor has stopped; an
external controller must explicitly resolve genuinely abandoned attempts until
owner-token leases are implemented. Idempotent event appends repair missing or
duplicate lifecycle-event emission safely. Every attempt identifier is attached
atomically and idempotently to its persisted WorkflowExecution.
Recoverable failures create a new numbered attempt on a later reconciliation
until the scheduler's explicit `max_attempts` limit is reached; all other
failure classes block dependent Tasks. Queued, started, succeeded, and failed
states are captured as append-only `ExecutionEvent` records with shared trace
and provenance data.

Each reconciliation validates both the caller evidence and the authoritative
persisted WorkflowExecution, then uses the persisted object as its source of
truth. WorkflowExecution, TaskExecution, and ExecutionEvent records are checked
against the authoritative runtime schemas before scheduler persistence.
