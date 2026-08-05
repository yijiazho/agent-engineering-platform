# AEP-017: Implement Context Builder

**Status:** Completed

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

## Implementation

`src/aep/context_builder.py` implements deterministic ContextPackage assembly
over the provider-neutral Repository Knowledge and GeneratedArtifact store
boundaries. It validates TaskExecution, WorkflowExecution, trace, Resource,
artifact, repository-revision, and knowledge-snapshot relationships; requires
exact KnowledgeBase and Policy versions declared by the Task; resolves
mandatory and optional context; preserves source and selection provenance;
estimates tokens; and prunes only optional candidates. Prior artifacts must be
produced by successful dependency TaskExecutions in the same workflow, trace,
and revision. Normalized Event identity and GitHub issue fields are bound to
the WorkflowExecution event identifier before inclusion. Mandatory context
that cannot be resolved or fit within the budget fails before invocation.

The builder produces recursively immutable, schema-valid runtime evidence and
can persist it idempotently through the Runtime Object Store. The fixture in
`fixtures/context-builder/mvp-task-types.json` and focused tests cover all six
ADR-003 MVP Task types, missing and unsupported requirements, mixed revisions,
budget exhaustion, optional pruning, artifact provenance, determinism, and
immutability.
