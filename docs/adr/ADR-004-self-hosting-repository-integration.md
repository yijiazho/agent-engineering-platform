# ADR-004: Self-Hosting Repository Integration

**Status:** Accepted

**Authors:** Project Team

**Date:** 2026-08-06

---

# Context

ADR-003 defines the generic MVP as one repository, one Workspace, and one
`github.issue.created` to pull-request workflow. This repository is intended to
be the first live integration so maintainers can request improvements through
ordinary GitHub Issues and review AEP-generated changes through ordinary pull
requests.

The current local composition binds this repository's identity but exposes only
health and Resource discovery. It does not authenticate webhook ingress,
provision mutable execution checkouts, supply live provider integrations, load
a complete `issue-to-pr` Resource bundle, or wire the full control loop.

Self-hosting also introduces a bootstrap hazard: a running control plane must
not mutate the source, configuration, or binaries from which it is currently
executing. Repository events are untrusted external input, and neither an event
payload nor an Agent may select an arbitrary repository or obtain credentials
outside the configured Workspace.

---

# Decision

AEP will use a repository-bound, generational self-hosting model.

One pinned AEP deployment is registered for exactly
`github:yijiazho/agent-engineering-platform` and exactly one immutable
Workspace version. The deployed version and its Resource checkout are
read-only. Each accepted issue creates or claims a separate revision-bound,
writable execution worktree. The running version may generate and publish an
unmerged pull request proposing the next version, but it cannot merge, deploy,
or replace itself.

Registration consists of six explicit capabilities:

1. Authenticated GitHub webhook ingress bound to the configured repository.
2. Trusted provisioning of clean, isolated, revision-pinned execution
   checkouts.
3. A complete, explicitly versioned `.ai/issue-to-pr` Resource bundle in this
   repository.
4. A least-privilege GitHub App integration for issue reads, repository fetch
   and branch push, and pull-request creation.
5. At least one live Model provider adapter selected by a Model Resource, with
   credentials supplied only through deployment secrets.
6. A pinned operational deployment and controlled dogfood pilot with durable,
   auditable runtime evidence.

These capabilities are tracked by AEP-038 through AEP-043. They extend the
generic runtime work in AEP-031 through AEP-037 rather than replacing it.

---

# Repository And Revision Binding

Webhook repository identity, Workspace repository identity, repository source,
checkout, WorkflowExecution, knowledge snapshot, ContextPackages, Tool
invocations, artifacts, evaluations, policies, Git push, and pull-request target
must all agree on one repository and immutable base revision.

The webhook payload is not authority to choose a repository. A mismatch is
rejected before Workflow resolution. Multi-repository routing and dynamic
repository onboarding remain out of scope.

The Git Tool remains bounded to an existing worktree. Clone, fetch, revision
selection, worktree creation, and cleanup are trusted control-plane
responsibilities and are not exposed to Agents as Tools.

---

# Authentication And Provider Boundaries

Ingress verifies the GitHub signature over the raw body before parsing the
payload. Delivery identity supplies the deduplication key. The webhook secret,
GitHub App private key and installation tokens, Git credentials, and Model
provider credentials are deployment secrets, never declarative Resources or
runtime evidence.

GitHub provider operations continue through the Git and GitHub Tool contracts
and their separate capability and publication gates. Model provider operations
continue through ModelInvocation and are never represented as Tools. Provider
implementations may translate protocols and failures but may not retrieve
repository knowledge, schedule Tasks, or authorize publication.

---

# Publication And Promotion

The dogfood workflow preserves ADR-003's six Tasks and deterministic evaluation
requirements. Publication requires passing patch, build, test, and acceptance
evidence; an allowing Publication Policy decision; and separate allowing
`git.push` and `github.create_pr` capability decisions.

The only permitted external result is an unmerged pull request. Human review
and merge are outside AEP. Deployment of the merged result occurs through a
separate release process, producing a new pinned control-plane version.

---

# Consequences

## Benefits

* The first integration exercises the platform against its own real contracts,
  tests, documentation, and governance rules.
* A running release remains recoverable because generated changes cannot alter
  it in place.
* Repository and credential scope are explicit and auditable.
* Registration can later be generalized without weakening the MVP's
  single-repository boundary.

## Trade-offs

* The pilot requires external GitHub App and Model provider configuration in
  addition to repository Resources.
* One deployment cannot service arbitrary repositories.
* Checkout storage, credential rotation, provider outages, and ambiguous remote
  mutations become operational concerns.
* Self-improvement remains asynchronous: a human must review and merge the PR,
  then release and deploy the next version.

---

# Rejected Alternatives

## Modify The Running Checkout

Rejected because a partial or incorrect patch could corrupt the active control
plane and its audit trail.

## Trust The Repository Named By The Webhook

Rejected because an untrusted event could redirect credentials or execution to
an unauthorized repository.

## Grant Agents Direct GitHub, Git, Or Repository Access

Rejected because it would bypass deterministic context construction, Tool
schemas, capability policy, publication policy, and immutable evidence.

## Automatically Merge Or Deploy Generated Pull Requests

Rejected for the MVP because human-reviewed promotion is the safety boundary
between a proposed self-improvement and the next running release.

### Evidence-bound planning

Self-hosting planning treats ranked repository paths as candidates only.
Required mutations need deterministic, revision- and digest-bound predicate
evidence. The versioned GeneratePatch contract will require an explicit
`CHANGE` or postcondition-proven `NO_CHANGE` for every exact editable target;
late narrowing must be durably recorded before use and never rewrites the
evaluated implementation plan. Until that atomic resource/runtime generation
is installed, the existing missing-required-path guard remains authoritative.
