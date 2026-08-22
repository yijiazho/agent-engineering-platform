# Execution Plan

**Project:** AI Agent Engineering Platform (AEP)

**Status:** Active

**Purpose:** Track implementation order and task status for the MVP vertical slice.

---

# Status Values

* Not Started
* In Progress
* Completed
* Blocked

New tasks start as `Not Started`. Update a task to `In Progress`, `Completed`,
or `Blocked` as implementation state changes.

---

# Progress Summary

| Status | Count |
| ------ | ----: |
| Completed | 43 |
| In Progress | 5 |
| Not Started | 0 |
| Blocked | 0 |
| Total | 48 |

The completed work establishes schemas, resource loading, runtime persistence,
GitHub issue event normalization and deduplication, WorkflowExecution creation,
TaskExecution lifecycle,
provider-neutral model and Tool boundaries, schema evaluation, the initial
repository-knowledge scanner and query API, and immutable content-addressed
GeneratedArtifact storage. Normalized events resolve deterministically to
explicitly versioned Workflow references, and Workflow Task DAGs resolve into
validated deterministic execution plans with parallel-ready groups.
Pre-execution capability policy composes versioned rules across all supported
scopes and persists explainable decisions before Tool execution. The
workspace-confined Filesystem Tool now supports policy-authorized reads and
writes with persisted invocation evidence. The repository-bound Git Tool now
provides branch, status, diff, and authorized push operations with redacted
command evidence and read-only patch applicability checks. Deterministic patch
evaluation binds generated patches to an immutable revision and allowed paths.
Docker validation now captures deterministic per-command
execution evidence behind that policy boundary, and build/test evaluation
converts terminal Docker evidence into separate immutable technical outcomes.
Publication Policy now validates revision-bound patch and evaluation evidence,
composes versioned rules conservatively, and persists explainable final-gate
decisions without performing external effects.
The GitHub Tool now performs
structured issue reads and policy-gated pull-request creation through a
provider-neutral client. Agent resolution now binds Task-assigned Agents to
explicit Prompt, Model, non-model Tool, and Task/Agent Policy versions in an
immutable ResolvedAgent. Deterministic ContextPackage construction now
assembles provenance-complete Resources, events, repository knowledge,
policies, and prior artifacts within an explicit token budget. The Workflow
scheduler now reconciles deterministic parallel-ready waves,
persists numbered TaskExecution attempts, blocks dependents until prerequisite
success, and emits append-only lifecycle events through a provider-neutral Task
executor boundary. The seven MVP control/execution service boundaries
now run in a local composition with explicit ports, health
checks, one repository and Workspace, and externalized local persistence. 
Shared structured observability now propagates one trace
through runtime and service boundaries and emits redacted lifecycle logs with
immutable execution, Resource, revision, status, timing, and failure evidence.
AgentInvocation coordination now binds immutable ResolvedAgent and
ContextPackage inputs, assembles bounded model requests, persists provider
evidence, and validates structured output without exposing repository retrieval.
The live OpenAI Model adapter now selects the provider from the immutable Model
Resource, enforces its model, parameters, output-token, timeout, structured
output, and retry bounds, injects runtime-only credentials, and records safe
provider identity, usage, latency, finish, and failure evidence.
The AnalyzeIssue Task handler now composes those boundaries to create issue
context, invoke the versioned Issue Analyzer, run deterministic schema
Evaluation, publish an `ISSUE_ANALYSIS` GeneratedArtifact, and attach the
resulting evidence to its TaskExecution.
The BuildImplementationPlan Task handler now requires that successful upstream
evidence, supplies it with revision-bound repository knowledge through a
deterministic ContextPackage, invokes the versioned Planner, evaluates required
plan sections, and publishes an immutable `IMPLEMENTATION_PLAN` artifact.
The RunValidation Task handler now consumes the evaluated patch, resolves a
versioned digest-pinned Docker validation contract, persists retry-safe Tool
evidence, creates separate build and test EvaluationResults, and publishes an
immutable validation report for both successful and failed validation runs.
The EvaluateAcceptance Task handler now walks the successful predecessor chain,
loads versioned Evaluation requirements and attached artifacts, rejects missing
or inconsistent execution and revision evidence, and persists a deterministic
acceptance-summary EvaluationResult without model or policy invocation.
The CreatePullRequest Task handler now applies the final Publication Policy and
separate Git/GitHub capability gates, persists retry-safe external operation
evidence, materializes and binds the accepted patch commit and published head,
and publishes the resulting pull-request description and URL. Authenticated
GitHub webhook ingress now verifies HMAC-SHA256 over raw deliveries, enforces
the bound repository and supported issue action, deduplicates through shared
durable storage, and atomically commits one provider-neutral reconciliation
outbox request with redacted trace evidence.  The
deterministic end-to-end MVP harness now loads a fixture `.ai/` bundle,
normalizes and deduplicates an issue event, executes the six-Task DAG through
the scheduler, and verifies runtime, artifact, evaluation, policy, fake-model,
and fake-GitHub evidence for allowed and blocked publication paths. ADR-004 adds the work required
to register this repository as the first live, repository-bound integration:
authenticated webhook ingress, isolated execution checkout provisioning, a
complete self-hosting Resource bundle, live GitHub and Model providers, and a
pinned dogfood deployment. These registration tasks extend rather than replace
the completed CreatePullRequest Task handler and unfinished deterministic
end-to-end harness. Trusted execution-checkout provisioning now resolves the
configured repository through an ephemeral credential boundary, verifies the
recorded immutable revision, atomically assigns an isolated worktree and
deterministic branch, and retains evidence for bounded cleanup and
interrupted-worker recovery. Durable compare-and-swap ownership, renewable
fencing tokens, and repository-cache-scoped claims protect those mutations
across workers and restarts. Lease heartbeats cover the full duration of active
mutations, while one checkout-bound orchestration seam constructs repository
context, Tool boundaries, and publication inputs without independent path or
revision drift.

