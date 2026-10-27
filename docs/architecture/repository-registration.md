# Repository Registration

**Project:** AI Agent Engineering Platform (AEP)

**Document:** Repository Registration

**Status:** Draft

**Version:** 0.1

---

# 1. Overview

This document specifies how AEP explicitly registers repositories and isolates
their execution, runtime state, artifacts, credentials, and publication.

It extends ADR-007. The current self-hosting deployment from ADR-004 remains the
first registration. Multi-registration operation is a control-plane capability,
not a requirement for local development.

The core rule is:

> A webhook may select an existing registration. It may never create one.

Repository registration is an operator action with durable evidence. The target
repository remains the source of its own `.ai` bundle and validation contract.

## 1.1 Goals

* Explicit, auditable registration of additional repositories.
* One repository, one Workspace version, one immutable Resource revision per
  registration generation.
* Registration-scoped runtime state, artifacts, credentials, approvals, and
  publication.
* Support for a single-registration deployment and a multi-registration control
  plane.
* No automatic onboarding, bundle generation, or cross-repository execution.

## 1.2 Non-Goals

* User accounts or multi-tenant SaaS authorization.
* Cross-organization artifact sharing.
* Cross-repository workflows in one WorkflowExecution.
* Repository selection by an Agent or by issue text.
* Mutating an active Resource checkout in place.

---

# 2. Current Boundary

The current implementation is repository-bound:

* `LocalServiceConfig` requires one repository and one Workspace identity, and
  initialization fails if `.ai/workspace.yaml` differs.
* `GitHubWebhookIngress` accepts only the configured repository and rejects a
  payload mismatch before normalization.
* `GitHubAppProvider` resolves the installation for the bound repository and
  scopes installation tokens to it.
* `DogfoodWorkflowRunner` requires `AEP_REPOSITORY_OWNER`, `AEP_REPOSITORY_NAME`,
  and `AEP_REPOSITORY_DEFAULT_BRANCH`.
* The dogfood deployment hardcodes the repository and Workspace identity.
* The self-hosting Compose profile hardcodes repository and Workspace values.
* The local Compose profile mounts this repository at `/workspace`.
* The RunValidation Task pins this repository's validation image and commands.
* The KnowledgeBase indexes this repository's documentation and source.

These constraints are intentional for the self-hosting MVP. Registration
generalizes them without allowing an event to choose a repository.

---

# 3. Concepts

## 3.1 Repository Registration

`RepositoryRegistration` is a control-plane runtime object. It records the
active binding between a repository and one Workspace generation.

It is not a declarative Resource. `Workspace` remains the declarative repository
binding. Registration records operational validation, activation, credentials by
reference, and generation state.

## 3.2 Workspace

`Workspace` declares:

* repository provider, owner, name, and default branch;
* the `.ai` discovery root;
* Workspace name and version.

One registration generation binds exactly one Workspace version. Two
registrations never share a Workspace. A registration ID identifies one
generation; the numeric repository ID identifies the logical registration
lineage.

## 3.3 Registration Generation

A generation is an immutable snapshot of:

* Workspace reference;
* Resource revision;
* validation Task and image digest;
* repository identity and default branch;
* GitHub App installation binding;
* state namespace and artifact scope.

Changing any of these creates a new generation. In-flight executions remain
pinned to their original generation.

## 3.4 Registration Manifest

The manifest is the operator input used to create or validate a generation. It
contains only references to secrets, never secret values.

Reference shape:

```yaml
apiVersion: aep.dev/v1alpha1
kind: RepositoryRegistrationManifest
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
resourceRoot: /srv/aep/registrations/widgets/resources
githubApp:
  appId: 12345
  installationId: 67890
ingress:
  webhookPath: /v1/webhooks/github
  secretRef: github-webhook-secret
validation:
  taskRef: {kind: Task, name: run-validation, version: 1.1.0}
stateNamespace: registration-widgets
```

The manifest is validated and then represented by a durable
`RepositoryRegistration` runtime object with status and evidence fields.

## 3.5 Registration Identity

A registration ID is deterministic:

```text
registration-<uuid5(canonical repository, Workspace ref, generation)>
```

The numeric GitHub repository ID is the stable repository key. Owner and name
are routing and display attributes that must be revalidated after a rename or
transfer.

---

# 4. Lifecycle

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

At most one generation per repository ID may be `ACTIVE`. When a new generation
activates, the previous active generation moves to `DRAINING`. A `DRAINING`
generation rejects new Events and new WorkflowExecutions, but it permits
redispatch and lease acquisition for already accepted executions. This includes
approval wake-up and recovery after a worker loss.

