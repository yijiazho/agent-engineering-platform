# AEP-027: Implement Build And Test Evaluation

**Status:** Not Started

## Context

The Docker validation Tool records command execution; it does not interpret those results as Task success. Build and Test Evaluation converts captured exit codes, logs, timing, timeouts, and missing results into deterministic EvaluationResults.

Build and test outcomes must remain separately visible even when the overall Task fails, providing immutable evidence for acceptance evaluation, Publication Policy, and debugging.

## Deliverable

Implement build and test Evaluation that:

* consumes a completed Docker ToolInvocation and the configured build/test expectations;
* creates separate build and test EvaluationResults with status, duration, logs reference, and evidence;
* handles nonzero exits, timeouts, missing commands, and incomplete output deterministically;
* performs no LLM reasoning or publication decision; and
* tests pass, build failure, test failure, timeout, and missing validation output.

## Dependencies

* AEP-002
* AEP-023

## Acceptance Criteria

* Evaluation records build status, test status, logs reference, and duration.
* Nonzero build exit fails build evaluation.
* Nonzero test exit fails test evaluation.
* Missing validation output fails configuration validation.
* Tests cover passing, build failing, test failing, and timeout cases.
