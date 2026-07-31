# AEP-008: Create WorkflowExecution

**Status:** Completed

## Context

`WorkflowExecution` is the trace root for one reconciliation of an Event against an explicitly versioned Workflow. It binds the trigger, repository revision, knowledge snapshot, and Workflow definition to the runtime state that will own all TaskExecutions.

Creation occurs only after deduplication and Workflow resolution. Because controllers may retry or race, it must be idempotent for the same Event and Workflow and preserve sufficient provenance for scheduling, audit, and trace propagation.

## Deliverable

Implement a WorkflowExecution creation service that:

* constructs and persists the initial runtime object from Event, Workflow, repository revision, and knowledge graph version;
* derives a deterministic idempotency key for the Event and Workflow pair;
* initializes status, timestamps, provenance, and trace identifier;
* emits an append-only creation `ExecutionEvent`; and
* tests retries, provenance, and initial-state behavior.

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

## Implementation

`aep.workflow_execution.WorkflowExecutionCreator` creates a `RUNNING`
WorkflowExecution from a deduplicated normalized Event and the resolved,
explicitly versioned Workflow and Event Resources. The execution binds the
normalized Event identifier, Resource references, repository revision,
knowledge graph version, initial timestamp, and a deterministic trace
identifier.

The normalized Event identifier and Workflow Resource identity form the
deterministic persistence key. Repeated and concurrent creation attempts
therefore return the first execution, while a deterministic
`WorkflowExecutionStarted` ExecutionEvent makes event emission idempotent and
allows a retry to repair an interruption after the trace root was persisted.
Both complete runtime records are validated against their authoritative JSON
Schemas, including RFC3339 timestamp formats, before the first persistence
mutation.
