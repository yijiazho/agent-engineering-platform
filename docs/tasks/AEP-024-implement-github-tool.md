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
binds the CreatePullRequest task and decision while allowing artifacts and
evaluations to retain their owning GeneratePatch, RunValidation, or
EvaluateAcceptance tasks. Every record must share the WorkflowExecution,
repository revision, and trace. The verifier also requires a successful Git
push ToolInvocation owned by the current CreatePullRequest task. The push must
reference an immutable-version Git Tool, bind matching input and output for the
approved repository, head, and revision, and reference its own persisted
pre-execution `git.push` `ALLOW` PolicyDecision with matching task, trace,
workflow, revision, and target. Publication separately binds the exact base,
`PUBLICATION` gate, and `github.create_pr` action. Changed or incomplete targets
are denied before the capability hook. Caller-supplied decision fields are
rejected. Issue reads use `github.issue.read`.

Provider operations use a cancellable execution handle so the Tool Runtime can
enforce timeout, termination, kill, and cleanup without a synchronous network
call blocking adapter startup. Rate limits and failures produce immutable
per-attempt evidence including retryability, retry-after hints, provider request
ID, classification, outcome, and trace ID. Read-only issue requests honor
retry-after within one invocation deadline and may retry within a configured bound.
Timeouts retain the same evidence plus whether an incomplete provider response
makes publication ambiguous; the timed-out operation is terminated, killed if
needed, and cleaned up without replay.
Pull-request creation is attempted once so that a provider failure cannot
silently duplicate an external publication; retry orchestration can use the
reported classification.

The operation schemas are published under `schemas/tools/v1/`, with
deterministic examples under `fixtures/github-tool/`. Tests inject a fake
client and cover reads, successful publication, gate ordering and denial,
invalid input, rate limits, retries, provider failures, and metadata.
