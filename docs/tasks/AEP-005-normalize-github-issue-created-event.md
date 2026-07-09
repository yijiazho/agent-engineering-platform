# AEP-005: Normalize GitHub Issue Created Event

**Status:** Not Started

## Context

The MVP starts from a GitHub Issue Created webhook and converts it into a platform Event object.

## Deliverable

Implement webhook normalization for `github.issue.created`.

## Dependencies

* AEP-001

## Acceptance Criteria

* Normalized Event includes id, source, type, repository, issue, sender, receivedAt, and deduplicationKey.
* Duplicate webhook deliveries produce the same deduplication key.
* Invalid payloads fail with structured errors.
* Normalizer does not start workflows directly.
* Tests include real-shaped GitHub issue payload fixtures.
