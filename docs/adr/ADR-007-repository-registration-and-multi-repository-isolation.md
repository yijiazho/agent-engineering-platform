# ADR-007: Repository Registration And Multi-Repository Isolation

**Status:** Proposed

**Authors:** Project Team

**Date:** 2026-10-27

---

# Context

ADR-004 binds the first self-hosting deployment to exactly
`github:yijiazho/agent-engineering-platform` and one immutable Workspace
version. That boundary is implemented and enforced:

* the webhook ingress accepts only the configured repository;
* the loaded `Workspace` must match the configured provider, owner, name,
  Workspace name, and Workspace version;
* the GitHub App provider resolves the installation for the bound repository
  and scopes installation tokens to it;
* the dogfood deployment hardcodes the repository and Workspace identity;
* the local Compose profile mounts this repository at `/workspace`;
* the validation Task pins this repository's validation image and commands;
* the knowledge base indexes this repository's documentation and source.

ADR-004 deliberately leaves multi-repository routing and dynamic onboarding out
of scope. The next capability is explicit registration of additional
repositories. It must not become webhook-driven onboarding, a shared Workspace,
or a shared runtime namespace.

The target repository must remain the source of its own AI behavior. AEP does
not infer, generate, or silently copy a `.ai` bundle into another repository.
Registration validates a pinned bundle; it does not author one.

This ADR defines the target contract. It does not change the current
single-repository deployment boundary, does not block the AEP-043 controlled
pilot, and does not mark any implementation task complete.

## Requirements

1. Registration is an explicit, auditable control-plane operation.
2. The webhook payload never creates a registration and never selects an
   arbitrary repository.
3. Every accepted Event, WorkflowExecution, artifact, approval, and publication
   belongs to exactly one registration and one Workspace version.
4. One registration cannot read, approve, publish, or mutate another
   registration's runtime state or artifacts.
5. Repository Resources and the validation contract remain versioned and pinned
   to an immutable revision.
6. The target repository can supply its own `.ai` bundle, validation image, and
   knowledge sources without forking AEP.
7. The current single-repository deployment remains a supported registration
   with the same semantics.
8. The design composes with distributed execution (ADR-006).

## Non-Goals

* Multi-tenant SaaS, end-user accounts, or per-user authorization.
* Automatic `.ai` generation or automatic pull-request onboarding.
* Cross-repository workflow execution in one WorkflowExecution.
* Cross-organization artifact sharing.
* Repository selection by an Agent or by issue text.
* Hot-editing the active `.ai` checkout in place.

---

# Decision

AEP will introduce an explicit `RepositoryRegistration` control-plane object and
make repository identity a first-class scope for runtime state, artifacts,
credentials, and routing.

## 1. Registration Is Operational State, Not A New Resource

`Workspace` remains the declarative repository binding. A new runtime object,
`RepositoryRegistration`, records which Workspace generation is active and the
operational evidence required to route work to it.

A new declarative `RepositoryRegistration` Resource is rejected because it
would duplicate `Workspace.repository` and blur desired configuration with
validation and activation state. Registration manifests are operator inputs;
the resulting runtime object is the durable record. A manifest uses AEP-style
YAML for readability, but it is not loaded as a declarative Resource and cannot
be referenced by Task or Agent resources.

Durable runtime object shape (abridged):

```yaml
kind: RepositoryRegistration
registrationId: registration-<deterministic-id>
generation: 1
status: ACTIVE
repository:
  provider: github
  repositoryId: 123456789
  owner: acme
  name: widgets
  defaultBranch: main
workspaceRef:
  kind: Workspace
  name: widgets
  version: 1.0.0
resourceRevision: 0123456789abcdef0123456789abcdef01234567
resourceRoot: /srv/aep/registrations/<id>/resources
githubApp:
  appId: 12345
  installationId: 67890
ingress:
  webhookPath: /v1/webhooks/github
  secretRef: github-webhook-secret
validation:
  status: PASSED
  checkedAt: 2026-10-27T00:00:00Z
  taskRef: {kind: Task, name: run-validation, version: 1.1.0}
stateNamespace: registration-<deterministic-id>
createdAt: 2026-10-27T00:00:00Z
updatedAt: 2026-10-27T00:00:00Z
```

