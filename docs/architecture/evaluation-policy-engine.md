# Evaluation & Policy Engine

**Project:** AI Agent Engineering Platform (AEP)

**Document:** Evaluation & Policy Engine

**Status:** Draft

**Version:** 0.1

---

# 1. Overview

The Evaluation & Policy Engine is responsible for ensuring that workflow outputs are both **correct** and **permitted** before they affect external systems.

The subsystem consists of two independent but cooperating engines:

* Evaluation Engine
* Policy Engine

Evaluation determines whether an artifact satisfies technical expectations.

Policy determines whether an action is authorized.

AEP uses two named policy gates:

* Pre-Execution Capability Policy
* Publication Policy

This separation prevents governance concerns from becoming coupled to correctness checks.

---

# 2. Architecture

```text
                  Workflow

                     │

                     ▼

                Generated Artifact

                     │

                     ▼

             Evaluation Engine

             PASS / FAIL

                     │

              (if PASS)

                     ▼

               Publication Policy

          ALLOW / APPROVE / DENY

                     │

                     ▼

             External Action
```

---

# 3. Design Goals

The subsystem should provide:

* deterministic evaluation
* reusable validation
* policy-based governance
* human approval workflows
* complete auditability
* explainable decisions

---

# 4. Non-Goals

The subsystem does not:

* generate code
* perform planning
* execute workflows
* retrieve context
* invoke LLM reasoning

---

# Part I — Evaluation Engine

---

# 5. Purpose

Evaluation answers one question:

> **Did this Task produce the expected result?**

Evaluation is purely technical.

It contains no organizational policy.

---

# 6. Evaluation Model

Every Task may reference one or more Evaluations.

```text
Task

↓

Evaluation

↓

Result
```

Evaluations are immutable resources.

EvaluationResult is a runtime object.

---

# 7. Evaluation Types

Typical evaluation categories include:

## Build

* compilation
* dependency resolution

---

## Testing

* unit tests
* integration tests
* regression tests

---

## Static Analysis

* lint
* formatting
* security scanning

---

## Acceptance Criteria

Defined by the Task.

Examples:

* API added

* documentation updated

* migration generated

---

## Artifact Validation

Examples:

* markdown valid

* JSON schema

* OpenAPI validation

---

# 8. Evaluation Pipeline

```text
Artifact

↓

Compile

↓

Tests

↓

Static Analysis

↓

Acceptance Criteria

↓

Evaluation Result
```

Evaluations execute deterministically.

No LLM is required.

---

# 9. EvaluationResult

Runtime object.

Contains:

* evaluator
* timestamp
* outcome
* metrics
* logs
* evidence

EvaluationResults are immutable.

## 9.1 Build And Test Evaluation

Build and test evaluation consumes a terminal Docker `ToolInvocation`; it does
not execute commands. The caller supplies the canonical, immutable Docker Tool
reference, which must exactly match the invocation. Runtime `status` and Tool
`resultStatus` must both be present and form a consistent completed state. Two
configured expectations bind immutable Evaluation references to distinct
indexes in the invocation's ordered command list. The evaluator creates
separate build and test `EvaluationResult` records so a completed build remains
visible when testing fails or times out.

Each result records the selected command status, exit code, duration, logs
address, Tool result status, and deterministic evidence hash. Exit code zero
passes the selected evaluation. A nonzero exit fails it. When Docker stops
after a failed command, later configured commands are recorded as `NOT_RUN`;
when the deadline expires, the first command lacking completion evidence is
`TIMED_OUT` and later commands are `NOT_RUN`.

Missing invocation output, incomplete command records, and expectations that
select an unconfigured command produce a failed `EvaluationResult` with a
`CONFIGURATION` failure. Sequence corruption, including extra, reordered, or
trailing records after a nonzero exit, invalidates both results because neither
command can be trusted independently. Technical failures such as nonzero exits
and timeouts produce successfully completed evaluations with a `FAIL` outcome.
Both results are constructed and contract-validated before persistence. The
current `RuntimeObjectStore` has no atomic multi-create operation, so a backend
failure between the two valid creates remains a storage-level limitation. This
evaluator performs neither LLM reasoning nor a Publication Policy decision.

## 9.2 Patch Evaluation

For patch artifacts, deterministic evaluation first verifies the
GeneratedArtifact content address and immutable repository revision. It then
uses the repository-bound Git adapter's non-mutating applicability check and
compares every changed path, including rename sources, with normalized allowed
repository-relative roots. The EvaluationResult records the sorted changed-file
list, Git diagnostics and log reference, per-path boundary decisions, and
stable failure codes. A failed check is correctness evidence only; it never
authorizes a write, push, or publication action.

