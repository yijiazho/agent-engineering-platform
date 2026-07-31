# AEP-024: Implement GitHub Tool

**Status:** Completed

## Context

GitHub is both the MVP event source and the external publication target. Interactions must use a Tool adapter so issue reads and pull-request creation have stable schemas, policy decisions, retry classification, trace metadata, and auditable responses.

Reading issue data is distinct from publishing a PR. PR creation may occur only after technical evaluation, Publication Policy, and pre-execution authorization; branch creation and push remain responsibilities of the Git Tool.

## Deliverable

Implement a GitHub Tool adapter that:

* defines structured operations for reading an issue and creating a pull request;
* maps provider responses and errors into stable AEP result and failure types;
* requires both Publication Policy and `github.create_pr` authorization for PR creation;
* records repository, issue or PR identifiers, URLs, provider request IDs, and trace metadata; and
* uses a fake client to test reads, successful publication, denial, invalid input, rate limits, and provider failures.

## Dependencies

* AEP-019
* AEP-020

## Acceptance Criteria

* Read issue returns structured issue data.
* Create pull request accepts branch, title, body, and base branch.
* PR creation requires Pre-Execution Capability Policy and Publication Policy.
* Adapter records GitHub response metadata.
* Tests use a fake GitHub client.

## Implementation

`src/aep/github_tool.py` implements a provider-neutral GitHub client boundary
and a Tool Runtime adapter for `readIssue` and `createPullRequest`. The adapter
normalizes issue and pull-request identifiers, URLs, repository identity,
provider request IDs, attempt counts, and the Tool trace ID.

Pull-request creation supplies only immutable runtime-object identifiers to a
trusted Publication Policy verifier. The verifier resolves persisted
`GeneratedArtifact`, `EvaluationResult`, and `PolicyDecision` evidence and
binds task and workflow execution, repository and revision, trace, artifact,
evaluation, `PUBLICATION` gate, and `github.create_pr` action. It checks passing
technical evidence and an `ALLOW` decision before the shared pre-execution
authorization hook runs. Caller-supplied decision fields are rejected. Issue
reads use `github.issue.read`.

Provider operations use a cancellable execution handle so the Tool Runtime can
enforce timeout, termination, kill, and cleanup without a synchronous network
call blocking adapter startup. Rate limits and failures produce immutable
per-attempt evidence including retryability, retry-after hints, provider request
ID, classification, outcome, and trace ID. Read-only issue requests honor
retry-after within one invocation deadline and may retry within a configured bound.
Pull-request creation is attempted once so that a provider failure cannot
silently duplicate an external publication; retry orchestration can use the
reported classification.

The operation schemas are published under `schemas/tools/v1/`, with
deterministic examples under `fixtures/github-tool/`. Tests inject a fake
client and cover reads, successful publication, gate ordering and denial,
invalid input, rate limits, retries, provider failures, and metadata.
