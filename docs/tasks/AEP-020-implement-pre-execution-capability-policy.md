# AEP-020: Implement Pre-Execution Capability Policy

**Status:** Not Started

## Context

Every privileged ToolInvocation must pass Pre-Execution Capability Policy before execution.

## Deliverable

Implement policy evaluation for tool capabilities.

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
