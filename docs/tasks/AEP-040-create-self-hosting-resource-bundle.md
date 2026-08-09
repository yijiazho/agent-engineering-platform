# AEP-040: Create Self-Hosting Resource Bundle

**Status:** Completed

## Context

This repository already declares its Workspace and the
`github.issue.created` Event under `.ai/`, but it does not declare the
`issue-to-pr` Workflow or the Resources required to execute its six Tasks. AEP
cannot dogfood its own control loop until its desired AI behavior is complete,
versioned, reviewable, and loadable from this repository.

The bundle must configure behavior without embedding credentials or runtime
state. Agents remain stateless, model providers remain Model Resources rather
than Tools, repository knowledge reaches Agents only through ContextPackages,
and publication must fail closed unless deterministic evidence passes.

## Deliverable

Create and validate this repository's complete `.ai/` self-hosting bundle,
including:

* the versioned `issue-to-pr` Workflow and `AnalyzeIssue`,
  `BuildImplementationPlan`, `GeneratePatch`, `RunValidation`,
  `EvaluateAcceptance`, and `CreatePullRequest` Tasks;
* Issue Analyzer, Planner, Code Generator, and PR Writer Agents with explicit
  Prompt, Model, Tool, Policy, Evaluation, and KnowledgeBase references;
* repository-appropriate context requirements, allowed paths, Docker image,
  build/test commands, token budgets, timeouts, retry limits, and structured
  output contracts; and
* deterministic fixtures and tests proving Resource loading, reference
  resolution, Workflow matching, DAG ordering, and policy coverage.

## Dependencies

* AEP-003
* AEP-017
* AEP-020
* AEP-025
* AEP-029
* AEP-030
* AEP-031
* AEP-032
* AEP-033
* AEP-034

## Acceptance Criteria

* Loading this repository's `.ai/` directory discovers one bound Workspace,
  the issue-created Event, the complete six-Task Workflow, four Agents, and all
  referenced Resources with explicit immutable versions.
* The Workflow resolver selects exactly `issue-to-pr` for a normalized issue
  event, and the DAG resolver returns the six Tasks in the required dependency
  order without missing or floating references.
* AnalyzeIssue and BuildImplementationPlan receive repository and issue
  context; GeneratePatch receives the plan and scoped write capabilities;
  validation and acceptance remain deterministic; and only CreatePullRequest
  receives push and PR-creation capabilities.
* The repository KnowledgeBase includes maintained README, architecture, ADR,
  task, schema, source, and test knowledge needed by the Context Builder, with
  no direct repository-retrieval Tool granted to an Agent.
* Validation uses this repository's locked, documented test command in a
  digest-pinned, network-disabled Docker configuration with bounded resources
  and timeout.
* Policies deny undeclared capabilities and prevent merge, deployment, secret
  access, out-of-scope writes, push, or PR creation unless the exact required
  evidence and approvals are present.
* No Resource contains credentials, provider tokens, webhook secrets, runtime
  object identifiers, or GeneratedArtifact content.
* README documents how maintainers version and review the self-hosting bundle,
  and tests fail when a reference, policy gate, or required Resource is removed.
