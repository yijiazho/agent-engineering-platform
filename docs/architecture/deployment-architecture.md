# Deployment Architecture

**Project:** AI Agent Engineering Platform (AEP)

**Document:** Deployment Architecture

**Status:** Draft

**Version:** 0.1

---

# 1. Overview

The AEP deployment architecture separates the platform into three independent planes:

* Control Plane
* Execution Plane
* Storage Plane

Each plane owns a distinct responsibility and may scale independently.

The deployment model is Kubernetes-native.

All platform components execute as containerized services.

---

# 2. Design Goals

The deployment architecture should provide:

* deterministic execution
* horizontal scalability
* fault isolation
* reproducibility
* observability
* incremental evolution

---

# 3. High-Level Deployment

```text
                      Kubernetes Cluster

┌──────────────────────────────────────────────────────────────┐

                 CONTROL PLANE

┌──────────────────────────────────────────────────────────┐
│                                                          │
│ Resource Registry                                        │
│ Workflow Controller                                      │
│ Event Controller                                         │
│ Knowledge Compiler                                       │
│ Policy Controller                                        │
│ Version Manager                                          │
│                                                          │
└──────────────────────────────────────────────────────────┘

──────────────────────────────────────────────────────────────

                 EXECUTION PLANE

┌──────────────────────────────────────────────────────────┐
│                                                          │
│ Workflow Runtime                                         │
│ Agent Resolver                                           │
│ Context Builder                                          │
│ Tool Runtime                                             │
│ Evaluation Engine                                        │
│                                                          │
└──────────────────────────────────────────────────────────┘

──────────────────────────────────────────────────────────────

                 STORAGE PLANE

┌──────────────────────────────────────────────────────────┐
│                                                          │
│ Git                                                      │
│ PostgreSQL                                               │
│ Object Storage                                           │
│ Graph Store                                              │
│ Redis                                                    │
│                                                          │
└──────────────────────────────────────────────────────────┘

└──────────────────────────────────────────────────────────────┘
```

---

# 4. Control Plane

The Control Plane manages Resources.

Responsibilities include:

* resource lifecycle
* reconciliation
* dependency resolution
* version discovery
* repository synchronization
* knowledge compilation

The Control Plane never executes workflows.

---

# 5. Execution Plane

The Execution Plane executes runtime objects.

Responsibilities include:

* workflow execution
* task scheduling
* context construction
* agent resolution
* tool execution
* evaluation

Execution services are stateless.

Execution state is externalized.

The Agent Resolver is a stateless execution-plane component, not a runtime service with durable state.

It loads Agent, Prompt, Model, Tool, and Policy resources and produces ResolvedAgent runtime objects for Workflow Runtime use.

---

# 6. Storage Plane

The Storage Plane persists durable platform state.

The platform distinguishes between:

## Systems of Record

Authoritative sources.

Examples:

* Git repositories
* GitHub

---

## Operational State

Mutable runtime state.

Examples:

* WorkflowExecution
* TaskExecution
* leases
* queues

---

## Derived State

Can always be regenerated.

Examples:

* Repository Knowledge Graph
* indexes
* caches

---

## Durable Artifacts

Produced by workflows.

Examples:

* plans
* reports
* patches
* evaluations

---

# 7. Storage Components

## Git

Source of truth for:

* source code
* workflows
* prompts
* policies
* resources

Nothing supersedes Git.

---

## PostgreSQL

Stores operational metadata.

Examples:

* execution state
* workflow history
* approvals
* runtime metadata

Does not store repository knowledge.

---

## Graph Store

Stores Repository Knowledge Graph versions.

The graph is a compiled artifact.

It is never manually edited.

---

## Object Storage

Stores immutable artifacts.

Examples:

* reports
* logs
* patches
* documentation

Objects are content-addressable.

---

## Redis

Provides:

* distributed locks
* execution queues
* caching
* rate limiting

Redis contains no authoritative data.

---

# 8. Kubernetes Topology

Each subsystem executes independently.

Example deployment:

```text
namespace

aep-system

    resource-controller

    workflow-runtime

    agent-resolver

    context-builder

    knowledge-compiler

    tool-runtime

    evaluation-engine

    observability
```

Each deployment scales independently.

---

# 9. Workflow Execution

Every WorkflowExecution creates runtime workers.

```text
Workflow

↓

WorkflowExecution

↓

TaskExecution

↓

AgentInvocation

↓

ToolInvocation
```

