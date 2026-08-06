# Workflow Runtime

**Project:** AI Agent Engineering Platform (AEP)

**Document:** Workflow Runtime

**Status:** Draft

**Version:** 0.2

---

# 1. Overview

The Workflow Runtime is responsible for executing declarative workflows.

Its responsibilities are intentionally limited to:

* scheduling
* dependency resolution
* state management
* retries
* failure handling
* parallel execution
* observability

The runtime does **not** perform reasoning.

The runtime never interacts directly with LLMs.

Instead, it executes Tasks, resolves Agents into immutable invocation inputs, and lets model-backed Agents perform the cognitive work defined by those Tasks.

---

# 2. Runtime Architecture

```text
                 Event
                   │
         Workflow Controller
                   │
                   ▼
            Execution Plan
                   │
                   ▼
              DAG Scheduler
                   │
        ┌──────────┴──────────┐
        ▼                     ▼
   Ready Queue          Waiting Queue
        │
        ▼
    Task Executor
        │
        ▼
   Context Builder
        │
        ▼
  Context Package
        │
        ▼
        Agent
        │
        ▼
   Tool Platform
        │
        ▼
 Execution Result
        │
        ▼
Execution State Store
```

The runtime owns execution.

The Context Builder owns information retrieval.

Agents own reasoning.

---

# 3. Task DAG Resolution

Before scheduling begins, the Workflow Runtime resolves the Workflow's
explicitly versioned Task references into an immutable execution plan.
Resolution:

* verifies that every Workflow node resolves to a loaded Task Resource;
* rejects duplicate Task identities, dependencies outside the Workflow graph,
  and dependency cycles;
* preserves Workflow declaration order as the deterministic tie-breaker;
* records each Task's dependencies and dependents; and
* partitions the graph into ordered parallel-ready groups.

The resolver validates graph structure only. It does not create
TaskExecutions, execute handlers, or allow an Agent or model to choose
execution order. The scheduler consumes the resolved plan in a separate
lifecycle step.

## Scheduler Reconciliation

The MVP scheduler advances a resolved plan one parallel-ready wave per
reconciliation. It reads all persisted TaskExecution attempts for the
WorkflowExecution, then:

* selects Tasks whose declared dependencies have a successful attempt;
* creates one idempotent attempt for every ready Task before execution begins;
* invokes Task handlers through a provider-neutral executor boundary;
* retries only recoverable failures while the configured attempt limit allows;
* binds dependents to the exact successful prerequisite attempt identifiers;
  and
* records queued, started, succeeded, and failed ExecutionEvents.

Task and event identifiers are deterministic. The atomic TaskExecution status
transition from `PENDING` to `RUNNING` keeps concurrent reconcilers from
invoking the same attempt twice. The MVP never reclaims a `RUNNING` attempt from
elapsed time alone: `startedAt` is evidence, not an ownership lease, and the
executor may still be live. Genuinely abandoned attempts require explicit
external resolution until atomic owner-token leases and renewal are available.
Reconciliation repairs idempotent lifecycle events and WorkflowExecution
membership before advancing the DAG. A later reconciliation observes newly
successful prerequisites and schedules the next wave; configuration,
evaluation, policy, permanent, and exhausted recoverable failures leave
dependents blocked.

The scheduler loads the authoritative WorkflowExecution from runtime storage,
verifies immutable caller evidence against it, and validates all
WorkflowExecution, TaskExecution, and ExecutionEvent records against their
runtime schemas before scheduler persistence. Newly created TaskExecutions
carry the WorkflowExecution's repository revision and, when present, knowledge
graph version in provenance so downstream Context Builder validation remains
revision-bound.

---

# 4. Core Runtime Objects

The runtime manipulates immutable Resources together with runtime-only objects.

## Resources

Resources are declarative and versioned.

Examples include:

* Workflow
* Task
* Agent
* Prompt
* Tool
* KnowledgeBase
* Policy
* Evaluation

Resources are stored in Git and never modified during execution.

---

## Runtime Objects

Runtime objects are created during execution and discarded after completion.

Examples include:

* WorkflowExecution
* TaskExecution
* ContextPackage
* ResolvedAgent
* AgentInvocation
* ToolInvocation
* EvaluationResult

Runtime objects are **not** Resources.

They represent transient execution state.

---

# 5. Agent Resolution

The Workflow Runtime does not host an Agent Runtime service.

For each Task, it asks the Agent Resolver to load the referenced Agent, Prompt, Model, Tools, and Policies.

The Agent Resolver produces a ResolvedAgent runtime object.

ResolvedAgent contains the immutable model configuration, prompt reference, tool allowlist, and policy constraints required for a single AgentInvocation.

Resolution verifies that the Task assigns the requested Agent, loads every
reference at an explicit immutable version, and rejects wrong-kind or missing
Resources. Task and Agent policy references are retained in declaration order.
An unconditional pre-execution policy denial makes a configured Tool invalid at
resolution time; conditional and approval rules remain attached for evaluation
with the execution-time policy input.

The Agent Resolver owns no durable state.

---

# 6. ContextPackage

ContextPackage is the contract between the Workflow Runtime and the Context Builder.

The runtime never assembles context itself.

Instead, it requests a ContextPackage for each Task.

```text
Task
  │
  ▼
Context Builder
  │
  ▼
ContextPackage
  │
  ▼
Agent
```

