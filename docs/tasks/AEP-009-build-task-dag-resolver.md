# AEP-009: Build Task DAG Resolver

**Status:** Not Started

## Context

Workflow Resources orchestrate Task Resources through a deterministic DAG.

## Deliverable

Resolve a Workflow Resource into an executable Task DAG.

## Dependencies

* AEP-003

## Acceptance Criteria

* Resolver validates all referenced Tasks exist.
* Resolver rejects cyclic dependencies.
* Resolver returns deterministic topological order.
* Resolver supports parallel-ready Task groups.
* Tests cover linear, branched, missing, and cyclic DAGs.
