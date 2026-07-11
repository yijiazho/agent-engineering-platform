# AEP-037: Build End-To-End MVP Harness

**Status:** Not Started

## Context

The MVP is complete only when one deterministic test proves the full ADR-003 control loop from `github.issue.created` through pull-request creation. The harness validates integration contracts and dependency wiring rather than testing live model or GitHub behavior.

It should use repository fixtures, a fake model provider, isolated/local Tool executors, and a fake GitHub client so CI can reproduce success and failure paths without network access or external credentials.

## Deliverable

Implement an end-to-end MVP harness that:

* loads fixture `.ai/` Resources and a fixture repository revision;
* normalizes and deduplicates an issue event, resolves the Workflow, and executes its Task DAG;
* exercises context, Agent, Tool, artifact, evaluation, policy, trace, and persistence boundaries using fakes where required;
* asserts final runtime history, GeneratedArtifacts, EvaluationResults, PolicyDecisions, and PR URL; and
* provides a documented deterministic CI command plus at least one blocked-publication failure scenario.

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