This abstraction allows the Context Builder to evolve independently from the runtime.

---

# 7. ContextPackage Structure

A ContextPackage represents the minimum sufficient information required to execute a Task.

Typical contents include:

## Task Information

* Task identifier
* Task objective
* Input parameters
* Expected outputs

---

## Repository Context

* Relevant source files
* AST fragments
* Symbol definitions
* Dependency graph fragments
* File metadata

---

## Knowledge

* Architecture documents
* ADRs
* Coding standards
* Project documentation
* Runbooks

---

## Related Artifacts

* Previous plans
* Design documents
* Generated patches
* Pull requests
* Reviews

---

## External Context

* GitHub Issue
* Pull Request
* Commit metadata
* Acceptance criteria

---

## Policies

Applicable governance rules.

Examples:

* security restrictions
* repository permissions
* approval requirements

---

## Provenance

Every context element records its origin.

Examples:

```text
architecture.md

↓

section 4.2

↓

commit

abc123
```

or

```text
UserService.cs

↓

Class

UserService

↓

Method

Authenticate()
```

Every generated output can therefore explain *why* a particular piece of information was included.

---

## Budget Metadata

ContextPackage also contains execution metadata such as:

* estimated token count
* actual token count
* truncation information
* compression strategy
* retrieval score

This enables deterministic prompt assembly and observability.

---

# 8. Context Resolution Lifecycle

For every Task execution:

```text
Resolve Task

↓

Resolve Agent

↓

Agent Resolver

??

ResolvedAgent

↓

Request ContextPackage

↓

Validate ContextPackage

↓

Invoke Agent

↓

Execute Tools

↓

Produce Artifacts
```

The runtime only validates that a ContextPackage satisfies the Task's contract.

It never determines how the package was constructed.

The MVP `AnalyzeIssue` handler implements this composition through the existing
provider-neutral boundaries. For one already-running TaskExecution it builds
and records the ContextPackage, persists the ResolvedAgent and AgentInvocation,
runs the declared schema Evaluation, and publishes an immutable
`ISSUE_ANALYSIS` GeneratedArtifact only after Evaluation passes. It attaches
all produced identifiers to the TaskExecution; the deterministic scheduler
remains responsible for the terminal success or classified failure transition.

The AgentInvocation coordinator then combines that immutable package with the
resolved Prompt, Model configuration, and output schema. It persists the
AgentInvocation and each ModelInvocation, including token, latency, cost, and
provider metadata when supplied. A provider call may succeed while structured
output validation fails; in that case the ModelInvocation remains successful
with `schemaValidation: FAILED`, while the AgentInvocation terminates with an
`EVALUATION` failure. No repository-knowledge query interface is exposed at
this boundary. An atomic paired-identity claim binds the AgentInvocation to its
ModelInvocation before runtime records are created, preventing concurrent
reconciliation from repeating the external model call or substituting a child
invocation identity.

---

# 9. Context Validation

Before invoking an Agent, the runtime validates the ContextPackage.

Validation includes:

* required context present
* schema validation
* provenance completeness
* policy compliance
* size constraints

Invalid ContextPackages fail fast before model invocation.

---

# 10. Context Immutability

A ContextPackage is immutable.

Once delivered to an Agent, it cannot be modified.

If additional information is required, the Agent cannot mutate its ContextPackage directly.

Instead, it must trigger a follow-up Task that requests a new ContextPackage from the Context Builder.

Agents may invoke only the non-knowledge Tools allowed by their ResolvedAgent and policies.

---

# 11. Context Reproducibility

Every WorkflowExecution records:

* ContextPackage identifier
* source revisions
* KnowledgeBase versions
* GeneratedArtifact identifiers
* Prompt version
* Agent version
* ResolvedAgent identifier

Given the same repository revision and Resource versions, the Context Builder should produce an equivalent ContextPackage.

This makes executions reproducible and debuggable.

---

# 12. Runtime Responsibilities

The Workflow Runtime owns:

* DAG scheduling
* Task lifecycle
* dependency management
* retries
* parallel execution
* execution state
* ContextPackage validation
* Agent resolution requests
* AgentInvocation lifecycle
* Tool orchestration
* GeneratedArtifact publication
* observability

The runtime explicitly does **not** own:

* repository analysis
* retrieval logic
* ranking
* prompt engineering
* knowledge indexing
* context optimization

Those responsibilities belong to the Context Builder subsystem.

---

# 13. Design Principles

## Separation of Concerns

The runtime orchestrates execution.

The Context Builder assembles knowledge.

Agents perform reasoning.

Tools perform actions.

Each subsystem has a single responsibility.

---

## Immutable Runtime Inputs

Every Task executes against an immutable set of:

* Resources
* ContextPackage
* Input parameters

This guarantees deterministic execution.

---

## Explicit Contracts

ContextPackage forms the only interface between orchestration and reasoning.

Neither the Workflow Runtime nor the Agent needs to understand the internal implementation of the Context Builder.

---

# 14. Summary

Introducing ContextPackage as a runtime object establishes a clear architectural boundary between execution and intelligence.

The Workflow Runtime becomes responsible only for scheduling and lifecycle management, while the Context Builder independently produces immutable, provenance-rich ContextPackages that encapsulate all information required for cognitive work.

This separation improves modularity, reproducibility, observability, and future extensibility while allowing retrieval and context optimization techniques to evolve without impacting workflow execution.
