# Architecture Overview

**Project:** AI Agent Engineering Platform (AEP)

**Status:** Draft

**Version:** 0.1

---

# 1. Introduction

The AI Agent Engineering Platform (AEP) is a declarative workflow platform for software engineering automation.

Unlike traditional AI assistants that rely on conversational context and ad-hoc orchestration, AEP executes deterministic workflows triggered by engineering events while leveraging structured repository knowledge and reusable AI components.

The platform is designed around a simple principle:

> **AI should perform cognitive work. The platform should perform orchestration.**

LLMs generate plans, code, documentation, reviews, and analyses.

The platform determines **when**, **why**, **how**, and **under what constraints** those AI capabilities are invoked.

---

# 2. Vision

The long-term vision is to build an AI-native control plane inspired by Kubernetes.

Repositories declare AI resources.

Controllers reconcile repository events into workflow executions.

AI behavior becomes reproducible, version-controlled, and observable.

Rather than becoming another AI agent framework, AEP becomes the operating layer responsible for coordinating AI systems.

---

# 3. Design Principles

## 3.1 Declarative First

Everything is represented as declarative resources.

Examples include:

* Workspaces
* Workflows
* Tasks
* Agents
* Prompts
* Models
* Tools
* Policies
* Knowledge Bases
* Evaluations

Application code does not contain AI behavior.

Instead, AI behavior is described through version-controlled specifications.

---

## 3.2 Deterministic Orchestration

Workflow execution is deterministic.

The runtime decides:

* execution order
* dependencies
* retries
* scheduling
* failure recovery

LLMs never determine workflow execution paths.

They produce structured outputs consumed by deterministic controllers.

---

## 3.3 Context Before Generation

LLMs should never operate directly against repositories.

Instead, every task begins with deterministic context construction.

Repository knowledge

↓

Policies

↓

Documentation

↓

Artifacts

↓

Acceptance criteria

↓

Prompt Assembly

↓

LLM

The quality of generated output depends primarily on context quality rather than prompt complexity.

---

## 3.4 Knowledge Over Conversation

Conversation history is not considered persistent memory.

Instead, the platform maintains structured knowledge derived from authoritative sources.

Examples:

* source code
* documentation
* architecture
* Git history
* generated artifacts

Knowledge can always be reconstructed.

Conversation cannot.

---

## 3.5 Reproducibility

Every execution must be reproducible.

Every artifact must record:

* workflow version
* agent version
* prompt version
* model version
* repository revision
* knowledge snapshot

Given identical inputs, the same workflow should produce comparable outputs.

---

## 3.6 Human Governance

AI systems may automate engineering tasks.

Only humans approve changes affecting production environments.

Policies determine which actions require approval.

---

# 4. High-Level Architecture

```text
                      GitHub

                         │

                    Webhooks

                         │

               Event Controller

                         │

              Workflow Scheduler

                         │

               Workflow Runtime

                         │

        ┌────────────────────────────────────┐
        │                                    │
        ▼                                    ▼

 Context Builder                     Workflow Graph

        │                                    │

        ▼                                    ▼

 Repository Knowledge              Cognitive Agents

        │                                    │

        └──────────────┬─────────────────────┘
                       ▼

                 Tool Platform

                       │

              Docker / Kubernetes

                       │

                  Generated Artifacts

                       │

                 Evaluation Engine

                       │

                 Policy Controller

                       │

               Human Approval (Optional)

                       │

                 External Systems
```

---

# 5. System Layers

The platform is organized into six logical layers.

## Layer 1 — Event Layer

Responsible for observing external systems.

Examples:

* GitHub
* Schedulers
* Future integrations

Outputs normalized platform events.

---

## Layer 2 — Control Layer

Responsible for orchestration.

Includes:

* Workflow Scheduler
* Controllers
* Runtime
* State Management

This layer contains no AI logic.

---

## Layer 3 — Intelligence Layer

Responsible for cognitive tasks.

Examples:

* Planning
* Coding
* Reviewing
* Documentation
* Classification

This layer contains reusable AI Agents.

---

## Layer 4 — Context Layer

Responsible for assembling task context.

Consumes:

* Repository Knowledge
* Policies
* Artifacts
* Knowledge Bases
* Acceptance Criteria

Produces optimized context packages.

---

## Layer 5 — Execution Layer

Responsible for interacting with external systems.

Examples:

* GitHub
* Docker
* Filesystem
* Search
* Python
* Kubernetes

All non-model external capabilities are exposed as Tools.

---

## Layer 6 — Governance Layer

Responsible for trust.

Includes:

* Evaluation
* Policies
* Permissions
* Human Approval
* Audit

---

# 6. Resource Model

Every platform capability is represented as a Resource.

Resources are immutable and versioned.

The initial resource model includes:

* Workspace
* Workflow
* Task
* Agent
* Prompt
* Tool
* Model
* KnowledgeBase
* Policy
* Evaluation
* Event

Controllers reconcile resources into execution.

Resources themselves contain no execution logic.

Every declarative Resource should have a corresponding controller responsible for validation and reconciliation.

Declarative Resources
        │
        ▼
Controllers
        │
        ▼
        │
        ▼
Runtime Objects

---

# 7. Workflow Lifecycle

Every workflow follows the same lifecycle.

```text
GitHub Event

↓

Normalize Event

↓

Resolve Workflow

??

Resolve Task Graph

↓

Build Context

↓

Execute DAG

↓

Generate Artifacts

↓

Evaluate

↓

Publication Policy Check

↓

Human Approval (optional)

↓

Publish Results
```

For the self-hosting pull-request path, Publication Policy consumes the
canonical fields `patchGenerated`, `validationRan`,
`requiredArtifactsPresent`, `requiredEvaluationsPresent`,
`allRequiredEvaluationsPassed`, `noPriorPolicyViolation`, and `failures`.
Version `publication-evidence:1.1.0` allows only `github.create_pr` when all six
booleans are true and the failure list is empty; unmatched input remains
`DENY`. Git push and GitHub PR creation remain separate capability gates.

This lifecycle is identical regardless of workflow purpose.

Only resource definitions change.

---

# 8. Context Construction

The Context Builder is the central intelligence service of the platform.

Its responsibility is not to retrieve documents.

Its responsibility is to construct the minimum sufficient context required for a task.

Possible inputs include:

* repository graph
* source files
* dependency graph
* documentation
* architecture decisions
* coding standards
* previous artifacts
* GitHub issues
* pull requests
* acceptance criteria
* workflow history

Every context element records its origin.

This allows every AI decision to be explained.

Agents never retrieve repository knowledge directly.

If an Agent needs additional repository or knowledge context, the workflow must create a follow-up Task that receives a new ContextPackage from the Context Builder.

---

# 9. Repository Intelligence

Repositories are continuously analyzed into structured knowledge.

Primary representations include:

* Abstract Syntax Trees (AST)
* Symbol tables
* Dependency graphs
* File relationships
* Documentation indices
* Git history

Embeddings are supplemental rather than foundational.

Structured relationships remain the primary source of truth.

---

# 10. AI Agents

Agents encapsulate reusable cognitive capabilities.

Examples:

* Planner
* Issue Analyzer
* Code Generator
* Reviewer
* Documentation Writer

Agents do not orchestrate workflows.

Agents receive context.

Agents perform reasoning.

Agents produce structured outputs.

Agents remain stateless and immutable.

At execution time, an Agent Resolver loads the Agent, Prompt, Model, Tools, and Policies and produces a ResolvedAgent for invocation.

---

# 11. Tool Platform

Every non-model interaction with external systems occurs through Tools.

Examples include:

* GitHub
* Docker
* Python
* Filesystem
* Search
* Kubernetes

The platform distinguishes between:

* Data Sources
* Execution Tools
* External Services

Model providers are configured through Model resources, not Tool resources.

Every tool exposes:

* schema
* permissions
* version
* metadata

Tools execute inside isolated containers.

---

# 12. Evaluation & Governance

Every workflow may define acceptance criteria.

Generated artifacts are evaluated before publication.

Evaluation may include:

* compilation
* testing
* policy validation
* acceptance criteria
* security checks

Policies determine whether:

* execution continues
* human approval is required
* publication is permitted

The platform names two policy gates explicitly:

* Pre-Execution Capability Policy determines whether a Tool or privileged capability may run.
* Publication Policy determines whether evaluated artifacts may be published or require approval.

---

# 13. Observability

Every workflow execution produces complete provenance.

Captured information includes:

* workflow version
* execution graph
* context sources
* tool invocations
* prompts
* model versions
* generated artifacts
* evaluation results
* execution cost
* latency

Every decision within the platform should be explainable.

---

# 14. Future Evolution

The architecture intentionally separates orchestration from intelligence.

This enables future capabilities such as:

* additional event sources
* multiple model providers
* autonomous workflow generation
* agent evolution
* distributed execution
* organization-wide knowledge bases
* multi-repository workflows

without requiring changes to the core runtime.

---

# 15. Architectural Summary

AEP is built around four core concepts:

* **Controllers** reconcile events into workflow executions.
* **Workflows** orchestrate deterministic execution using DAGs.
* **Context Builders** assemble authoritative task context from structured knowledge.
* **Agents** perform bounded cognitive tasks and generate reusable engineering artifacts.

By separating orchestration, knowledge, cognition, execution, and governance into independent layers, the platform remains modular, explainable, reproducible, and extensible while providing a foundation for AI-native software engineering workflows.