---

# Topological Order

The following order respects task dependencies and keeps contract work ahead of dependent implementations.

| Order | Task | Status |
| -----: | ---- | ------ |
| 1 | [AEP-001: Define Resource Schemas](tasks/AEP-001-define-resource-schemas.md) | Completed |
| 2 | [AEP-002: Define Runtime Object Schemas](tasks/AEP-002-define-runtime-object-schemas.md) | Completed |
| 3 | [AEP-015: Build MVP Repository Scanner](tasks/AEP-015-build-mvp-repository-scanner.md) | Completed |
| 4 | [AEP-003: Build Resource Loader](tasks/AEP-003-build-resource-loader.md) | Completed |
| 5 | [AEP-004: Build Runtime Object Store Interface](tasks/AEP-004-build-runtime-object-store-interface.md) | Completed |
| 6 | [AEP-005: Normalize GitHub Issue Created Event](tasks/AEP-005-normalize-github-issue-created-event.md) | Completed |
| 7 | [AEP-014: Implement ModelInvocation Adapter Interface](tasks/AEP-014-implement-modelinvocation-adapter-interface.md) | Completed |
| 8 | [AEP-019: Define Tool Runtime Contract](tasks/AEP-019-define-tool-runtime-contract.md) | Completed |
| 9 | [AEP-025: Implement Schema Evaluation](tasks/AEP-025-implement-schema-evaluation.md) | Completed |
| 10 | [AEP-011: Implement TaskExecution Lifecycle](tasks/AEP-011-implement-taskexecution-lifecycle.md) | Completed |
| 11 | [AEP-016: Build Repository Knowledge Query API](tasks/AEP-016-build-repository-knowledge-query-api.md) | Completed |
| 12 | [AEP-018: Implement GeneratedArtifact Store](tasks/AEP-018-implement-generatedartifact-store.md) | Completed |
| 13 | [AEP-006: Implement Event Deduplication](tasks/AEP-006-implement-event-deduplication.md) | Completed |
| 14 | [AEP-007: Resolve Workflow For Event](tasks/AEP-007-resolve-workflow-for-event.md) | Completed |
| 15 | [AEP-009: Build Task DAG Resolver](tasks/AEP-009-build-task-dag-resolver.md) | Completed |
| 16 | [AEP-020: Implement Pre-Execution Capability Policy](tasks/AEP-020-implement-pre-execution-capability-policy.md) | Completed |
| 17 | [AEP-008: Create WorkflowExecution](tasks/AEP-008-create-workflowexecution.md) | Completed |
| 18 | [AEP-012: Implement Agent Resolver](tasks/AEP-012-implement-agent-resolver.md) | Completed |
| 19 | [AEP-021: Implement Filesystem Tool](tasks/AEP-021-implement-filesystem-tool.md) | Completed |
| 20 | [AEP-022: Implement Git Tool](tasks/AEP-022-implement-git-tool.md) | Completed |
| 21 | [AEP-023: Implement Docker Validation Tool](tasks/AEP-023-implement-docker-validation-tool.md) | Completed |
| 22 | [AEP-024: Implement GitHub Tool](tasks/AEP-024-implement-github-tool.md) | Completed |
| 23 | [AEP-017: Implement Context Builder](tasks/AEP-017-implement-context-builder.md) | Completed |
| 24 | [AEP-026: Implement Patch Evaluation](tasks/AEP-026-implement-patch-evaluation.md) | Completed |
| 25 | [AEP-027: Implement Build And Test Evaluation](tasks/AEP-027-implement-build-and-test-evaluation.md) | Completed |
| 26 | [AEP-010: Implement Workflow Scheduler](tasks/AEP-010-implement-workflow-scheduler.md) | Completed |
| 27 | [AEP-013: Implement AgentInvocation Contract](tasks/AEP-013-implement-agentinvocation-contract.md) | Completed |
| 28 | [AEP-028: Implement Publication Policy](tasks/AEP-028-implement-publication-policy.md) | Completed |
| 29 | [AEP-029: Implement AnalyzeIssue Task Handler](tasks/AEP-029-implement-analyzeissue-task-handler.md) | Completed |
| 30 | [AEP-030: Implement BuildImplementationPlan Task Handler](tasks/AEP-030-implement-buildimplementationplan-task-handler.md) | Completed |
| 31 | [AEP-031: Implement GeneratePatch Task Handler](tasks/AEP-031-implement-generatepatch-task-handler.md) | Completed |
| 32 | [AEP-032: Implement RunValidation Task Handler](tasks/AEP-032-implement-runvalidation-task-handler.md) | Completed |
| 33 | [AEP-033: Implement EvaluateAcceptance Task Handler](tasks/AEP-033-implement-evaluateacceptance-task-handler.md) | Completed |
| 34 | [AEP-034: Implement CreatePullRequest Task Handler](tasks/AEP-034-implement-createpullrequest-task-handler.md) | Completed |
| 35 | [AEP-035: Compose MVP Services](tasks/AEP-035-compose-mvp-services.md) | Completed |
| 36 | [AEP-036: Add Structured Logging And Tracing](tasks/AEP-036-add-structured-logging-and-tracing.md) | Completed |
| 37 | [AEP-037: Build End-To-End MVP Harness](tasks/AEP-037-build-end-to-end-mvp-harness.md) | Completed |
| 38 | [AEP-038: Implement Authenticated GitHub Webhook Ingress](tasks/AEP-038-implement-authenticated-github-webhook-ingress.md) | Completed |
| 39 | [AEP-039: Provision Revision-Bound Execution Checkouts](tasks/AEP-039-provision-revision-bound-execution-checkouts.md) | Completed |
| 40 | [AEP-040: Create Self-Hosting Resource Bundle](tasks/AEP-040-create-self-hosting-resource-bundle.md) | Completed |
| 41 | [AEP-041: Implement GitHub App Provider Integration](tasks/AEP-041-implement-github-app-provider-integration.md) | Completed |
| 42 | [AEP-042: Implement Live Model Provider Adapter](tasks/AEP-042-implement-live-model-provider-adapter.md) | Completed |
| 43 | [AEP-044: Stabilize Self-Hosting Dogfood Startup](tasks/AEP-044-stabilize-self-hosting-dogfood-startup.md) | Completed |
| 44 | [AEP-045: Optimize Context Token Efficiency](tasks/AEP-045-optimize-context-token-efficiency.md) | In Progress |
| 45 | [AEP-046: Coordinate Model Rate Limits](tasks/AEP-046-coordinate-model-rate-limits.md) | In Progress |
| 46 | [AEP-047: Build Hermetic Validation Image](tasks/AEP-047-build-hermetic-validation-image.md) | In Progress |
| 47 | [AEP-048: Align Publication Policy Evidence Contract](tasks/AEP-048-align-publication-policy-evidence-contract.md) | In Progress |
| 48 | [AEP-043: Deploy Self-Hosting Dogfood Pilot](tasks/AEP-043-deploy-self-hosting-dogfood-pilot.md) | In Progress |