`secretRef` is a reference, never a secret value. Registration evidence must not
contain private keys, webhook secrets, provider tokens, issue bodies, or
artifact bodies. The GitHub App installation ID may be omitted in the initial
manifest and is resolved and recorded during validation.

## 2. One Workspace Per Registration

One registration generation binds exactly one repository, one Workspace
version, and one immutable Resource revision. A registration ID identifies one
generation; the numeric GitHub repository ID identifies the logical registration
lineage. A repository that needs different `.ai` behavior publishes a new
Workspace version and creates a new registration generation.

Two repositories never share a Workspace. A Workspace never names more than one
repository.

## 3. Explicit Registration Lifecycle

```text
PENDING
  |
  v
VALIDATING --failure--> INVALID --new validation--> VALIDATING
  |
  v
ACTIVE --operator--> SUSPENDED --validation--> VALIDATING
  |
  +--new generation active--> DRAINING --> REMOVED
  |
  +--operator remove--> REMOVED
```

At most one generation per repository ID may be `ACTIVE`. The previous active
generation moves to `DRAINING` when a new generation activates.

* `PENDING`: manifest accepted, validation not started.
* `VALIDATING`: repository, bundle, validation contract, credentials, and state
  isolation are being checked.
* `INVALID`: validation failed. No ingress and no dispatch.
* `ACTIVE`: ingress and dispatch are enabled for the active generation.
* `DRAINING`: no new Event admission and no new WorkflowExecution. In-flight
  executions pinned to this generation may finish, acquire or renew leases, and
  be redispatched after approval or worker loss. Their publication follows the
  original policy.
* `SUSPENDED`: new events are rejected. In-flight publication defaults to
  disabled until the operator revalidates.
* `REMOVED`: no new work. Durable evidence is retained according to policy;
  branches and pull requests remain external Git/GitHub evidence.

Lifecycle transitions are durable runtime evidence. A registration cannot move
from `INVALID` to `ACTIVE` without a new validation pass.

## 4. Immutable Registration Generations

A registration generation is immutable. Changing any of the following creates a
new generation:

* Workspace name or version;
* Resource revision;
* validation Task or image digest;
* repository identity or default branch;
* GitHub App installation binding;
* state namespace assignment policy or artifact scope.

A new generation validates independently. The active generation switches only
after the new generation is `ACTIVE`; the previous active generation moves to
`DRAINING`. Validation permits an active predecessor in the same repository
lineage. Activation uses a compare-and-swap on the lineage and fails if the
lineage changed while validation was running. In-flight WorkflowExecutions
remain pinned to the generation and revision that created them and drain under
that generation. A generation is never mutated in place.

## 5. Registration Validation

Registration validation is deterministic and fail-closed. It checks:

1. The repository exists, the GitHub App is installed, and required
   least-privilege permissions are present.
2. `.ai/workspace.yaml` exists, matches the manifest repository identity, and
   declares the expected Workspace name and version.
3. The Resource checkout is detached, clean, and at the declared 40-character
   revision.
4. Every Resource validates against the AEP schemas and every reference uses an
   explicit immutable version.
5. The Event-to-Workflow resolution for `github.issue.created` produces a valid
   deterministic Task DAG.
6. Every Task, Agent, Prompt, Model, Tool, Policy, Evaluation, and
   KnowledgeBase reference resolves, and Agents cannot retrieve repository
   knowledge directly.
7. The RunValidation Task declares a digest-pinned validation image, bounded
   commands, required executables, timeout, CPU, and memory.
8. KnowledgeBase source paths exist at the pinned revision and remain inside the
   target repository.
9. Webhook secret and GitHub App credential references resolve without exposing
   values.
10. The state namespace is empty or belongs to the same registration and
    generation.
