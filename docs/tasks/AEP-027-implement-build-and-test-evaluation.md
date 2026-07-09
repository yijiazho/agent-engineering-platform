# AEP-027: Implement Build And Test Evaluation

**Status:** Not Started

## Context

RunValidation produces deterministic command results from Docker.

## Deliverable

Implement build and test EvaluationResult generation from validation ToolInvocation.

## Dependencies

* AEP-002
* AEP-023

## Acceptance Criteria

* Evaluation records build status, test status, logs reference, and duration.
* Nonzero build exit fails build evaluation.
* Nonzero test exit fails test evaluation.
* Missing validation output fails configuration validation.
* Tests cover passing, build failing, test failing, and timeout cases.
