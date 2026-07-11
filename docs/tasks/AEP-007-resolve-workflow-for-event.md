# AEP-007: Resolve Workflow For Event

**Status:** Not Started

## Context

The Event Controller must map a normalized Event to declarative Workflow triggers without embedding workflow logic in the provider adapter. Resolution happens after normalization and before WorkflowExecution creation, using the deterministic, versioned Resource collection loaded from Git.

For the MVP, `github.issue.created` selects the single `issue-to-pr` Workflow. The resolver must preserve the explicit Workflow version, return a clear no-match result, and treat multiple matches as a configuration error unless fan-out is explicitly enabled.

## Deliverable

Implement a Workflow resolver that:

* accepts a normalized Event and loaded Workflow Resources;
* matches declared trigger source and event type;
* returns one explicitly versioned Workflow reference or a no-match result;
* reports ambiguous and invalid trigger configurations through structured errors; and
* includes unit tests backed by Resource Loader fixtures.

## Dependencies

* AEP-003
* AEP-005

## Acceptance Criteria

* Resolver matches `github.issue.created` to `issue-to-pr`.
* Resolver returns explicit Workflow version.
* Resolver returns no match when no trigger exists.
* Resolver fails on multiple ambiguous matches unless policy allows fan-out.
* Tests use Resource Loader fixtures.
