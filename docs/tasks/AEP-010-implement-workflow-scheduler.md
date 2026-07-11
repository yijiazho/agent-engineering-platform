# AEP-010: Implement Workflow Scheduler

**Status:** Not Started

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
