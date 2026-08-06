# AEP-035: Compose MVP Services

**Status:** Completed

## Context

ADR-003 names the MVP logical services, while the deployment architecture separates control, execution, and storage responsibilities and keeps execution state externalized. Before production Kubernetes manifests exist, contributors need a repeatable local composition that exercises the same service boundaries.

The local topology may use in-memory or local adapters, but configuration must bind exactly one repository and Workspace and must not collapse architectural ownership into hidden global state.

## Deliverable

Create local MVP service composition that:

* starts the event/resource control components and workflow, Agent Resolver, Context Builder, Tool, and Evaluation services or adapters;
* wires explicit configuration, ports, health checks, and local persistence dependencies;
* identifies one repository, Workspace, and execution environment;
* documents startup, shutdown, configuration, and health-verification commands; and
* includes a smoke test that starts the composition and resolves basic Resources without external credentials.

## Dependencies

* AEP-003
* AEP-004

## Acceptance Criteria

* All MVP services can start locally.
* Services expose health endpoints.
* Services can use in-memory or local storage adapters.
* Configuration identifies one repository and workspace.
* Smoke test starts services and resolves basic configuration.

## Implementation Notes

The local Docker Compose topology preserves the seven ADR-003 service
boundaries while using a shared, provider-neutral Python HTTP adapter for MVP
startup and health. Each container receives explicit repository, Workspace,
execution-environment, port, and local-state configuration. Repository
Resources are mounted read-only from Git, and mutable local state is
externalized to a named volume. The container schema directory is passed
explicitly rather than inferred from the installed Python package location.

Every service validates the configured identity against the immutable
repository-local Workspace before becoming ready. The Resource Controller also
exposes read-only resolved Resource references. The smoke test starts all seven
adapters on ephemeral ports, verifies every health endpoint, resolves the
Workspace and Event Resource, and requires no external credentials or Docker
daemon.
