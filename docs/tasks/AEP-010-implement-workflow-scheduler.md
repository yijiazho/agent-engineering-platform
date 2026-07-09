# AEP-010: Implement Workflow Scheduler

**Status:** Not Started

## Context

The Workflow Runtime schedules TaskExecutions based on dependencies and status.

## Deliverable

Implement a scheduler that transitions ready Tasks into TaskExecutions.

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