| State | Ingress | Dispatch | Publication | Notes |
| --- | --- | --- | --- | --- |
| PENDING | Rejected | None | None | Manifest accepted |
| VALIDATING | Rejected | None | None | Checks running |
| INVALID | Rejected | None | None | Stable failure evidence |
| ACTIVE | Accepted | Enabled | Per policy | Normal operation |
| DRAINING | Rejected | Existing executions only | Per original policy | Generation rollover; redispatch allowed |
| SUSPENDED | Rejected | No new work | Disabled by default | Drift, operator, or credential failure |
| REMOVED | Rejected | None | None | Evidence retained per policy |

## 4.1 Activation

Activation requires a complete validation report and an empty or matching state
namespace. Activation is idempotent for the same generation.

At most one generation is `ACTIVE` for a repository ID. Activating a new
generation moves the previous `ACTIVE` generation to `DRAINING`. The switch and
the previous generation's state change commit atomically. Validation may run
while an active predecessor exists; activation uses a compare-and-swap on the
lineage and fails if another generation activated or the lineage changed during
validation.

## 4.2 Suspension

Suspension is used for:

* Resource revision drift;
* invalid or missing `.ai` bundle;
* GitHub App uninstall or permission loss;
* secret rotation failure;
* repeated provider authorization failure;
* operator emergency action.

Suspension stops new events immediately. In-flight executions may finish local
work but publication is disabled by default. An operator can explicitly allow a
drain publication if policy permits it.

## 4.3 Resumption

Resumption reruns validation against the current generation. A new generation
is required if the Resource revision, Workspace, validation contract, or App
binding changed.

## 4.4 Removal

Removal stops ingress and dispatch. It does not delete branches, pull requests,
or merged code; those remain Git/GitHub evidence. Runtime evidence retention is
governed by policy. Artifact and evidence deletion is a separate, auditable
operation.

---

# 5. Validation Pipeline

Registration validation is deterministic and fail-closed. It runs before any
Event is accepted for the registration.

## 5.1 Repository And Provider

* Repository exists and is accessible.
* GitHub App is installed on the repository.
* Required permissions are present: metadata and issue read, contents and
  branch write, pull-request write.
* Default branch resolves to a 40-character revision.
* Webhook configuration targets the AEP ingress.
* Webhook secret and App private key references resolve to non-empty secrets
  without exposing them.

## 5.2 Resource Checkout

* Checkout is detached, clean, and at the declared revision.
* No untracked or modified files exist.
* `.ai/workspace.yaml` exists and matches repository identity and Workspace
  name/version.
* Resource discovery root contains only declarative Resources.

## 5.3 Resource Graph

* Every Resource validates against the AEP schemas.
* Every reference uses an explicit immutable version; `latest` is rejected.
* The Event-to-Workflow resolution for `github.issue.created` succeeds.
* The Task DAG is deterministic and acyclic.
* Every Task, Agent, Prompt, Model, Tool, Policy, Evaluation, and
  KnowledgeBase reference resolves.
* Agent Tool allowlists and policy references are valid.
* No Agent has direct repository-knowledge retrieval.
* No Resource contains credentials, tokens, webhook secrets, or artifact
  bodies.

## 5.4 Validation Contract

* RunValidation declares a digest-pinned image.
* Required executable probes have bounded version patterns.
* Build and test commands are bounded and non-interactive.
* The workspace mount resolves inside the execution worktree.
* CPU, memory, and timeout limits are positive and bounded.
* Offline dependency requirements are satisfiable when networking is disabled.

A registration may run an image-readiness probe during validation. It must not
run the full repository build during registration.

## 5.5 Knowledge

* Every KnowledgeBase source path exists at the pinned revision.
* Paths remain inside the repository.
* Documentation, ADR, runbook, and repository source types are supported.
* Required knowledge does not depend on an untracked checkout.

## 5.6 State And Isolation

* State namespace is empty or belongs to the same registration generation.
* Artifact prefix is empty or belongs to the same registration generation.
* The repository lineage has at most one `ACTIVE` generation. A `VALIDATING`
  successor may coexist with its `ACTIVE` predecessor; validation does not
  require the predecessor to be suspended or removed.
* Worker allowlists and quota scopes can represent the registration.

## 5.7 Smoke Resolution

A credential-free smoke load resolves:

* Workspace;
* Event;
* Workflow;
* Task DAG;
* required Evaluations and Policies.

