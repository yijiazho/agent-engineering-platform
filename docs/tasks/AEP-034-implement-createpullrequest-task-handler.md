# AEP-034: Implement CreatePullRequest Task Handler

**Status:** Completed

## Context

`CreatePullRequest` is the publication boundary of the MVP and runs only after acceptance evaluation succeeds. It pushes the execution branch and creates a GitHub pull request linking the issue, plan, patch, and validation summary; it never merges the PR.

Publication Policy must allow the evaluated output, and Pre-Execution Capability Policy must separately authorize `git.push` and `github.create_pr`. External retries must be idempotent to avoid duplicate branches or pull requests.

## Deliverable

Implement the `CreatePullRequest` Task handler that:

* assembles a PR title and description from issue, plan, patch, and validation artifacts;
* evaluates Publication Policy before any external mutation;
* authorizes and invokes Git push and GitHub PR creation through their Tool adapters;
* persists the PR description artifact, provider identifiers, and URL with trace evidence; and
* tests success, every denial gate, partial/retried publication, provider failure, and duplicate prevention with fakes.

## Dependencies

* AEP-024
* AEP-028
* AEP-033

## Acceptance Criteria

* Handler evaluates Publication Policy before creating PR.
* Handler evaluates Pre-Execution Capability Policy for git push and GitHub create PR.
* Handler pushes branch.
* Handler creates pull request.
* Handler persists PR description GeneratedArtifact and PR URL.
* Tests use fake Git and GitHub clients.

## Implementation

`CreatePullRequestTaskHandler` consumes the successful acceptance summary and
its revision-bound issue-analysis, implementation-plan, patch, validation, and
evaluation evidence. It deterministically assembles the pull-request title and
body, evaluates Publication Policy before any remote mutation, then evaluates
separate Pre-Execution Capability Policy gates for `git.push` and
`github.create_pr`.

After `git.push` authorization, the handler invokes the repository-bound Git
Tool's `commit_changes` operation to materialize the accepted working-tree
patch after verifying its SHA-256 content address. Publication Policy then
binds both the immutable evidence base revision
and the resulting head revision before any remote mutation. The confirmed push
and GitHub verifier must resolve that exact head, preventing an empty or stale
branch from satisfying publication evidence.

Both external operations use deterministic persisted ToolInvocation identities.
Terminal retries reuse their recorded results, and an ambiguous or failed
pull-request publication is not automatically repeated. After confirmed push
and pull-request creation, the handler publishes an immutable
`PULL_REQUEST_DESCRIPTION` GeneratedArtifact containing the provider request
identifier, pull-request number and URL, policy decisions, Tool invocations,
and input evidence provenance.

Retry reconciliation spans scheduler attempts, not only repeated execution of
one TaskExecution. A clean branch can reuse the digest-bound accepted commit,
and a prior GitHub invocation is resolved by immutable workflow, base/head,
issue, description, artifact, and evaluation identity before any new provider
call. Reuse is recorded as a new current-attempt reconciliation ToolInvocation
pointing to the original provider invocation, and each attempt publishes or
reuses only an artifact it owns; TaskExecution attachments never claim
evidence owned by another attempt.

Coverage includes successful publication, every deny and approval-required
gate, push and provider failures, partial artifact-publication recovery,
duplicate prevention, and the versioned Task fixture.
The Git Tool integration coverage also uses a temporary working repository and
bare remote to prove the published head contains the accepted changed files.
