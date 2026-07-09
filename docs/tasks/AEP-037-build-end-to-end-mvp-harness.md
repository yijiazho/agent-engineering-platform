# AEP-037: Build End-To-End MVP Harness

**Status:** Not Started

## Context

The platform needs one repeatable test proving issue-to-PR flow with fakes where needed.

## Deliverable

Implement an end-to-end harness for `github.issue.created` to pull request creation.

## Dependencies

* AEP-034
* AEP-035
* AEP-036

## Acceptance Criteria

* Harness loads fixture `.ai/` resources.
* Harness normalizes issue event.
* Harness executes all MVP Tasks in order.
* Harness uses fake model and fake GitHub client.
* Harness verifies GeneratedArtifacts, EvaluationResults, PolicyDecisions, and PR URL.
* Harness can be run deterministically in CI.