11. The repository lineage has at most one active generation. A validating
    successor may coexist with its active predecessor; the activation
    transaction enforces the single-active invariant atomically.
12. No Resource contains a secret, credential, or runtime-generated body.
13. A credential-free smoke load can resolve the Workspace, Event, Workflow,
    and Task DAG without invoking a model or external provider.

A validation failure records stable check identifiers and safe reasons. It does
not persist repository bodies or provider exception text.

## 6. Ingress Routing

Ingress is app-level and signature-first:

```text
GitHub App webhook
        |
        v
verify X-Hub-Signature-256 with the App webhook secret
        |
        v
parse minimal payload identity (repository.id, full_name, event, action)
        |
        v
compute stable dedup key:
github:repository:<repository.id>:delivery:<X-GitHub-Delivery>
        |
        v
existing Event? --yes--> return original Event and registration generation
        |
        no
        v
look up ACTIVE registration by repository.id
        |
        v
reject unregistered, suspended, or repository-mismatched delivery
        |
        v
commit Event + outbox row scoped to the selected registrationId
```

The payload is untrusted until the signature verifies. After verification, the
payload can only select an existing registration. It cannot create one, change
one, or redirect credentials.

* Unregistered repository: `404` or `422`, no Event persisted.
* Suspended registration: `503`, no Event persisted.
* Repository rename: the numeric GitHub repository ID remains the registration
  key; owner and name are display and routing attributes that must be
  revalidated after a transfer or rename.
* Duplicate delivery: the deduplication key is stable across registration
  generations: `github:repository:<repository-id>:delivery:<delivery-id>`. A
  redelivery returns the original Event and its original registration
  generation, even when a newer generation is active. It never re-routes the
  delivery to the active generation.
* Optional per-repository webhook secrets are a future isolation refinement. The
  first implementation uses one App webhook secret and registration-scoped
  routing.

GitHub App installation tokens are requested per registration and scoped to the
registered repository. A token from one registration cannot read or push
another repository.

## 7. Isolation Model

| Scope | Isolation rule |
| --- | --- |
| Resources | Each registration loads its own immutable `ResourceCollection` from its own checkout. Cross-registration Resource references are invalid. |
| Runtime objects | Every Event, WorkflowExecution, TaskExecution, ContextPackage, AgentInvocation, ModelInvocation, ToolInvocation, EvaluationResult, PolicyDecision, Approval, GeneratedArtifact, and ExecutionEvent carries `registrationId`. Queries and execution identity are registration-scoped; webhook deduplication is keyed by stable repository lineage plus delivery identity. |
| Runtime state | Each registration has a state namespace. A shared database is partitioned by registration; a local profile uses one namespace. |
| Artifacts | Metadata is registration-scoped. Object-store keys are prefixed by registration. Cross-registration deduplication is disabled by default. |
| Checkouts | Branch names, worktree roots, and checkout registry uniqueness are scoped by registration and repository. |
| Credentials | Webhook secret, GitHub App installation, source Git credential leases, and Model credentials resolve through registration-scoped references. Publication credentials are held only by the publication service. |
| Policy and approval | Policy decisions and approval requests are registration-scoped. An approver for one registration cannot authorize another. |
| Model quota | Each registration maps to a provider/credential quota scope. Shared provider limits are enforced by the distributed admission service from ADR-006. |
| Dispatch | Queue items and worker leases carry `registrationId`; a worker may hold capabilities for a declared registration set. |
| Inspection | The CLI filters by registration. Cross-registration inspection requires an explicit future operator role, not an implicit global read. |
| Emergency disable | The durable disable marker can be global or registration-scoped. Registration-scoped disable is preferred for multi-registration operation. |

Cross-registration artifact import, if ever added, is an explicit operator
operation that copies content into the destination registration's scope and
records new provenance. It is not an implicit read.

## 8. Provider And Resource Instances

Each registration constructs its own:

