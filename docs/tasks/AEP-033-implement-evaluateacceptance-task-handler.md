# AEP-033: Implement EvaluateAcceptance Task Handler

**Status:** Not Started

## Context

EvaluateAcceptance combines deterministic evaluations before publication.

## Deliverable

Implement acceptance evaluation Task handler.

## Dependencies

* AEP-028
* AEP-032

## Acceptance Criteria

* Handler reads required EvaluationResults.
* Handler verifies required GeneratedArtifacts exist.
* Handler produces final EvaluationResult summary.
* Handler does not call an LLM for MVP.
* Tests cover all pass, missing evaluation, and failed evaluation.
