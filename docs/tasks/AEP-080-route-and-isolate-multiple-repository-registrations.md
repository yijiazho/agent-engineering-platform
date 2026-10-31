# AEP-080: Route And Isolate Multiple Repository Registrations

**Status:** Not Started

## Context

AEP-079 introduces explicit repository registrations. AEP-008 already binds
every event, execution, artifact, approval, and publication to one repository.
The next step is to route webhook deliveries among active registrations and
enforce registration scope throughout the runtime.

ADR-007 requires signature-first routing, stable deduplication across
generations, registration-scoped state and artifacts, and no cross-registration
reads or publication.

## Deliverable

Add multi-registration webhook routing, registration-scoped construction of
Resource collections and providers, registration-scoped runtime and artifact
queries, worker allowlists, and inspection filtering.

## Dependencies

* AEP-038
* AEP-041
* AEP-073
* AEP-074
* AEP-075
* AEP-077
* AEP-079

## Acceptance Criteria

* Ingress verifies the GitHub App signature before parsing identity, then routes
  by stable numeric repository ID to one `ACTIVE` registration generation.
* The stable deduplication key is
  `github:repository:<repository-id>:delivery:<delivery-id>`.
* A redelivery returns the original Event, registration ID, and generation even
  when a newer generation is active; it never re-routes to the active
  generation.
* Unregistered, suspended, renamed, or repository-mismatched deliveries are
  rejected before persistence with stable response codes.
* Each registration constructs its own Resource collection, Workspace,
  validation configuration, GitHub App provider, source credentials, knowledge
  snapshot, policy bindings, and artifact namespace.
* Every Event, WorkflowExecution, TaskExecution, ContextPackage,
  AgentInvocation, ModelInvocation, ToolInvocation, EvaluationResult,
  PolicyDecision, Approval, GeneratedArtifact, and ExecutionEvent carries
  `registrationId`.
* Runtime, queue, lease, checkout, artifact, approval, and inspection queries are
  registration-scoped. A cross-registration read is denied by default.
* Artifact object keys and catalog records are registration-scoped; cross-
  registration deduplication is disabled unless an explicit import is recorded.
* A worker may hold capabilities for a declared registration set, but executes
  one registration's WorkflowExecution at a time and never mixes worktrees,
  context, credentials, or artifacts.
* `DRAINING` registrations reject new Events and new WorkflowExecutions but
  permit redispatch of already accepted executions, including approval wake-up
  and crash recovery.
* Publication uses the registration's repository, branch, policy evidence,
  GitHub App binding, and publication service scope.
* The CLI filters by registration and denies cross-registration inspection
  without an explicit future operator role.
* Tests cover routing, duplicate delivery across generation rollover,
  unregistered and suspended repositories, rename/transfer, cross-registration
  artifact and execution isolation, worker allowlists, and DRAINING redispatch.
* Operator documentation describes adding, suspending, resuming, and removing a
  registration and the isolation guarantees.

## Out Of Scope

* Cross-repository WorkflowExecution.
* Organization-wide artifact import.
* Multi-tenant user authorization.

## Design References

* [ADR-007](../adr/ADR-007-repository-registration-and-multi-repository-isolation.md)
* [Repository Registration](../architecture/repository-registration.md)
* [Small-Scale Deployment](../architecture/small-scale-deployment.md)
