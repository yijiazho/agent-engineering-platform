# AEP-002: Define Runtime Object Schemas

**Status:** Completed

## Context

Runtime objects capture observed execution state and evidence. They are not Resources and are not stored in `.ai/`.

## Deliverable

Define schemas for WorkflowExecution, TaskExecution, ContextPackage, ResolvedAgent, AgentInvocation, ModelInvocation, ToolInvocation, EvaluationResult, PolicyDecision, Approval, GeneratedArtifact, and ExecutionEvent.

## Dependencies

None.

## Acceptance Criteria

* Every runtime object has an identifier, timestamps, trace identifier, and provenance fields.
* Runtime objects reference Resources by immutable versioned identifiers.
* GeneratedArtifact is modeled as a runtime object, not a Resource.
* PolicyDecision supports `ALLOW`, `DENY`, and `REQUIRE_APPROVAL`.
* Fixtures cover successful, failed, and pending runtime states.
