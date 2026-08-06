# AEP-028: Implement Publication Policy

**Status:** Completed

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

## Implementation Notes

`src/aep/publication_policy.py` implements a deterministic, fail-closed
Publication Policy evaluator. It binds the candidate publication target to an
immutable repository revision, required `GeneratedArtifact` and
`EvaluationResult` identifiers, prior policy decisions, trace provenance, and
explicitly versioned Policy resources. A required PATCH artifact and at least
one completed, passing required evaluation are mandatory. Missing, incomplete,
failed, cross-execution, or prior-denial evidence produces `DENY` before
publication rules can authorize the action.

All supplied runtime evidence is validated against its kind-specific JSON
Schema and must exactly match the immutable record in the trusted
`RuntimeObjectStore`; malformed, unpersisted, or spoofed mappings fail closed.

Applicable publication rules compose in deterministic scope/name/version order
with `DENY` over `REQUIRE_APPROVAL` over `ALLOW`. The persisted
`PolicyDecision` includes the evaluated and matched rules, reason, evidence
summary, exact artifact and evaluation identifiers, prior decision identifiers,
repository revision, and publication target. Reusing a decision identifier is
idempotent only for identical inputs. The evaluator performs no Git push or
GitHub operation.

`tests/test_publication_policy.py` covers passing evidence, missing artifacts
and evaluations, absent validation, failed and incomplete evaluation results,
prior denial, approval-required and restrictive composition outcomes,
conditional rules, fail-closed defaults, provenance mismatch, idempotency, and
identity conflicts, plus invalid and spoofed runtime evidence.