It invokes no model and performs no external side effect. Failure produces
`INVALID` with stable check identifiers.

---

# 6. Ingress Routing

## 6.1 Endpoint

The first implementation uses one App-level webhook endpoint. The payload is
untrusted until the signature verifies.

```text
POST /v1/webhooks/github
  |
  +-- verify X-Hub-Signature-256 with App webhook secret
  |
  +-- require X-GitHub-Event: issues
  |
  +-- require action: opened
  |
  +-- parse repository.id
  |
  +-- compute stable dedup key:
  |   github:repository:<repository.id>:delivery:<delivery-id>
  |
  +-- existing Event? --yes--> return original Event and registration generation
  |
  +-- look up ACTIVE registration by repository.id
  |
  +-- reject unregistered, suspended, renamed, or mismatched repository
  |
  +-- commit Event + outbox scoped by registrationId
```

## 6.2 Responses

| Condition | Response | Persistence |
| --- | --- | --- |
| Authenticated accepted delivery | `202 accepted` | Event and outbox row |
| Authenticated duplicate | `200 duplicate` with original Event ID, registration ID, and generation | None |
| Invalid signature | `401` | None |
| Malformed or incomplete payload | `400` | None |
| Unsupported event or action | `422` | None |
| Repository not registered | `422 repository_not_registered` | None |
| Registration suspended | `503 registration_suspended` | None |
| Storage or dispatch failure | `503` | Transaction rolled back |

No repository body, issue body, signature header, or secret appears in
lifecycle evidence.

## 6.3 Deduplication

The deduplication key is stable across registration generations:

```text
github:repository:<repository-id>:delivery:<delivery-id>
```

Ingress checks this key before selecting the active generation. A redelivery
returns the original Event, registration ID, generation, and execution identity
even when a newer generation is active. It never re-routes the delivery to the
active generation or creates another execution.

## 6.4 Per-Registration Secrets

A future refinement may use separate repository webhooks with per-registration
secrets. The first implementation uses one App secret and registration-scoped
routing because the App already receives deliveries for every installed
repository.

---

# 7. Isolation Model

| Resource | Scope | Enforcement |
| --- | --- | --- |
| Workspace | One per registration generation | Resource loader and registration validation |
| Resource collection | Registration | In-memory collection per execution |
| Event | Registration | `registrationId` on the Event; stable repository-lineage plus delivery dedup key |
| WorkflowExecution | Registration | Creation transaction and provenance |
| TaskExecution and attempts | Registration via execution | Ownership check |
| ContextPackage | Registration and execution | Context identity validation |
| Agent, Prompt, Model, Tool, Policy, Evaluation | Registration resource collection | Explicit version resolution |
| ModelInvocation | Registration via execution | Provenance and quota scope |
| ToolInvocation | Registration via execution | Provenance and workspace binding |
| EvaluationResult | Registration via execution | Producer and revision check |
| PolicyDecision | Registration via execution | Policy refs from registration |
| Approval | Registration | Reviewer set and target binding |
| GeneratedArtifact | Registration | Catalog scope, object prefix, provenance |
| Checkout | Registration and repository | Registry uniqueness and branch prefix |
| Publication | Registration and execution | Publication service reconstructs the accepted patch, checks lease/fence, and uses idempotent provider identity |
| Credentials | Registration and service | Registration-scoped source credentials; publication credentials only in the publication service |
| Dispatch queue | Registration | Worker allowlist and claim predicate |
| Model quota | Registration and provider scope | Reservation keys |
| Inspection | Registration | CLI filter and authorization |
| Emergency disable | Global or registration | Durable marker |

## 7.1 Artifact Isolation

Artifact metadata is registration-scoped. Object-store keys are prefixed by
registration:

```text
<prefix>/<registrationId>/sha256/<first-two>/<remaining-hex>
```

Cross-registration deduplication is disabled by default. A future explicit
import operation may copy content into a destination registration and record
new provenance; it is not an implicit read.

## 7.2 Credential Isolation

* Webhook secret: App-level or registration-scoped reference.
* GitHub App installation: resolved per registration repository.
* Installation token: requested with the registered repository in the token
  scope.
* Git credential lease: source-fetch only and bound to the registration
  repository.
* Publication credentials: held only by the registration's publication service
  boundary and never issued to workers.
* Model credential: reference attached to the registration's Model scope.
* No credential is stored in Resources, runtime objects, logs, or artifact
  metadata.

## 7.3 Publication Isolation

Publication requires:

