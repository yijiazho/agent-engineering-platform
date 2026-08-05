# AEP-027: Implement Build And Test Evaluation

**Status:** Completed

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

## Implementation Notes

`src/aep/build_test_evaluation.py` implements deterministic build and test
evaluation over a terminal Docker `ToolInvocation`. `ValidationExpectation`
binds each immutable Evaluation reference to a distinct ordered command index.
The invocation must exactly match the caller-supplied canonical Docker Tool
reference and contain consistent terminal runtime and Tool result statuses.
The evaluator persists separate immutable results with command status, exit
code, duration, logs address, evidence, and an evidence content address.

Nonzero exits and timeouts are technical `FAIL` outcomes. Commands skipped
after an earlier failure are retained as `NOT_RUN`. Missing output, incomplete
command records, mismatched command order, and missing configured commands are
persisted as configuration-failed EvaluationResults. Extra, reordered, or
trailing command evidence invalidates both results. Both results are built and
validated before either is stored, although the store contract does not offer
an atomic multi-create operation for backend failures between writes. The
deterministic fixture under `fixtures/build-test-evaluation/` and focused tests
cover passing output, build and test failures, timeouts, missing commands,
incomplete output, Tool and result identity, sequence corruption, duplicate
result identifiers, immutable persistence, and invalid references.