---

# Dependency Tracker

| Task | Depends On | Status |
| ---- | ---------- | ------ |
| AEP-001 | None | Completed |
| AEP-002 | None | Completed |
| AEP-003 | AEP-001 | Completed |
| AEP-004 | AEP-002 | Completed |
| AEP-005 | AEP-001 | Completed |
| AEP-006 | AEP-004, AEP-005 | Completed |
| AEP-007 | AEP-003, AEP-005 | Completed |
| AEP-008 | AEP-004, AEP-006, AEP-007 | Completed |
| AEP-009 | AEP-003 | Completed |
| AEP-010 | AEP-004, AEP-008, AEP-009, AEP-011 | Completed |
| AEP-011 | AEP-002, AEP-004 | Completed |
| AEP-012 | AEP-003, AEP-011 | Completed |
| AEP-013 | AEP-012, AEP-014, AEP-017 | Completed |
| AEP-014 | AEP-001, AEP-002 | Completed |
| AEP-015 | None | Completed |
| AEP-016 | AEP-015 | Completed |
| AEP-017 | AEP-003, AEP-004, AEP-016, AEP-018 | Completed |
| AEP-018 | AEP-002, AEP-004 | Completed |
| AEP-019 | AEP-001, AEP-002 | Completed |
| AEP-020 | AEP-003, AEP-004, AEP-019 | Completed |
| AEP-021 | AEP-019, AEP-020 | Completed |
| AEP-022 | AEP-019, AEP-020 | Completed |
| AEP-023 | AEP-019, AEP-020 | Completed |
| AEP-024 | AEP-019, AEP-020 | Completed |
| AEP-025 | AEP-002 | Completed |
| AEP-026 | AEP-002, AEP-022 | Completed |
| AEP-027 | AEP-002, AEP-023 | Completed |
| AEP-028 | AEP-004, AEP-020, AEP-025, AEP-026, AEP-027 | Completed |
| AEP-029 | AEP-010, AEP-013, AEP-017, AEP-018, AEP-025 | Completed |
| AEP-030 | AEP-029 | Completed |
| AEP-031 | AEP-021, AEP-022, AEP-026, AEP-030 | Completed |
| AEP-032 | AEP-023, AEP-027, AEP-031 | Completed |
| AEP-033 | AEP-028, AEP-032 | Completed |
| AEP-034 | AEP-024, AEP-028, AEP-033 | Completed |
| AEP-035 | AEP-003, AEP-004 | Completed |
| AEP-036 | AEP-004, AEP-008, AEP-011 | Completed |
| AEP-037 | AEP-034, AEP-035, AEP-036 | Completed |
| AEP-038 | AEP-005, AEP-006, AEP-007, AEP-008, AEP-035, AEP-036 | Completed |
| AEP-039 | AEP-015, AEP-021, AEP-022, AEP-035, AEP-036 | Completed |
| AEP-040 | AEP-003, AEP-017, AEP-020, AEP-025, AEP-029, AEP-030, AEP-031, AEP-032, AEP-033, AEP-034 | Completed |
| AEP-041 | AEP-022, AEP-024, AEP-036, AEP-038 | Completed |
| AEP-042 | AEP-013, AEP-014, AEP-036 | Completed |
| AEP-043 | AEP-031, AEP-032, AEP-033, AEP-034, AEP-035, AEP-036, AEP-037, AEP-038, AEP-039, AEP-040, AEP-041, AEP-042, AEP-045, AEP-046, AEP-047, AEP-048 | In Progress |
| AEP-044 | AEP-035, AEP-038, AEP-039, AEP-040, AEP-041, AEP-042 | Completed |
| AEP-045 | AEP-016, AEP-017, AEP-029, AEP-040, AEP-042 | In Progress |
| AEP-046 | AEP-010, AEP-036, AEP-040, AEP-042, AEP-045 | In Progress |
| AEP-047 | AEP-023, AEP-027, AEP-032, AEP-039, AEP-040 | In Progress |
| AEP-048 | AEP-028, AEP-034, AEP-037, AEP-040, AEP-047 | In Progress |

