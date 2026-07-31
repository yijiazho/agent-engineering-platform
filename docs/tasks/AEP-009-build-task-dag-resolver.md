# AEP-009: Build Task DAG Resolver

**Status:** Completed

## Context

A Workflow orchestrates explicitly versioned Task Resources as a directed acyclic graph. Before scheduling, the platform must convert that declarative graph into a validated, deterministic execution plan without allowing an Agent or model to choose execution order.

The resolver validates graph structure and dependencies only. It does not create TaskExecutions or execute handlers.

## Deliverable

Implement a Task DAG resolver that:

* loads and validates every referenced Task;
* rejects missing nodes, duplicate identities, and cycles with structured errors;
* produces stable topological order and dependency metadata;
* exposes parallel-ready Task groups; and
* tests linear, branched, missing-reference, and cyclic graphs.

## Dependencies

* AEP-003

## Acceptance Criteria

* Resolver validates all referenced Tasks exist.
* Resolver rejects cyclic dependencies.
* Resolver returns deterministic topological order.
* Resolver supports parallel-ready Task groups.
* Tests cover linear, branched, missing, and cyclic DAGs.