* Resource collection and Context Builder configuration;
* GitHub App provider and credential leases;
* repository source and execution checkout manager;
* validation configuration;
* policy and evaluation bindings;
* knowledge base snapshot and repository reader;
* dispatch eligibility and worker allowlist;
* artifact namespace;
* publication service binding and provider credential scope.

A worker may serve multiple registrations, but it executes one registration's
WorkflowExecution at a time. A worktree, context package, or artifact from one
registration is never mixed into another execution. A worker never receives
publication credentials.

## 9. Validation Portability

The target repository owns its validation contract. The registration requires:

* a digest-pinned validation image;
* required executable and version probes;
* bounded build and test commands;
* offline dependency availability when networking is disabled;
* explicit CPU, memory, and timeout limits;
* a workspace mount that does not escape the execution worktree.

AEP does not reuse its own validation image for another repository unless that
repository's toolchain and dependency set are compatible. The AEP image remains
the control-plane image; repository validation images are separate artifacts.

The target `.ai` bundle remains the source of truth for Task objectives, context
requirements, Agents, Prompts, Models, Tools, Policies, Evaluations, and
KnowledgeBase sources. AEP does not generate or silently repair a bundle during
registration.

## 10. Deployment Modes

| Mode | Registrations | Runtime state | Workers |
| --- | --- | --- | --- |
| Local | One | Durable JSON | One process |
| Single-registration self-hosting | One | Transactional database or durable JSON | One or more |
| Multi-registration control plane | Many | Shared transactional database | Shared worker pool with registration allowlists |

The single-registration mode remains the default self-hosting profile. The
multi-registration mode is a control-plane capability, not a requirement for
local development.

## 11. API And CLI

Registration operations are control-plane operations:

```text
aep repositories register --manifest <file>
aep repositories list
aep repositories show <registration-id>
aep repositories validate <registration-id>
aep repositories suspend <registration-id>
aep repositories resume <registration-id>
aep repositories remove <registration-id>
```

The manifest contains no secret values. Credentials are resolved from deployment
secret references at validation and runtime. A registration operation emits
durable lifecycle evidence and a validation report.

There is no webhook endpoint or Resource annotation that registers a repository.
A pull request cannot register its own repository.

## 12. Migration From The Single-Repository Deployment

1. Derive the first registration from the existing `.ai/workspace.yaml` and
   deployment identity.
2. Assign it generation 1 and a state namespace.
3. Backfill `registrationId` into runtime objects, events, execution identity
   keys, artifact metadata, and checkout records. Existing IDs remain valid.
4. Rebuild webhook deduplication entries under the stable repository-ID plus
   delivery-ID key. Existing accepted Events retain their original registration
   generation.
5. Replace `_BOUND_REPOSITORY`, inline Compose identity, and the local
   `../../:/workspace` assumption with a registration manifest and a pinned
   `AEP_RESOURCE_CHECKOUT`.
6. Report `registrationId`, generation, repository, Workspace, and resource
   revision in every health and readiness response.
7. Preserve the existing single-repository profile as the default until the
   multi-registration control plane is validated.

The existing repository remains a valid registration. Its behavior does not
change merely because the registration model is introduced.

---

# Schema And Contract Impact

* A new `RepositoryRegistration` runtime kind records identity, generation,
  Workspace reference, resource revision, provider binding, validation status,
  and state namespace. It is not a Resource.
* Runtime objects gain a `registrationId` scope. Existing single-repository
  evidence receives a default registration during migration.
* `Workspace` schema and semantics are unchanged. It remains the single
  declarative repository binding.
* Resource, Task, Agent, Prompt, Model, Tool, Policy, Evaluation, and
  KnowledgeBase schemas are unchanged. Their interpretation remains scoped to
  one registration's Resource collection.
* The registration manifest is an operator input with no runtime Resource
  identity and cannot be referenced by a Workflow.
* Health and readiness responses add registration ID, generation, repository,
  Workspace, and resource revision.
* The `aep` CLI adds registration-scoped list, show, validate, suspend, resume,
  and remove operations.

---

# Consequences

