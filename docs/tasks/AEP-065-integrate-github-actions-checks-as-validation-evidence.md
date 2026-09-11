# AEP-065: Integrate GitHub Actions Checks as Validation Evidence

**Status:** Not Started

## Context

AEP should focus on AI reasoning, context, evaluation, and governance instead of rebuilding commodity CI/CD. Repositories already express builds, tests, security scans, and integration checks in GitHub Actions. The platform needs a governed way to trigger or observe those checks and consume their results as deterministic validation evidence.

The integration must not make GitHub Actions the source of truth for AEP Resource resolution or AI execution provenance. It should treat external CI as a Tool/integration boundary whose check identity, revision, status, and outputs are captured as runtime evidence.

## Deliverable

Implement a GitHub Actions/checks integration that can request or observe configured workflows/check runs for an exact repository revision, wait for terminal status, normalize results, and expose them to AEP Evaluation and publication policy.

Support repository configuration that identifies allowed workflow/check names and whether AEP may trigger a workflow or only consume an already-running check.

## Dependencies

* AEP-019
* AEP-020
* AEP-024
* AEP-027
* AEP-028
* AEP-041
* AEP-051
* AEP-056

## Acceptance Criteria

* A versioned Tool or equivalent integration contract declares allowed GitHub Actions workflow/check identities and permitted operations using exact repository scope.
* AEP can correlate a check run with the exact candidate commit/repository revision being validated and rejects successful checks from a different revision as acceptance evidence.
* The integration can normalize queued, in-progress, success, failure, cancelled, timed-out, and unavailable states into stable AEP runtime evidence.
* Triggering a GitHub Actions workflow is capability-policy governed and optional; repositories can configure observe-only behavior.
* Polling or webhook reconciliation is idempotent and does not create duplicate validation TaskExecutions or duplicate workflow dispatches after restart.
* EvaluationResult can cite normalized GitHub Actions evidence alongside existing Docker/build/test evidence without changing the meaning of historical evaluations.
* Publication policy can require named external checks to succeed before pull-request publication or another governed action.
* AEP-056 inspection links external check identity, revision, status, timestamps, and safe result metadata without exposing GitHub credentials or unrestricted log content.
* Tests use deterministic GitHub fixtures to cover successful, failing, stale-revision, missing, cancelled, duplicate-event, policy-denied trigger, and restart scenarios.
* Architecture and operator documentation define the boundary: GitHub Actions owns configured CI execution while AEP owns AI workflow orchestration, evaluation, provenance, and publication policy.
