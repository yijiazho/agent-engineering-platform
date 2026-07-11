# AEP-028: Implement Publication Policy

**Status:** Not Started

## Context

Publication Policy is the final governance gate between evaluated workflow output and an external effect such as creating a pull request. It is distinct from Pre-Execution Capability Policy: technical evidence is considered here, while the Git and GitHub capabilities are authorized again immediately before execution.

For the MVP, PR publication requires successful patch generation, completed validation, all required passing EvaluationResults, and no earlier policy violation. The decision must be reproducible and explain which evidence and versioned rules were applied.

## Deliverable

Implement the MVP Publication Policy evaluator that:

* accepts the candidate action, required artifacts and evaluations, prior policy state, and applicable Policies;
* verifies required evidence exists and every required EvaluationResult passed;
* returns and persists `ALLOW`, `DENY`, or `REQUIRE_APPROVAL` with matched rules and reasons;
* never performs Git push or PR creation itself; and
* tests passing evidence, missing evidence, failed evaluation, prior denial, and approval-required outcomes.

## Dependencies

* AEP-004
* AEP-020
* AEP-025
* AEP-026
* AEP-027

## Acceptance Criteria

* Publication Policy requires patch generation success.
* Publication Policy requires validation ran.
* Publication Policy requires required EvaluationResults are present.
* Publication Policy denies when any required evaluation failed.
* PolicyDecision records evaluated rule and reason.
* Tests cover allow, deny, and require approval.