Runtime workers remain stateless.

---

# 10. Tool Execution

Tool execution occurs in isolated containers.

```text
Tool Runtime

↓

Sandbox

↓

Container

↓

External System
```

Containers are destroyed after execution.

No Tool persists state locally.

---

# 11. Knowledge Compilation

Repository synchronization pipeline:

```text
Git Push

↓

Repository Sync

↓

Knowledge Compiler

↓

Repository Knowledge Graph

↓

Publish Graph Version
```

Compilation occurs asynchronously.

Workflow execution always references published graph versions.

---

# 12. Scheduling

Workflow Runtime schedules Tasks.

Kubernetes schedules containers.

These responsibilities remain separate.

Workflow Runtime never manages Pods directly.

---

# 13. Networking

Internal communication occurs through platform APIs.

Examples:

Workflow Runtime

↓

Context Builder API

↓

Knowledge Query API

↓

Tool Runtime API

Services remain loosely coupled.

---

# 14. Scaling Strategy

Control Plane

Scale for repository count.

Execution Plane

Scale for concurrent workflow executions.

Knowledge Compiler

Scale for repository analysis throughput.

Tool Runtime

Scale for external workload.

Evaluation Engine

Scale for CI demand.

Each subsystem scales independently.

---

# 15. Fault Isolation

Failures remain isolated.

Examples:

Tool crash

↓

Restart Tool Runtime

Workflow continues

Knowledge compilation failure

↓

Repository marked stale

Workflow uses previous graph

Agent timeout

↓

Retry Task

Controller failure

↓

Execution unaffected

---

# 16. Observability

Every service emits:

* logs
* metrics
* traces
* events

Each runtime object receives a unique execution identifier.

Observability follows the complete execution path.

```text
WorkflowExecution

↓

TaskExecution

↓

ContextPackage

↓

AgentInvocation

↓

ToolInvocation

↓

EvaluationResult
```

Every runtime object shares the same trace identifier.

---

# 17. Metrics

Example platform metrics:

Control Plane

* reconciliation latency
* graph compilation duration
* controller queue depth

Execution Plane

* workflow duration
* task latency
* retry count
* agent latency

Tool Runtime

* execution time
* timeout rate
* resource consumption

Evaluation

* pass rate
* failure categories

Platform

* execution cost
* token usage
* artifact count

---

# 18. Logging

Every runtime object emits structured logs.

Logs always include:

* execution ID
* task ID when the event is within a TaskExecution
* trace ID
* workflow version
* task version
* agent version
* repository revision
* status, timing, and failure classification

Logs are immutable.

The provider-neutral field contract, lifecycle event names, service-boundary
propagation rules, and redaction requirements are defined in
[Structured Observability](observability.md). Secrets and artifact bodies are
never lifecycle-log fields; logs retain only safe identifiers and content
addresses for large evidence.

---

# 19. Disaster Recovery

Recovery priorities:

Git

↓

Resources

↓

Knowledge Graph

↓

Workflow History

↓

Artifacts

Derived state may be regenerated.

Operational state may be replayed.

Git remains the ultimate source of truth.

---

# 20. Future Evolution

The deployment architecture intentionally supports:

* multiple Kubernetes clusters
* remote execution workers
* GPU scheduling
* distributed Tool Runtimes
* cross-region execution
* organization-wide Knowledge Graphs
* multi-repository orchestration

No architectural changes should be required.

---

# 21. Design Principles

## Kubernetes Native

The platform should leverage Kubernetes rather than replacing it.

---

## Stateless Services

Business logic remains stateless.

Persistent state belongs to the Storage Plane.

---

## Git Is the Source of Truth

Resources originate from Git.

Derived systems may be rebuilt.

---

## Immutable Artifacts

Artifacts never change after publication.

---

## Independent Scaling

Every subsystem scales independently.

---

## Recoverable State

Everything except Git repositories and workflow history should be reproducible.

---

# 22. Summary

The AEP deployment architecture separates resource management, workflow execution, and persistence into independent Control, Execution, and Storage planes.

By treating Git as the authoritative source of truth, Repository Knowledge Graphs as compiled artifacts, runtime objects as ephemeral state, and execution services as stateless workers, the platform achieves reproducibility, fault isolation, and horizontal scalability while remaining aligned with Kubernetes' architectural principles.
