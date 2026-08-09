# AEP-039: Provision Revision-Bound Execution Checkouts

**Status:** Completed

## Context

Resource discovery may use a read-only repository mount, but GeneratePatch and
RunValidation require a clean, writable checkout dedicated to one
WorkflowExecution. The existing Git Tool deliberately operates only on an
already-provisioned worktree and does not clone, fetch, select a base revision,
or manage checkout lifetime.

Checkout provisioning belongs to trusted control-plane infrastructure, not to
an Agent or a general-purpose Tool. It must bind the remote repository, exact
40-character commit, repository-knowledge snapshot, execution branch, and
workspace path without exposing credentials or allowing one execution to
observe another execution's changes.

## Deliverable

Implement a provider-neutral repository source and execution-checkout manager
that:

* resolves and materializes an immutable default-branch revision from the
  configured repository through an injected credential boundary;
* creates an isolated, clean, writable worktree and unique safe branch for each
  WorkflowExecution;
* supplies the validated path, repository identity, base revision, branch, and
  lifecycle metadata to Repository Knowledge, Filesystem, Git, and Docker
  boundaries;
* makes provisioning and retry idempotent for the same execution while
  preventing path, branch, and revision reuse across executions; and
* performs bounded cleanup only after terminal evidence is durable, retaining
  actionable failure and recovery metadata.

## Dependencies

* AEP-015
* AEP-021
* AEP-022
* AEP-035
* AEP-036

## Acceptance Criteria

* Provisioning produces a clean Git worktree at the exact recorded revision and
  a deterministic execution-scoped branch acceptable to the Git Tool.
* Repository scanning, context construction, patch evidence, Docker mounts, and
  publication evidence use the same repository identity and revision.
* A retry returns the same valid checkout; concurrent claims cannot create two
  owners for one execution or share a writable checkout between executions.
* Revision drift, dirty source state, missing commits, unsafe paths or branches,
  credential failure, and interrupted provisioning fail with explicit
  recoverable or configuration classifications.
* Credentials are short-lived where supported, are not persisted in Git
  configuration or remote URLs, and are redacted from logs and errors.
* Tests use temporary local Git repositories and fake credential/source
  providers to cover success, retry, concurrency, drift, isolation, cleanup,
  and recovery without accessing a remote repository.
* Deployment and operator documentation describe source-cache, writable
  worktree, storage, cleanup, and recovery requirements.
