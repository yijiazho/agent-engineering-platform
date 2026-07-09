# AEP-007: Resolve Workflow For Event

**Status:** Not Started

## Context

The Event Controller maps normalized Events to Workflow Resources without embedding workflow logic.

## Deliverable

Implement Workflow resolution based on event triggers declared in Workflow Resources.

## Dependencies

* AEP-003
* AEP-005

## Acceptance Criteria

* Resolver matches `github.issue.created` to `issue-to-pr`.
* Resolver returns explicit Workflow version.
* Resolver returns no match when no trigger exists.
* Resolver fails on multiple ambiguous matches unless policy allows fan-out.
* Tests use Resource Loader fixtures.
