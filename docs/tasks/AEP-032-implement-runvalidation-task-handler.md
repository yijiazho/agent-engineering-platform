# AEP-032: Implement RunValidation Task Handler

**Status:** Not Started

## Context

RunValidation executes deterministic build and test commands in Docker.

## Deliverable

Implement validation Task handler.

## Dependencies

* AEP-023
* AEP-027
* AEP-031

## Acceptance Criteria

* Handler invokes Docker validation Tool.
* Handler persists validation report GeneratedArtifact.
* Handler creates build and test EvaluationResults.
* Handler classifies tool failures.
* Tests cover pass, fail, and timeout.
