# AEP-033: Implement EvaluateAcceptance Task Handler

**Status:** Not Started

## Context

`EvaluateAcceptance` aggregates deterministic evidence produced by earlier Tasks and determines whether the implementation satisfies the MVP workflow's required technical criteria. It does not use an LLM and does not itself authorize publication.

The handler verifies that required artifacts and EvaluationResults exist, correspond to the same execution and repository revision, and have acceptable outcomes. Its summary becomes the principal evidence consumed by Publication Policy.

## Deliverable

Implement the `EvaluateAcceptance` Task handler that:

* loads the Workflow's required GeneratedArtifacts and EvaluationResults;
* validates execution, revision, provenance, completeness, and pass/fail consistency;
* persists a final acceptance-summary EvaluationResult with supporting references;
* fails deterministically without invoking a model; and
* tests all-pass, failed, missing, stale-revision, and inconsistent evidence.

## Dependencies

* AEP-028
* AEP-032

## Acceptance Criteria

* Handler reads required EvaluationResults.
* Handler verifies required GeneratedArtifacts exist.
* Handler produces final EvaluationResult summary.
* Handler does not call an LLM for MVP.
* Tests cover all pass, missing evaluation, and failed evaluation.
