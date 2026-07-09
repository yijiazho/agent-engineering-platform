# AEP-028: Implement Publication Policy

**Status:** Not Started

## Context

CreatePullRequest should only run after required evaluation results pass and policy allows publication.

## Deliverable

Implement Publication Policy evaluator for pull request creation.

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
