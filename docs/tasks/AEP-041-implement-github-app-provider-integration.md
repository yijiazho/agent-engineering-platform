# AEP-041: Implement GitHub App Provider Integration

**Status:** Completed

## Context

The Git and GitHub Tools have provider-neutral contracts, but live
self-hosting requires a concrete GitHub identity that can read the triggering
issue, fetch and push the execution branch, and create a pull request. The
current adapters rely on injected protocols and tests use fakes; they do not
acquire GitHub App installation tokens or call the live provider.

GitHub authentication must remain outside declarative Resources. Installation
tokens are short-lived capabilities scoped to one configured repository, and
their use must preserve the existing policy, idempotency, evidence, failure,
and redaction boundaries.

## Deliverable

Implement a GitHub App integration that:

* resolves the installation for the bound repository and obtains renewable,
  short-lived installation tokens through an injected secret and HTTP
  transport boundary;
* implements the GitHub client operations required by the GitHub Tool for issue
  reads and pull-request creation;
* implements the Git credential lease required by the Git Tool for authenticated
  fetch and push without persisting credentials in the checkout;
* maps authentication, authorization, validation, rate-limit, retryable server,
  ambiguous mutation, and permanent provider failures into existing stable
  Tool evidence; and
* exposes readiness diagnostics that prove configuration and installation
  identity without exposing private keys or tokens.

## Dependencies

* AEP-022
* AEP-024
* AEP-036
* AEP-038

## Acceptance Criteria

* The integration can read an issue, lease credentials for the bound Git
  remote, push only the authorized execution branch, and create a pull request
  through existing Tool interfaces.
* Repository owner/name, installation identity, head branch, base branch,
  expected revision, and policy evidence must match before a provider mutation.
* Token caching respects expiry and concurrency; credentials are revoked or
  discarded after use and never appear in URLs, process arguments, logs,
  exceptions, ToolInvocation bodies, or GeneratedArtifacts.
* Rate limits and transient failures return bounded retry metadata, while
  authorization and validation failures are non-retryable; ambiguous push or
  PR outcomes retain an explicit unknown mutation state for reconciliation.
* PR creation remains idempotent under retry and cannot bypass Publication
  Policy or the separate `git.push` and `github.create_pr` capability decisions.
* Tests use a scripted fake HTTP transport and local Git remote to cover token
  renewal, issue read, push, PR creation, duplicate reconciliation, rate limit,
  permission denial, redaction, and ambiguous provider failure without live
  credentials.
* Operator documentation lists the GitHub App webhook subscription, repository
  permissions, secret inputs, installation procedure, and credential rotation
  process.

## Implementation Notes

`aep.github_app_provider` implements repository-bound GitHub App JWT signing,
installation resolution, concurrency-safe renewable token caching, a bounded
HTTP transport, the existing GitHub client operations, duplicate pull-request
reconciliation, one-use Git askpass credential leases for checkout fetch and
Tool push, safe failure classification, and credential-free readiness. The
environment factory fails closed over explicit repository, App, key-file,
default-branch, state-root, API, and branch-prefix inputs.
Sequential provider calls share one monotonic deadline and receive only its
remaining budget. Readiness revalidates installation identity on every probe,
and authentication or installation failures invalidate cached identity.
Pull-request timeout evidence distinguishes pre-mutation `NOT_ATTEMPTED` from
post-POST `UNKNOWN` state. All unconfirmed post-POST failures, including
malformed successful responses, retain that unknown state, while installation
lookup and token-request transport timeouts remain Tool `TIMED_OUT` results.

The existing GitHub Tool remains responsible for immutable publication
evidence and Publication Policy plus `github.create_pr` authorization; the Git
Tool separately requires `git.push` and retains `UNKNOWN` for ambiguous pushes.
Provider tests use a scripted transport and local bare Git remote to cover
renewal, concurrency, issue read, authorized push, successful PR creation,
duplicate reconciliation, rate limiting, binding denial, redaction, and
ambiguous mutation without live credentials. Operator setup and rotation are
documented in `docs/operations/github-app.md`.
