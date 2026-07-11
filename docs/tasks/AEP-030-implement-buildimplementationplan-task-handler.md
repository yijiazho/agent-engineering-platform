# AEP-030: Implement BuildImplementationPlan Task Handler

**Status:** Not Started

## Context

`BuildImplementationPlan` converts the approved issue analysis into an actionable plan grounded in repository knowledge. Its output identifies intended files, tests, assumptions, risks, and ordered implementation steps for GeneratePatch.

The handler must consume the prior GeneratedArtifact through the Context Builder and follow the same resolved-Agent and structured-output boundaries as AnalyzeIssue. It must not modify the checkout.

## Deliverable

Implement the `BuildImplementationPlan` Task handler that:

* builds context containing the issue analysis and relevant repository knowledge;
* resolves and invokes the Planner Agent;
* persists a structured implementation-plan GeneratedArtifact;
* evaluates its schema and required sections before succeeding; and
* tests valid plans, missing prior artifacts, missing sections, and invalid output.

## Dependencies

* AEP-029

## Acceptance Criteria

* Handler consumes prior issue analysis GeneratedArtifact.
* Handler creates deterministic ContextPackage.
* Handler persists implementation plan GeneratedArtifact.
* Handler evaluates required plan sections.
* Tests cover successful plan and missing required section.
