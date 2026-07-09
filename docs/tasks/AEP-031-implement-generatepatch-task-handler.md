# AEP-031: Implement GeneratePatch Task Handler

**Status:** Not Started

## Context

GeneratePatch applies code and test changes using scoped tools.

## Deliverable

Implement Task handler for patch generation.

## Dependencies

* AEP-021
* AEP-022
* AEP-026
* AEP-030

## Acceptance Criteria

* Handler consumes implementation plan GeneratedArtifact.
* Handler uses only Tools allowed by ResolvedAgent and policies.
* Handler persists patch GeneratedArtifact.
* Handler records changed files.
* Handler runs patch Evaluation.
* Tests cover successful patch and disallowed file change.
