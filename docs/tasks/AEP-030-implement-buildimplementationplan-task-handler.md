# AEP-030: Implement BuildImplementationPlan Task Handler

**Status:** Not Started

## Context

BuildImplementationPlan turns issue analysis and repository context into a concrete plan.

## Deliverable

Implement Task handler for implementation planning.

## Dependencies

* AEP-029

## Acceptance Criteria

* Handler consumes prior issue analysis GeneratedArtifact.
* Handler creates deterministic ContextPackage.
* Handler persists implementation plan GeneratedArtifact.
* Handler evaluates required plan sections.
* Tests cover successful plan and missing required section.
