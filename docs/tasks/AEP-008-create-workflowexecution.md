# AEP-008: Create WorkflowExecution

**Status:** Not Started

## Context

When a Workflow is selected, the platform creates a WorkflowExecution runtime object as the trace root.

## Deliverable

Create WorkflowExecution from Event, Workflow Resource, repository revision, and knowledge graph version.

## Dependencies

* AEP-004
* AEP-006
* AEP-007

## Acceptance Criteria

* WorkflowExecution records eventRef, workflowRef, repositoryRevision, status, and traceId.
* Create is idempotent for the same deduplicated Event and Workflow.
* Initial status is `Pending` or `Running`.
* ExecutionEvent is emitted for creation.
* Tests verify provenance fields.
