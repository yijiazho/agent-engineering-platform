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
