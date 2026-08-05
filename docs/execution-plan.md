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
| Completed | 20 |
| In Progress | 0 |
| Not Started | 17 |
| Blocked | 0 |
| Total | 37 |

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
command evidence. Docker validation now captures deterministic per-command
execution evidence behind that policy boundary, and build/test evaluation
converts terminal Docker evidence into separate immutable technical outcomes.
The GitHub Tool now performs
structured issue reads and policy-gated pull-request creation through a
provider-neutral client. Agent resolution now binds Task-assigned Agents to
explicit Prompt, Model, non-model Tool, and Task/Agent Policy versions in an
immutable ResolvedAgent. The end-to-end MVP workflow is not yet runnable.

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
| 23 | [AEP-017: Implement Context Builder](tasks/AEP-017-implement-context-builder.md) | Not Started |
| 24 | [AEP-026: Implement Patch Evaluation](tasks/AEP-026-implement-patch-evaluation.md) | Not Started |
| 25 | [AEP-027: Implement Build And Test Evaluation](tasks/AEP-027-implement-build-and-test-evaluation.md) | Completed |
| 26 | [AEP-010: Implement Workflow Scheduler](tasks/AEP-010-implement-workflow-scheduler.md) | Not Started |
| 27 | [AEP-013: Implement AgentInvocation Contract](tasks/AEP-013-implement-agentinvocation-contract.md) | Not Started |
| 28 | [AEP-028: Implement Publication Policy](tasks/AEP-028-implement-publication-policy.md) | Not Started |
| 29 | [AEP-029: Implement AnalyzeIssue Task Handler](tasks/AEP-029-implement-analyzeissue-task-handler.md) | Not Started |
| 30 | [AEP-030: Implement BuildImplementationPlan Task Handler](tasks/AEP-030-implement-buildimplementationplan-task-handler.md) | Not Started |
| 31 | [AEP-031: Implement GeneratePatch Task Handler](tasks/AEP-031-implement-generatepatch-task-handler.md) | Not Started |
| 32 | [AEP-032: Implement RunValidation Task Handler](tasks/AEP-032-implement-runvalidation-task-handler.md) | Not Started |
| 33 | [AEP-033: Implement EvaluateAcceptance Task Handler](tasks/AEP-033-implement-evaluateacceptance-task-handler.md) | Not Started |
| 34 | [AEP-034: Implement CreatePullRequest Task Handler](tasks/AEP-034-implement-createpullrequest-task-handler.md) | Not Started |
| 35 | [AEP-035: Compose MVP Services](tasks/AEP-035-compose-mvp-services.md) | Not Started |
| 36 | [AEP-036: Add Structured Logging And Tracing](tasks/AEP-036-add-structured-logging-and-tracing.md) | Not Started |
| 37 | [AEP-037: Build End-To-End MVP Harness](tasks/AEP-037-build-end-to-end-mvp-harness.md) | Not Started |

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
| AEP-010 | AEP-004, AEP-008, AEP-009, AEP-011 | Not Started |
| AEP-011 | AEP-002, AEP-004 | Completed |
| AEP-012 | AEP-003, AEP-011 | Completed |
| AEP-013 | AEP-012, AEP-014, AEP-017 | Not Started |
| AEP-014 | AEP-001, AEP-002 | Completed |
| AEP-015 | None | Completed |
| AEP-016 | AEP-015 | Completed |
| AEP-017 | AEP-003, AEP-004, AEP-016, AEP-018 | Not Started |
| AEP-018 | AEP-002, AEP-004 | Completed |
| AEP-019 | AEP-001, AEP-002 | Completed |
| AEP-020 | AEP-003, AEP-004, AEP-019 | Completed |
| AEP-021 | AEP-019, AEP-020 | Completed |
| AEP-022 | AEP-019, AEP-020 | Completed |
| AEP-023 | AEP-019, AEP-020 | Completed |
| AEP-024 | AEP-019, AEP-020 | Completed |
| AEP-025 | AEP-002 | Completed |
| AEP-026 | AEP-002, AEP-022 | Not Started |
| AEP-027 | AEP-002, AEP-023 | Completed |
| AEP-028 | AEP-004, AEP-020, AEP-025, AEP-026, AEP-027 | Not Started |
| AEP-029 | AEP-010, AEP-013, AEP-017, AEP-018, AEP-025 | Not Started |
| AEP-030 | AEP-029 | Not Started |
| AEP-031 | AEP-021, AEP-022, AEP-026, AEP-030 | Not Started |
| AEP-032 | AEP-023, AEP-027, AEP-031 | Not Started |
| AEP-033 | AEP-028, AEP-032 | Not Started |
| AEP-034 | AEP-024, AEP-028, AEP-033 | Not Started |
| AEP-035 | AEP-003, AEP-004 | Not Started |
| AEP-036 | AEP-004, AEP-008, AEP-011 | Not Started |
| AEP-037 | AEP-034, AEP-035, AEP-036 | Not Started |

---

# Phase Tracker

| Phase | Tasks | Status |
| ----- | ----- | ------ |
| Foundation Contracts | AEP-001, AEP-002, AEP-003, AEP-004 | Completed |
| Event And Control | AEP-005, AEP-006, AEP-007, AEP-008 | Completed |
| Workflow Runtime | AEP-009, AEP-010, AEP-011, AEP-012, AEP-013, AEP-014 | In Progress |
| Repository Context | AEP-015, AEP-016, AEP-017, AEP-018 | In Progress |
| Tool Platform | AEP-019, AEP-020, AEP-021, AEP-022, AEP-023, AEP-024 | In Progress |
| Evaluation And Policy | AEP-025, AEP-026, AEP-027, AEP-028 | In Progress |
| MVP Workflow | AEP-029, AEP-030, AEP-031, AEP-032, AEP-033, AEP-034 | Not Started |
| Deployment And Observability | AEP-035, AEP-036, AEP-037 | Not Started |

---

# Completion Rules

A task may be marked `Completed` only when all acceptance criteria in the task file are satisfied.

A phase may be marked `Completed` only when every task in that phase is completed.
