# AEP-079: Implement Repository Registration Control Plane

**Status:** Not Started

## Context

The current deployment binds exactly one repository and one Workspace identity.
The dogfood profile hardcodes the repository, the local Compose profile mounts
this repository, and the webhook rejects any other repository. ADR-007 defines
an explicit repository registration control plane so additional repositories can
be registered without allowing an event to select or create one.

Registration is operational state, not a new declarative Resource. `Workspace`
remains the declarative repository binding.

## Deliverable

Implement the `RepositoryRegistration` runtime object, registration manifest and
CLI, validation pipeline, immutable generations, lifecycle transitions, and
operator evidence for explicitly registering repositories.

## Dependencies

* AEP-038
* AEP-041
* AEP-073
* AEP-074
* AEP-075
* AEP-077

## Acceptance Criteria

* A `RepositoryRegistration` runtime object records registration ID, generation,
  repository ID, owner/name, default branch, Workspace ref, resource revision,
  App installation, webhook secret reference, validation result, and state
  namespace.
* Registration is an explicit operator operation. No webhook payload, issue
  label, or Resource annotation can create or activate a registration.
* Registration validation checks repository access, App installation and
  permissions, `.ai/workspace.yaml`, schema validity, explicit Resource
  versions, Event-to-Workflow resolution, Task DAG, validation image,
  KnowledgeBase paths, secret references, and state namespace isolation.
* Validation records stable check identifiers and safe reasons without
  repository bodies, issue text, provider responses, or secrets.
* Lifecycle supports `PENDING`, `VALIDATING`, `INVALID`, `ACTIVE`, `DRAINING`,
  `SUSPENDED`, and `REMOVED` with durable evidence.
* At most one generation per repository ID is `ACTIVE`; activation uses a
  compare-and-swap transaction and moves the previous generation to `DRAINING`.
* A validating successor may coexist with an active predecessor; validation does
  not require an ingress interruption.
* Resource revision, Workspace, validation image, App binding, or state scope
  changes create a new immutable generation. In-flight executions remain pinned
  to their original generation.
* The CLI supports register, list, show, validate, suspend, resume, and remove
  with machine-readable output and redacted evidence.
* The existing single-repository deployment migrates to generation 1 without
  changing object IDs or historical evidence.
* Tests cover valid registration, missing or invalid bundle, identity mismatch,
  missing App permission, resource revision drift, duplicate registration,
  concurrent activation, suspension/resumption, and removal.
* Operator documentation describes registration prerequisites, lifecycle, and
  failure recovery.

## Out Of Scope

* Multi-registration webhook routing and cross-registration isolation
  (AEP-080).
* Automatic `.ai` bundle generation.
* Cross-repository WorkflowExecution.

## Design References

* [ADR-007](../adr/ADR-007-repository-registration-and-multi-repository-isolation.md)
* [Repository Registration](../architecture/repository-registration.md)
* [Small-Scale Deployment](../architecture/small-scale-deployment.md)
