# AEP-011: Implement TaskExecution Lifecycle

**Status:** Completed

## Context

`TaskExecution` represents one attempt to perform a Task and is the primary boundary for retries, evidence, failure classification, and observability. Runtime status may change while an attempt is active, but completed evidence must remain immutable under ADR-002.

The lifecycle must distinguish recoverable, configuration, evaluation, and policy failures and reject transitions that would rewrite terminal execution history.

## Deliverable

Implement a TaskExecution lifecycle component that:

* defines supported statuses and an explicit transition table;
* persists transitions atomically with concurrency checks;
* records attempt, timestamps, failure class, and trace provenance;
* prevents mutation after terminal completion; and
* tests success, cancellation, approval waiting, retry, invalid transitions, and each failure class.

## Dependencies

* AEP-002
* AEP-004

## Acceptance Criteria

* Supported statuses include Pending, Running, Succeeded, Failed, Cancelled, and AwaitingApproval.
* Invalid transitions are rejected.
* Failure class is recorded for failed executions.
* Completed evidence cannot be mutated.
* Tests cover success, recoverable failure, configuration failure, evaluation failure, and policy denial.

## Implementation Note

TaskExecution creation validates the complete record against the authoritative
runtime schema, including timestamp formats, before the lifecycle performs its
first persistence mutation.
