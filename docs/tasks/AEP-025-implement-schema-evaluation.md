# AEP-025: Implement Schema Evaluation

**Status:** Not Started

## Context

AnalyzeIssue and BuildImplementationPlan require structured outputs.

## Deliverable

Implement Evaluation for JSON/schema validation.

## Dependencies

* AEP-002

## Acceptance Criteria

* EvaluationResult records evaluatorRef, target, pass/fail, logs, and evidence.
* Invalid model output fails deterministically.
* Evaluation does not call an LLM.
* Tests cover valid output, missing fields, and invalid types.
