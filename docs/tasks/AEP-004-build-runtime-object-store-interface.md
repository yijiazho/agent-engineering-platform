# AEP-004: Build Runtime Object Store Interface

**Status:** Completed

## Context

Runtime services need a persistence boundary without coupling implementation to PostgreSQL, object storage, or Redis from day one.

## Deliverable

Define a Runtime Object Store interface and an in-memory implementation for tests.

## Dependencies

* AEP-002

## Acceptance Criteria

* Store supports create, update status, append event, get by id, and list by workflow execution.
* Store enforces immutable evidence after completion.
* Store supports idempotent create by deterministic key.
* Store has tests for concurrent status updates.
* Interface does not expose Git Resource storage concerns.

## Implementation

`src/aep/runtime_store.py` defines the persistence interface and a thread-safe
in-memory implementation. Status updates support optimistic concurrency through
an expected status, returned objects are defensive immutable snapshots, and
terminal objects reject further state changes. Execution events remain separate,
append-only runtime objects and all runtime objects can be indexed by their
owning WorkflowExecution.