## Benefits

* Additional repositories become an explicit, auditable operation rather than a
  configuration side effect.
* Runtime state, artifacts, credentials, and approvals have a clear isolation
  boundary.
* A target repository can evolve its `.ai` bundle and validation contract
  without changing the AEP control-plane image.
* The existing single-repository deployment remains supported.
* Multi-repository operation composes with distributed worker execution.

## Trade-offs

* Every runtime object, query, queue row, and artifact needs a registration
  scope, increasing schema and migration work.
* Registration validation becomes a product surface with its own failures and
  operator documentation.
* Worker capability and registration allowlists add scheduling complexity.
* Cross-registration reporting and artifact migration require explicit future
  design.
* Renames, transfers, App reinstallations, and Workspace version changes require
  controlled generation transitions.

## Risks

* A missing registration predicate could leak artifacts or decisions across
  repositories.
* A shared worker could reuse a worktree, context, or credential across
  registrations if binding checks are weakened.
* Registration validation could accept a bundle whose validation image is
  unreachable or whose offline dependencies are incomplete.
* A single App installation or provider credential shared across registrations
  can create cross-repository rate-limit and failure coupling.

---

# Rejected Alternatives

## Register A Repository From A Webhook Payload

Rejected because an untrusted event would become an onboarding authority and
could redirect credentials, state, or execution to an unregistered repository.

## Use One Workspace For Multiple Repositories

Rejected because `Workspace` is the declarative repository binding. Sharing it
would make resource identity, revision binding, and policy provenance
ambiguous.

## Share One Runtime Namespace Across Registrations

Rejected because execution IDs, artifact provenance, approvals, and checkouts
would no longer have a single registration owner. Isolation must be structural,
not convention.

## Suspend The Predecessor Before Validating A Successor

Rejected because it creates an ingress gap that the atomic generation switch is
designed to avoid. A successor validates while the predecessor remains
`ACTIVE`; activation performs the single-active transition atomically.

## Include The Registration Generation In The Deduplication Key

Rejected because a delivery accepted under one generation would become a new
Event if redelivered after a successor activated. Deduplication uses stable
repository lineage plus delivery identity, and the Event retains its original
generation.

## Block All Dispatch While A Generation Drains

Rejected because an already accepted execution awaiting approval or recovering
from worker loss still needs a new owner. DRAINING blocks new Event admission
and new WorkflowExecutions, not redispatch of existing executions.

## Reuse The AEP Validation Image For Every Repository

Rejected because the image contains AEP's Python toolchain and offline
dependency set. A different repository must provide a compatible, digest-pinned
validation contract.

## Generate Or Copy A `.ai` Bundle During Registration

Rejected because the target repository owns its desired AI behavior. AEP
validates the bundle; it does not author or silently repair it.

## Hot-Edit The Active Resource Checkout

Rejected because the deployed version and its Resource revision must remain
immutable. Changes create a new generation and are reviewed as ordinary pull
requests.

## Allow Cross-Registration Workflow Execution

Rejected for this increment. One WorkflowExecution belongs to one registration
and one repository. Cross-repository orchestration would require a separate
tenancy and dependency model.

## Deduplicate Artifacts Globally Across Registrations

Rejected as a default because object existence can become a cross-repository
side channel and because artifact authority is registration-scoped. Explicit
import is the extension point.

---

# Open Questions

* Should validation profiles become a reusable declarative Resource instead of
  remaining embedded in the RunValidation Task?
* Should the first implementation use one App-level webhook secret or require a
  per-registration repository webhook secret?
* How should repository transfers and renames be reconciled when the numeric
  repository ID is stable but the owner/name binding changes?
* What is the retention and deletion policy for a removed registration's
  artifacts, approvals, and runtime evidence?
* Should model quota scopes be per registration, per provider credential, or
  both?
* Is cross-registration artifact import required for regression suites or
  shared evaluation evidence?
* Does multi-registration operation require a per-registration worker pool, or
  is a shared pool with allowlists sufficient?
