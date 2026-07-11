# AEP-034: Implement CreatePullRequest Task Handler

**Status:** Not Started

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
