# AEP-035: Compose MVP Services

**Status:** Not Started

## Context

ADR-003 defines MVP services: event-controller, resource-controller, workflow-runtime, agent-resolver, context-builder, tool-runtime, and evaluation-engine.

## Deliverable

Create local service composition for MVP development.

## Dependencies

* AEP-003
* AEP-004

## Acceptance Criteria

* All MVP services can start locally.
* Services expose health endpoints.
* Services can use in-memory or local storage adapters.
* Configuration identifies one repository and workspace.
* Smoke test starts services and resolves basic configuration.
