# AEP-025: Implement Schema Evaluation

**Status:** Completed

## Context

AnalyzeIssue, BuildImplementationPlan, and other model-backed Tasks declare structured output contracts. Schema Evaluation provides deterministic technical validation of those outputs before they become trusted artifacts or inputs to downstream Tasks.

This evaluator produces immutable EvaluationResult evidence and never calls an LLM. It validates the declared schema version, target content, and failure details while remaining independent of publication authorization.

## Deliverable

Implement schema Evaluation that:

* accepts an Evaluation reference, target artifact or invocation output, and declared JSON Schema;
* returns and persists an EvaluationResult with pass/fail, evidence, logs, provenance, and trace data;
* reports stable, actionable validation paths and messages;
* performs no model or policy decisions; and
* tests valid output, missing fields, invalid types, malformed schemas, and deterministic error ordering.

## Dependencies

* AEP-002

## Acceptance Criteria

* EvaluationResult records evaluatorRef, target, pass/fail, logs, and evidence.
* Invalid model output fails deterministically.
* Evaluation does not call an LLM.
* Tests cover valid output, missing fields, and invalid types.