Task handlers that request persisted applicability evidence use the Git Tool's
atomic invocation boundary. The ToolInvocation identity is claimed before Git
execution, bound to the immutable request fingerprint, and cited by the
EvaluationResult. Matching concurrent or later retries reuse the terminal
result, while an identity conflict fails closed.

## 9.3 Acceptance Evidence Aggregation

The MVP `EvaluateAcceptance` Task is deterministic and non-cognitive. It
requires the complete ordered `AnalyzeIssue`, `BuildImplementationPlan`,
`GeneratePatch`, and `RunValidation` predecessor chain and the corresponding
`ISSUE_ANALYSIS`, `IMPLEMENTATION_PLAN`, `PATCH`, and `EVALUATION_REPORT`
artifacts. Each predecessor must retain exactly one expected artifact. Every
Evaluation declared by those explicit Task versions must resolve to a loaded
Evaluation Resource of the expected schema, patch, build, or test type and have
one attached, terminal EvaluationResult. The handler loads persisted records
rather than trusting caller-supplied summaries.

Every predecessor TaskExecution, EvaluationResult, and targeted AgentInvocation
or ToolInvocation is schema-validated. All
evidence must identify the same WorkflowExecution, trace, producer, exact Task
version, and repository revision. Artifact metadata must match the producer
attachment and its content-addressed body must still exist. Evaluation targets
must be correlated producer evidence: AgentInvocations for schema validation,
the PATCH for patch validation, and ToolInvocations for build and test.
Invocation contracts do not require a repository-revision field; their revision
binding is established through the validated owning TaskExecution and
EvaluationResult. If invocation provenance records a revision, it must match.
Missing,
failed, stale, pending, cross-execution, undeclared, substituted, or internally
inconsistent evidence fails closed.
The handler persists one immutable acceptance-summary EvaluationResult that
lists supporting artifact and evaluation identifiers, normalized summaries,
checks, and stable issue codes. This result evaluates technical correctness
only; Publication Policy remains the separate governance gate.

---

# 10. Evaluation Composition

Multiple Evaluations may be combined.

Example

```text
Compile

+

Tests

+

Security

+

Acceptance Criteria
```

The Workflow determines pass/fail semantics.

Examples:

* ALL_PASS
* ANY_PASS
* WEIGHTED
* CUSTOM

---

# Part II — Policy Engine

---

# 11. Purpose

Policy answers one question:

> **May this action occur?**

Policy never evaluates correctness.

---

# 12. Policy Model

Every privileged action passes through the Policy Engine.

```text
Action

↓

Policy Evaluation

↓

Decision
```

Possible decisions:

ALLOW

DENY

REQUIRE_APPROVAL

There are two policy evaluation stages.

## Pre-Execution Capability Policy

Pre-Execution Capability Policy answers:

> **May this capability run now?**

It is evaluated before Tool execution and other privileged capability use.

Examples:

filesystem.write

docker.run

secret.read

github.create_pr

## Publication Policy

Publication Policy answers:

> **May this evaluated output affect an external system?**

It is evaluated after technical evaluation passes and before publication, merge, deployment, or other external effects.

---

# 13. Policy Sources

Policies may originate from:

* Platform
* Workspace
* Workflow
* Task
* Agent
* Tool

Policies compose hierarchically.

The most restrictive rule always wins.

For Pre-Execution Capability Policy, rules identify one or more capabilities
and may include a JSON Schema condition evaluated against the capability,
actor, Resource scope, and execution context. Matching decisions compose as
`DENY` over `REQUIRE_APPROVAL` over `ALLOW`. If no applicable rule matches, the
decision is `DENY`.

Each evaluation persists a `PolicyDecision` containing every evaluated Policy
reference, the deterministically ordered matching rules, the winning rule and
reason, the actor, and the Resource scope. A `REQUIRE_APPROVAL` decision blocks
execution until an Approval is recorded; it is not equivalent to `ALLOW`.
Pre-execution rules must declare at least one capability. Pre-execution
decisions require the complete evidence set, while Publication Policy decisions
retain their gate-specific contract.

PolicyDecision persistence keys bind the caller-provided runtime ID to the
canonical task, trace, actor, capability, Resource scope, execution context,
and versioned Policy inputs. An idempotent retry returns the prior decision only
when those inputs are identical; reusing an ID for different authorization
inputs is rejected.