---

# Phase Tracker

| Phase | Tasks | Status |
| ----- | ----- | ------ |
| Foundation Contracts | AEP-001, AEP-002, AEP-003, AEP-004 | Completed |
| Event And Control | AEP-005, AEP-006, AEP-007, AEP-008 | Completed |
| Workflow Runtime | AEP-009, AEP-010, AEP-011, AEP-012, AEP-013, AEP-014 | Completed |
| Repository Context | AEP-015, AEP-016, AEP-017, AEP-018, AEP-045 | In Progress |
| Tool Platform | AEP-019, AEP-020, AEP-021, AEP-022, AEP-023, AEP-024 | Completed |
| Evaluation And Policy | AEP-025, AEP-026, AEP-027, AEP-028, AEP-048 | In Progress |
| MVP Workflow | AEP-029, AEP-030, AEP-031, AEP-032, AEP-033, AEP-034 | Completed |
| Deployment And Observability | AEP-035, AEP-036, AEP-037 | Completed |
| Repository Integration And Dogfooding | AEP-038, AEP-039, AEP-040, AEP-041, AEP-042, AEP-043, AEP-044, AEP-046, AEP-047 | In Progress |

---

# Completion Rules

A task may be marked `Completed` only when all acceptance criteria in the task file are satisfied.

A phase may be marked `Completed` only when every task in that phase is completed.
