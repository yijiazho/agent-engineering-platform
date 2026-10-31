# AEP-073: Implement PostgreSQL Transactional Runtime Store

**Status:** Not Started

## Context

The MVP persists the complete runtime object graph to one durable JSON file under
a cross-process writer fence. That is correct for one host, but it cannot
provide shared state, cross-node fencing, or transactional queue/lease
semantics for the small-scale deployment in ADR-008.

ADR-006 requires a transactional adapter behind the existing
`RuntimeObjectStore` contract. PostgreSQL is the reference implementation.
The JSON adapter must remain available for local development and deterministic
tests.

## Deliverable

Add a PostgreSQL-backed transactional runtime store, versioned schema
migrations, configuration, and conformance tests. Preserve existing runtime
object semantics and keep the JSON adapter as the local/test default.

## Dependencies

* AEP-004

## Acceptance Criteria

* `TransactionalRuntimeObjectStore` implements the complete
  `RuntimeObjectStore` contract with semantics equivalent to the durable JSON
  adapter.
* One transaction commits an object creation and its deterministic key.
* One transaction commits a lifecycle transition, owning WorkflowExecution
  attachment, and all required ExecutionEvents.
* Compare-and-swap status transitions and terminal immutability are preserved
  under concurrent writers.
* Task dispatch claims, approval wake-up, and approval request binding remain
  atomic.
* Runtime objects, events, deterministic keys, claims, and artifact metadata
  are scoped by `registrationId`; uniqueness constraints and indexes enforce
  that scope.
* Existing single-repository evidence receives a default registration during
  migration without changing object IDs.
* Schema migrations are versioned, forward-only, and checked into the
  repository. Startup fails closed when the schema version is incompatible.
* A real PostgreSQL integration fixture runs the store conformance suite,
  concurrent transition tests, restart/reconnect tests, and migration tests.
* The JSON adapter remains the default for local tests; no multi-writer JSON
  mode is introduced.
* Configuration, migration commands, connection pooling bounds, and local
  development instructions are documented.
* No credential, prompt, issue body, or artifact body appears in database
  diagnostics or tests.

## Out Of Scope

* The dispatch queue worker loop and execution leases (AEP-075).
* The object-store adapter (AEP-074).
* Repository registration lifecycle and ingress routing (AEP-079 and AEP-080).

## Design References

* [ADR-006](../adr/ADR-006-distributed-execution-and-shared-artifacts.md)
* [ADR-008](../adr/ADR-008-hetzner-postgresql-r2-small-scale.md)
* [Small-Scale Deployment](../architecture/small-scale-deployment.md)