* a durable publication request from the execution owner;
* a valid execution lease and fence at the publication service;
* a serialized publication attempt for the execution;
* an allowing capability and publication policy decision from the
  registration's Resource collection;
* the accepted PATCH artifact and evidence, reconstructed and verified by the
  publication service in a clean worktree;
* a repository and branch matching the registration;
* a GitHub provider bound to the registration installation;
* no suspension or emergency disable.

The publication service owns the external mutation credentials and reconciles
`IN_FLIGHT` or `UNKNOWN` attempts before an execution lease transfers. An
approval from one registration cannot authorize another. A source-fetch
credential from one repository cannot push another.

---

# 8. Resource And Validation Portability

## 8.1 Target-Owned Bundle

The target repository owns:

* `.ai/workspace.yaml`;
* Event and Workflow definitions;
* Task definitions and context requirements;
* Agents, Prompts, Models, Tools, Policies, and Evaluations;
* KnowledgeBase sources;
* RunValidation configuration.

AEP validates the bundle. It does not generate it, copy it from another
repository, or repair it during registration.

## 8.2 Validation Image

A repository's validation contract must be portable and self-contained:

* digest-pinned image;
* required executables with version probes;
* bounded build and test commands;
* offline dependency availability when networking is disabled;
* workspace mount confined to the execution worktree;
* CPU, memory, and timeout limits.

The AEP control-plane image is not automatically a valid validation image for a
different repository. A repository that needs another language, runtime, or
dependency set must publish its own image or use a generic image with explicit
tooling probes.

## 8.3 Prompt And Knowledge Portability

Prompts may be generic or repository-specific. They remain declarative
Resources. KnowledgeBase paths are relative to the target repository and are
scanned at the registration revision. AEP does not assume its own `README.md`,
`docs/`, `src/`, or `tests/` layout.

## 8.4 Resource Versioning

A `.ai` change is a public contract change:

* increment the changed Resource version;
* update every reference in the same pull request;
* never use a floating version;
* create a new registration generation after review;
* validate the complete reference graph before activation.

In-flight executions remain on the old generation. The active generation
switches only after the new one is `ACTIVE`.

---

# 9. Deployment Topologies

## 9.1 Single Registration

This is the current self-hosting topology:

* one repository;
* one Workspace;
* one Resource revision;
* one state namespace;
* one or more workers all serving the same registration.

The existing deployment becomes registration generation 1. Behavior is
unchanged.

## 9.2 Multi-Registration Control Plane

```text
                     GitHub App webhook
                            |
                            v
                    Event Controller
                            |
                 registration router
                            |
        +-------------------+-------------------+
        |                   |                   |
        v                   v                   v
   Registration A      Registration B      Registration C
   Resource bundle     Resource bundle     Resource bundle
   State namespace     State namespace     State namespace
   Artifact prefix     Artifact prefix     Artifact prefix
        |                   |                   |
        +-------------------+-------------------+
                            |
                    Dispatch queue
                            |
                 +----------+----------+
                 |                     |
                 v                     v
            Worker pool A         Worker pool B
            (allowlist A)         (allowlist A, B)
```

Rules:

* One control plane may serve many registrations.
* Workers advertise the registrations they may execute.
* A worker executes one registration's WorkflowExecution at a time.
* Runtime state, artifacts, approvals, and publication remain registration
  scoped.
* No WorkflowExecution spans registrations.
* Fairness and quota policy may partition the queue by registration.

## 9.3 Local

One registration derived from the local `.ai/workspace.yaml`, durable JSON
state, filesystem artifacts, and one process. Useful for tests and development.

---

# 10. API And CLI

Proposed operator surface:

```text
aep repositories register --manifest <file>
aep repositories list
aep repositories show <registration-id>
aep repositories validate <registration-id>
aep repositories suspend <registration-id>
aep repositories resume <registration-id>
aep repositories remove <registration-id>
```

Behavior:

* `register` validates and creates a `PENDING` or `VALIDATING` registration.
* `validate` runs or reruns validation and records a new report.
* `suspend` and `resume` require an operator reason.
* `remove` requires terminal or explicitly abandoned executions.
* Every command emits durable lifecycle evidence and a machine-readable result.
* The CLI never prints secrets, issue bodies, or artifact bodies.
* Cross-registration inspection is denied unless a future operator role is
  explicitly configured.

No Resource annotation, issue label, or webhook payload can register a
repository.

---

# 11. Failure Modes

