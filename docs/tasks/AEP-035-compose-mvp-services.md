# AEP-035: Compose MVP Services

**Status:** Not Started

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
