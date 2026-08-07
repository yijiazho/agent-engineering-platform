# AEP-033: Implement EvaluateAcceptance Task Handler

**Status:** Completed

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

## Implementation

`src/aep/evaluate_acceptance.py` implements the non-cognitive acceptance
handler. The handler requires the complete, ordered MVP predecessor chain from
`AnalyzeIssue` through `RunValidation`. It requires exactly one
`ISSUE_ANALYSIS`, `IMPLEMENTATION_PLAN`, `PATCH`, and `EVALUATION_REPORT`
artifact from the corresponding Tasks. For each predecessor it validates the
TaskExecution runtime schema and revision-bound provenance, resolves the loaded
Task's explicit Evaluation Resources and expected Evaluation types, and loads
attached GeneratedArtifacts and EvaluationResults from their stores.

Persisted EvaluationResults and their AgentInvocation or ToolInvocation targets
are schema-validated and must identify correlated producer evidence: schema
Evaluations target AgentInvocations, patch Evaluation targets the producer
PATCH, and build/test Evaluations target ToolInvocations.
Invocation targets bind to the revision-validated producer TaskExecution and
EvaluationResult because their runtime contracts do not require a revision
field; any optional revision recorded in invocation provenance must match.
Artifact attachments, content, Evaluation attachments, terminal states,
failure evidence, trace, WorkflowExecution, producer identity, repository
revision, and provenance must remain mutually consistent.

The handler resolves exactly one versioned `Evaluation` of type `acceptance`
from its own Task and persists one idempotent acceptance-summary
`EvaluationResult`. Missing, failed, stale, cross-execution, or inconsistent
evidence produces a terminal `FAIL` summary and an Evaluation-class Task
failure. Configuration errors that prevent the evidence scope from being
established do not manufacture a summary. The implementation has no Model or
Agent dependency and performs no policy or publication action.

Focused tests cover the complete four-Task chain, all-pass aggregation, missing,
duplicate, and substituted artifacts, missing and failed evaluations, stale
execution and evidence revisions, malformed or invalid targets, cross-execution and
attachment inconsistency, idempotent replay, the Resource fixture, and invalid
handler configuration.
