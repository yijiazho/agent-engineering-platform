# AEP-020: Implement Pre-Execution Capability Policy

**Status:** Not Started

## Context

Every ToolInvocation and privileged platform action must be authorized before execution. Pre-Execution Capability Policy answers whether a named capability may run now, independently of whether a generated artifact is technically correct or publishable.

Rules may be attached at Platform, Workspace, Workflow, Task, Agent, and Tool scopes. They compose conservatively, persist an explainable `PolicyDecision`, and may allow, deny, or pause for human approval; the Tool Runtime must never bypass the result.

## Deliverable

Implement the pre-execution policy evaluator that:

* accepts capability, actor, Resource scope, execution context, and applicable versioned policies;
* composes policy scopes deterministically with the most restrictive decision winning;
* returns and persists `ALLOW`, `DENY`, or `REQUIRE_APPROVAL` with rule and reason;
* exposes a reusable authorization boundary for all Tool adapters; and
* tests conflicting rules and the MVP capabilities `filesystem.write`, `docker.run`, `git.push`, and `github.create_pr`.

## Dependencies

* AEP-003
* AEP-004
* AEP-019

## Acceptance Criteria

* Evaluator supports ALLOW, DENY, and REQUIRE_APPROVAL.
* Evaluator composes Platform, Workspace, Workflow, Task, Agent, and Tool policies.
* Most restrictive rule wins.
* PolicyDecision is persisted.
* Tests cover filesystem.write, docker.run, git.push, and github.create_pr.
