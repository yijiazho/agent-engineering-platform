# AEP-043: Deploy Self-Hosting Dogfood Pilot

**Status:** In Progress

## Context

Completing the generic MVP harness does not itself register this repository
with a live platform. Dogfooding requires an operational, repository-bound
deployment that receives authenticated events for
`yijiazho/agent-engineering-platform`, runs a pinned AEP release against an
isolated checkout, and opens an unmerged pull request back to this repository.

The running instance must never rewrite its own installed package, image, or
control-plane checkout. AEP version N may propose version N+1 in a separate
worktree, but promotion remains an ordinary human-reviewed GitHub pull request.

## Deliverable

Create the self-hosting deployment and operator runbook that:

* deploys a pinned AEP build with durable runtime state, authenticated webhook
  ingress, repository synchronization, execution-checkout storage, Docker Tool
  isolation, and live GitHub and Model provider adapters;
* installs and binds a least-privilege GitHub App to this repository and routes
  its issue-created deliveries to the Event Controller;
* loads this repository's immutable `.ai/` bundle and wires the complete
  issue-to-PR Workflow across the seven service responsibilities;
* provides readiness, backup, recovery, credential rotation, upgrade,
  rollback, execution inspection, and emergency-disable procedures; and
* performs a controlled live pilot from a labeled GitHub issue to exactly one
  unmerged pull request with complete runtime and publication evidence.

## Dependencies

* AEP-031
* AEP-032
* AEP-033
* AEP-034
* AEP-035
* AEP-036
* AEP-037
* AEP-038
* AEP-039
* AEP-040
* AEP-041
* AEP-042

## Acceptance Criteria

* The deployed service reports one repository and one Workspace identity that
  exactly match `.ai/workspace.yaml`, and startup fails on identity or Resource
  drift.
* Only correctly signed issue-created deliveries from this repository enter
  reconciliation; replaying a delivery does not duplicate the
  WorkflowExecution, branch, or pull request.
* Runtime code and Resources come from a pinned release/read-only revision,
  while every generated change occurs in an isolated writable worktree at the
  recorded base revision.
* A controlled issue in this repository executes all six Tasks and produces
  exactly one unmerged pull request containing the issue link, implementation
  plan, changed-file summary, and build/test evidence.
* Runtime history contains correlated Event, WorkflowExecution,
  TaskExecutions, ContextPackages, Agent/Model/Tool invocations,
  GeneratedArtifacts, EvaluationResults, PolicyDecisions, and final PR URL with
  no secrets or artifact bodies in logs.
* A failed validation, policy denial, repository mismatch, stale revision, or
  emergency disable prevents push and PR creation; the operator can explain
  the failure from persisted evidence.
* The deployment cannot merge pull requests, deploy generated code, modify its
  running image, or use credentials outside this repository.
* The runbook documents installation, GitHub configuration, smoke validation,
  monitoring, backup/recovery, secret rotation, upgrades, rollback, shutdown,
  and removal of the repository registration.

## Implementation Status

The digest-pinned, repository-bound Compose profile, startup drift checks,
service-scoped secret mounts, durable storage layout, emergency publication
guard, deployment contract tests, and complete operator runbook are implemented.

Completion remains gated on the controlled external pilot. An authorized
operator must install the GitHub App, supply live GitHub and Model credentials,
enable authenticated ingress, run one labeled issue through all six Tasks, and
record exactly one open, unmerged pull request plus the complete correlated
runtime and publication evidence described by the runbook. Repository tests
cannot satisfy that live acceptance criterion.
