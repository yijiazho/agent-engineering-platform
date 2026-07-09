# AEP-004: Build Runtime Object Store Interface

**Status:** Not Started

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
