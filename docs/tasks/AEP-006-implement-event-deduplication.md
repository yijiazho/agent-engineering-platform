# AEP-006: Implement Event Deduplication

**Status:** Completed

## Context

GitHub may deliver the same webhook more than once. AEP must avoid duplicate WorkflowExecutions.

## Deliverable

Implement deduplication for normalized Events.

## Dependencies

* AEP-004
* AEP-005

## Acceptance Criteria

* First event with a deduplication key is accepted.
* Repeated event with the same key is ignored or linked to existing execution.
* Deduplication behavior is deterministic.
* Tests cover duplicate, near-duplicate, and distinct events.

## Implementation

`EventDeduplicator` claims each normalized event's deduplication key through the
`RuntimeObjectStore` boundary. Controllers sharing a persistent store therefore
have one atomic winner, while duplicates link to the first accepted event.