| Failure | Registration state | Ingress | Dispatch | Publication |
| --- | --- | --- | --- | --- |
| Missing `.ai/workspace.yaml` | INVALID | Rejected | None | None |
| Workspace identity mismatch | INVALID | Rejected | None | None |
| Floating Resource reference | INVALID | Rejected | None | None |
| Unresolved Task or Agent reference | INVALID | Rejected | None | None |
| Validation image unreachable | INVALID | Rejected | None | None |
| App not installed or permissions missing | INVALID | Rejected | None | None |
| Webhook secret missing | INVALID | Rejected | None | None |
| Resource revision drift | SUSPENDED | Rejected | No new | Disabled |
| App uninstalled at runtime | SUSPENDED | Rejected | No new | Disabled |
| State namespace mismatch | INVALID | Rejected | None | None |
| Successor validating while predecessor active | Predecessor ACTIVE; successor VALIDATING | Predecessor unchanged | Predecessor unchanged | Predecessor unchanged |
| Concurrent generation activation | Activation fails; successor remains VALIDATING | Per active generation | Per active generation | Per active generation |
| Duplicate delivery after generation rollover | Original Event's generation | Duplicate response | Original execution only | Original policy |
| Existing execution needs redispatch while DRAINING | DRAINING | Rejected for new work | Existing executions only | Per original policy |
| Duplicate registration | Idempotent | Per active state | Per active state | Per active state |
| Repository renamed | SUSPENDED until revalidated | Rejected | No new | Disabled |
| Removed registration | REMOVED | Rejected | None | None |

Failure evidence uses stable check identifiers and safe reasons. It never
contains repository source, issue text, provider response bodies, or secrets.

---

# 12. Migration

1. Derive the first registration from `.ai/workspace.yaml` and the current
   deployment environment.
2. Assign generation 1 and a state namespace.
3. Add `registrationId` to runtime objects, events, execution identity keys,
   artifact metadata, and checkout records.
4. Rebuild webhook deduplication entries under the stable repository-ID plus
   delivery-ID key. Existing accepted Events retain their original registration
   generation.
5. Keep existing runtime object IDs valid; backfill scope lazily or in a
   bounded migration.
6. Replace hardcoded repository and Workspace values with a registration
   manifest and a pinned `AEP_RESOURCE_CHECKOUT`.
7. Report registration ID, generation, repository, Workspace, and revision in
   health and readiness.
8. Keep the single-registration profile as the default until multi-registration
   is validated.
9. Add registration-scoped artifact prefixes only when the shared object-store
   profile is enabled.

Rollback:

* The previous single-registration profile remains deployable.
* Registration records are additive; removing the new ingress route does not
  invalidate historical runtime evidence.
* No historical execution is rewritten or reclassified.

---

# 13. Observability

Required fields on registration lifecycle and execution evidence:

* `registrationId`, generation, repository ID, owner/name;
* Workspace name/version and resource revision;
* validation check ID, result, and safe reason;
* ingress outcome and delivery ID;
* worker ID and execution lease;
* publication eligibility and disable reason.

Alerts:

* active registration with a dirty or drifted Resource checkout;
* registration validation failure;
* repeated ingress rejection for a registered repository;
* suspended registration with pending executions;
* credential or App authorization failure;
* artifact prefix or state namespace collision.

Logs remain redacted. Registration validation reports contain check results, not
repository or issue bodies.

---

# 14. Open Questions

* Should validation profiles become a separate declarative Resource?
* Should the first implementation use one App-level webhook secret or
  per-registration repository secrets?
* How should repository renames and transfers be reconciled with the stable
  numeric repository ID?
* Should a removed registration delete artifacts immediately, after a grace
  period, or according to legal hold?
* Should registrations share a worker pool or require dedicated pools for
  isolation?
* How is cross-registration artifact import authorized and recorded?
* Should registration validation run a repository-specific test image readiness
  probe, and how often should it be repeated?
* Does multi-registration operation require a per-registration quota for Model
  requests and tokens?

---

# 15. Related Documents

* [ADR-007: Repository Registration And Multi-Repository Isolation](../adr/ADR-007-repository-registration-and-multi-repository-isolation.md)
* [ADR-004: Self-Hosting Repository Integration](../adr/ADR-004-self-hosting-repository-integration.md)
* [ADR-006: Distributed Execution And Shared Artifacts](../adr/ADR-006-distributed-execution-and-shared-artifacts.md)
* [Deployment Architecture](deployment-architecture.md)
* [Distributed Execution](distributed-execution.md)
* [Workflow Runtime](workflow-runtime.md)
