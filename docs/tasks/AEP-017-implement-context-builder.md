# AEP-017: Implement Context Builder

**Status:** Not Started

## Context

The Context Builder is the only subsystem permitted to assemble repository, documentation, event, policy, workflow-history, and prior-artifact information for an Agent. It constructs the minimum sufficient context for a Task; it does not reason, execute Tools, or optimize Agent behavior.

For the MVP, construction combines the simplified knowledge API with loaded Resources and stored runtime artifacts. The result must be immutable, deterministic for identical inputs, budget-aware, and provenance-complete so the runtime can validate it before invocation.

## Deliverable

Implement MVP ContextPackage construction that:

* resolves each Task's mandatory and optional context requirements;
* retrieves repository knowledge only through AEP-016 and prior artifacts through AEP-018;
* assembles Task, Event, repository, knowledge, policy, and artifact sections with provenance;
* records budget, selection, truncation, and token-estimate metadata; and
* validates completeness and determinism with fixtures for every MVP Task type.

## Dependencies

* AEP-003
* AEP-004
* AEP-016
* AEP-018

## Acceptance Criteria

* ContextPackage includes Task, Event, repository, knowledge, policy, and prior GeneratedArtifact context as applicable.
* Every context element includes provenance.
* Required context constraints are validated.
* Token budget metadata is recorded, even if approximate.
* Context construction is deterministic for identical inputs.
* Tests cover each MVP Task type.
