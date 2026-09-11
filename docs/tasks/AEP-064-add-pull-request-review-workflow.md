# AEP-064: Add Pull Request Review Workflow

**Status:** Not Started

## Context

The MVP proves an issue-created to pull-request publication path. A natural second vertical workflow is pull-request review because it exercises event normalization, revision-bound context, parallel reasoning tasks, evaluation, policy, and GitHub publication without requiring a new execution model.

The workflow should demonstrate that AEP can compose specialized reviewers while keeping deterministic orchestration in the platform. Individual Agents may reason about bounded review concerns but must not control the workflow DAG or independently publish review comments.

## Deliverable

Add a versioned self-contained pull-request review Resource bundle and task handlers that react to pull-request opened or updated events, construct diff- and repository-aware context, run specialized review tasks, synthesize/evaluate findings, and publish a governed GitHub review.

At minimum the workflow must support correctness, test-coverage, and architecture/maintainability review concerns, with deterministic orchestration and one governed publication step.

## Dependencies

* AEP-007
* AEP-010
* AEP-013
* AEP-017
* AEP-024
* AEP-025
* AEP-028
* AEP-038
* AEP-041
* AEP-058
* AEP-056

## Acceptance Criteria

* GitHub pull-request opened and synchronize/update events normalize into immutable AEP Events with repository, PR, base revision, and head revision identity.
* Workflow resolution selects the PR-review Workflow using exact versioned Resource references and deduplicates repeated webhook deliveries.
* Context construction includes the PR diff plus bounded relevant repository evidence and tests tied to the exact head/base revisions.
* Correctness, test, and architecture review tasks can execute in parallel when their DAG dependencies permit; Agents cannot add arbitrary reviewer tasks at runtime.
* Reviewer outputs use typed schemas that distinguish actionable findings, severity, evidence/source locations, and no-finding results.
* A synthesis/evaluation step deduplicates overlapping findings and rejects malformed or unsupported findings before publication.
* Publication policy determines whether AEP posts an approval, request-changes review, comments, or no review action; Agents do not call GitHub publication directly.
* Reprocessing the same PR head revision does not create duplicate reviews, while a new head revision creates a distinct revision-bound execution.
* Credential-free end-to-end fixtures cover no findings, actionable findings, malformed reviewer output, policy denial, and updated PR revisions.
* Documentation includes the PR-review DAG, context contract, publication behavior, and operator troubleshooting path through AEP-056.