For Publication Policy, the candidate action supplies its complete publication
target, including the immutable repository revision. The evaluator requires an
explicit set of artifact and evaluation identifiers. At least one required
artifact must be a revision-bound PATCH, validation must have produced at least
one required `EvaluationResult`, and every required result must be terminal and
passing. Artifacts and results must share the candidate trace, WorkflowExecution,
and repository revision. Missing or mismatched evidence, a failed result, or an
earlier `DENY` fails closed before an allow rule can take effect. An earlier
unresolved `REQUIRE_APPROVAL` remains approval-required.

The evaluator owns one canonical evidence-summary contract.
`patchGenerated` means a required artifact is a PATCH; `validationRan` means at
least one required EvaluationResult was declared; `requiredArtifactsPresent`
and `requiredEvaluationsPresent` mean every declared identity was supplied;
`allRequiredEvaluationsPassed` means the required set is non-empty and every
result is terminal `SUCCEEDED`/`PASS`; `noPriorPolicyViolation` means no prior
decision is `DENY`; and `failures` is the ordered list of evidence-integrity
diagnostics. The first six fields are booleans. The self-hosting allow rule
requires all six to be true and `failures` to be empty. Missing, renamed, or
additional summary fields cannot match that versioned rule.

The caller's evidence mappings are not trusted by themselves. Every required
runtime object is validated against its kind-specific schema and must exactly
match the immutable object resolved from the runtime store. Malformed,
unpersisted, or substituted evidence therefore cannot manufacture an allow
decision.

Publication rules use the same deterministic scope, Resource name, and version
ordering and the same restrictive effect precedence as capability rules, but do
not declare capabilities. Their optional JSON Schema conditions evaluate the
candidate action, evidence summary, prior policy state, and Resource scope. The
persisted decision records the exact artifact, evaluation, and prior-decision
identifiers, publication target, evidence summary, matched and winning rules,
and explanation. The evaluator does not push Git state or invoke a publication
provider.

The self-hosting immutable graph is `publication-evidence:1.1.0` referenced by
`create-pull-request:1.2.0`, which is referenced by `issue-to-pr:1.3.0`.
Publication Policy is evaluated before the handler creates a local commit. An
allow is followed by distinct `git.push` and `github.create_pr` capability
decisions immediately before their corresponding external mutations.

---

# 14. Capability Evaluation

Policies evaluate capabilities.

Examples:

filesystem.write

github.create_pr

github.merge

docker.run

kubernetes.deploy

secret.read

---

# 15. Human Approval

Some capabilities require approval.

Example:

```text
Deploy

↓

Approval Required

↓

Human Approves

↓

Continue
```

Approval becomes a runtime event.

---

# 16. Approval Objects

Approval is a runtime object.

Contains:

* requester
* approver
* timestamp
* reason
* policy
* outcome

Approvals become part of execution history.

---

# 17. Governance

Every privileged action produces an audit record.

Recorded information includes:

* workflow
* task
* agent
* tool
* policy
* approval
* execution

Nothing bypasses the Policy Engine.

---

# 18. Observability

Every evaluation records:

* duration
* metrics
* evidence
* failure reason

Every policy decision records:

* evaluated rule
* decision
* approval status
* actor

---

# 19. Failure Handling

Evaluation failure

↓

Workflow-defined behavior

Examples:

Retry

Fail Workflow

Manual Review

Policy denial

↓

Execution stops immediately

Policy violations are never retried.

---

# 20. Future Extensions

Future capabilities include:

* security policy language
* organization policies
* cost policies
* execution quotas
* model governance
* policy simulation
* signed approvals
* risk scoring

These extend existing engines without changing their interfaces.

---

# 21. Design Principles

## Correctness Before Governance

Outputs are evaluated before permissions are considered.

---

## Separation of Concerns

Evaluation measures quality.

Policy governs authority.

---

## Deterministic Validation

Evaluations should avoid LLMs whenever possible.

---

## Immutable Evidence

Every decision records supporting evidence.

---

## Explainable Governance

Every denied action identifies the policy responsible.

---

# 22. Summary

The Evaluation & Policy Engine establishes trust within AEP by separating technical correctness from organizational governance.

The Evaluation Engine verifies that generated artifacts satisfy deterministic acceptance criteria, while the Policy Engine ensures that privileged actions comply with platform rules and approval requirements.

Together, these engines enable autonomous workflows to remain safe, auditable, and reproducible without coupling governance to AI reasoning.
