# AEP-038: Implement Authenticated GitHub Webhook Ingress

**Status:** Not Started

## Context

The MVP event components normalize and deduplicate `github.issue.created`, but
the deployed Event Controller has no HTTP endpoint that can safely receive a
GitHub webhook. A repository cannot be registered until ingress verifies the
raw delivery, binds it to the configured Workspace repository, and hands one
accepted Event to downstream repository-revision and Workflow reconciliation.

Webhook authentication is an edge responsibility. The shared secret is runtime
configuration, not a declarative Resource, and must never enter Event payloads,
logs, runtime evidence, or GeneratedArtifacts. An event naming another
repository must not be able to select Resources or create an execution in the
bound Workspace.

## Deliverable

Implement the GitHub webhook ingress for the Event Controller that:

* exposes a documented POST endpoint for GitHub `issues` deliveries;
* verifies `X-Hub-Signature-256` over the raw request body before decoding or
  trusting the payload;
* requires `X-GitHub-Event: issues` and `X-GitHub-Delivery`, accepts only the
  `opened` action, and validates the payload repository against the configured
  Workspace;
* composes normalization and shared-store deduplication, then dispatches the
  first accepted Event once through a provider-neutral reconciliation boundary;
  and
* returns stable HTTP outcomes and emits redacted, trace-correlated lifecycle
  evidence for accepted, duplicate, rejected, and failed deliveries.

## Dependencies

* AEP-005
* AEP-006
* AEP-007
* AEP-008
* AEP-035
* AEP-036

## Acceptance Criteria

* A correctly signed `issues/opened` delivery for the bound repository creates
  exactly one normalized Event and dispatches exactly one reconciliation
  request.
* Replaying the same `X-GitHub-Delivery` returns the prior accepted identity and
  does not create or schedule duplicate work.
* Missing or invalid signatures, missing delivery identifiers, unsupported
  event types or actions, malformed JSON, oversized bodies, and repository
  mismatches are rejected before Workflow execution.
* Signature comparison is constant-time, secrets and request bodies are
  excluded from logs, and configuration fails fast when the webhook secret is
  absent.
* Tests use fixed signed payloads and cover success, replay, signature failure,
  repository mismatch, unsupported delivery, and downstream persistence
  failure without requiring network access.
* README and deployment documentation identify the webhook route, required
  headers, secret injection method, response semantics, and retry behavior.
