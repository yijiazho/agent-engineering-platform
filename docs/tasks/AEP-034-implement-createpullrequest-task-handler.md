# AEP-034: Implement CreatePullRequest Task Handler

**Status:** Not Started

## Context

CreatePullRequest publishes the generated change after evaluation and policy allow it.

## Deliverable

Implement PR creation Task handler.

## Dependencies

* AEP-024
* AEP-028
* AEP-033

## Acceptance Criteria

* Handler evaluates Publication Policy before creating PR.
* Handler evaluates Pre-Execution Capability Policy for git push and GitHub create PR.
* Handler pushes branch.
* Handler creates pull request.
* Handler persists PR description GeneratedArtifact and PR URL.
* Tests use fake Git and GitHub clients.
