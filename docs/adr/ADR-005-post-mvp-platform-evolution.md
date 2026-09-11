# ADR-005: Post-MVP Platform Evolution

**Status:** Proposed

**Authors:** Project Team

**Date:** 2026-09-10

---

# Context

ADR-003 deliberately constrains the MVP to one repository, one Workspace, one
GitHub issue event, and one six-Task issue-to-pull-request workflow. ADR-004
binds that vertical slice to this repository for controlled self-hosting. The
MVP establishes the core Resource, runtime-object, context, Agent, Tool,
evaluation, policy, artifact, and provenance contracts, but it does not yet
provide a complete operator experience for explaining historical outcomes,
reproducing or continuing executions, governing human intervention, comparing
AI configurations, or composing broader repository workflows.

The next platform increments must preserve the properties validated by the MVP:
immutable versioned configuration, revision-bound evidence, deterministic
control-plane decisions, explicit side-effect policy, durable provenance, and
no direct Agent access to repository knowledge. Adding features as disconnected
utilities would weaken those guarantees and create a second execution or
evaluation model outside AEP.

---

# Decision

AEP will evolve after the MVP through four coordinated capability tracks. Each
track reuses the existing Resource and runtime-object model and remains ordered
through explicit implementation-task dependencies.

## Operations And Governance

AEP-056 through AEP-059 and AEP-062 add deterministic inspection and
explanation, exact replay, workflow resume, artifact-bound approval gates, and
first-class execution budgets.

Inspection is foundational: later features must expose their state and decisive
evidence through the same safe CLI rather than requiring operators to inspect
raw storage. Replay creates a new lineage-linked execution, while resume
continues eligible work from durable checkpoints; neither may mutate historical
evidence or silently repeat external effects. Approval authorizes one immutable
artifact or action under a resolved Policy and never acts as a general policy
bypass. Budgets are enforced by the control plane, independently of Agent
instructions.

## Evaluation And Experimentation

AEP-060 and AEP-061 add versioned regression suites and configuration
experiments. They reuse WorkflowExecution, replay, GeneratedArtifact, and
EvaluationResult rather than introducing an independent benchmark runtime.
Case selection and aggregation are deterministic, provider variability remains
visible, and publication or other external effects are disabled by default.
Experiment results inform an explicit later promotion decision; they never
rewrite or automatically promote production Resources.

## Repository Intelligence

AEP-063 extends the file-oriented MVP knowledge layer with deterministic,
revision-bound symbol identities and bounded relationships. The graph remains
platform-owned evidence selected by Context Builder. Agents do not browse it
directly, and unsupported language constructs cannot become invented edges.

## Workflow And Integration Expansion

AEP-064 adds a governed pull-request review workflow with deterministic DAG
orchestration and bounded specialized reviewers. AEP-065 consumes configured
GitHub Actions/check results as revision-bound validation evidence. GitHub
Actions owns commodity CI execution; AEP continues to own AI orchestration,
evidence normalization, evaluation, governance, and publication decisions.

---

# Sequencing And Scope

The authoritative ordering and status remain in
[`docs/execution-plan.md`](../execution-plan.md). AEP-056 is the common
inspection foundation. Replay precedes regression suites; suites and replay
precede configuration experiments. Resume precedes workflows that need durable
continuation semantics. Features may share completed MVP dependencies without
being forced into one serial implementation chain when their contracts are
otherwise independent.

AEP-056 through AEP-065 are post-MVP work. Their addition does not change the
ADR-003 success criteria and does not block completion of the AEP-043 controlled
dogfood pilot unless a later, explicit decision changes that boundary.

AEP-066 is a remediation to the existing issue-to-PR vertical slice rather
than post-MVP scope expansion. It replaces model-authored full-file copying for
localized changes with evidence-bound edit operations, preserves rejected
proposals for safe inspection, and keeps unexpected unrelated deletion blocked.
Future AEP-059 approvals may authorize an explicitly planned risky rewrite by
exact artifact digest, but human review does not replace deterministic
pre-publication rejection for accidental or contradictory changes.

---

# Consequences

## Benefits

* Operators gain one deterministic, redacted explanation path across current
  and future runtime evidence.
* Replay, resume, approval, and budgets extend the existing control plane
  instead of bypassing it with ad hoc scripts.
* Prompt, model, context, and evaluation changes can be compared with durable
  case-level and aggregate evidence.
* Repository understanding and workflow breadth can grow without granting
  Agents direct discovery or publication authority.
* External CI remains reusable while AEP retains revision and policy authority.

## Trade-offs

* New runtime lineage, approval, budget, suite, experiment, symbol, review, and
  external-check contracts increase schema and migration work.
* Exact replay cannot promise identical nondeterministic provider output; it can
  promise identical reconstructable inputs and explicit output differences.
* Resume and approval add durable scheduler states and require stronger
  idempotency across restarts.
* Symbol extraction and external CI normalization require provider- or
  language-specific adapters behind provider-neutral contracts.

---

# Rejected Alternatives

## Fold All New Work Into The MVP

Rejected because it would move the success boundary after the vertical slice
was already defined and obscure whether the original architecture was proven.

## Build Standalone Debug, Benchmark, Or Approval Utilities

Rejected because disconnected tools would lose immutable Resource references,
runtime lineage, policy enforcement, and shared redaction guarantees.

## Let Agents Control Replay, Budgets, Approval, Or CI Publication

Rejected because these are control-plane and governance decisions. Agents may
produce typed proposals, but deterministic runtime components and resolved
Policies remain authoritative.

## Treat External CI Or A Knowledge Graph As A New Source Of Truth

Rejected because repository revision, AEP Resource resolution, workflow
history, and publication authority remain owned by AEP. External systems and
compiled knowledge provide bounded evidence only.
