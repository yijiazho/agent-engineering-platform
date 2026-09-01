# ADR-001: Declarative Resource Model

**Status:** Proposed

**Authors:** Project Team

**Date:** 2026-07-07

---

# Context

AEP adopts a Resource-Oriented Architecture inspired by Kubernetes, where every major platform capability is represented as a declarative, immutable, versioned resource.

Resources describe desired behavior. Controllers reconcile those resources into runtime execution.

The objective is to separate orchestration, work definition, cognition, execution, and governance into independent, composable layers.

---

# Decision

The platform defines the following first-class resources:

* Workspace
* Workflow
* Task
* Agent
* Prompt
* Model
* Tool
* KnowledgeBase
* Policy
* Evaluation
* Event

Each resource has a single responsibility and may reference other resources through immutable versioned identifiers.

---

# Resource Hierarchy

```text
Repository

└── Workspace
    │
    ├── Workflow
    │     └── Task*
    │
    ├── Agent
    │
    ├── Prompt
    │
    ├── Model
    │
    ├── Tool
    │
    ├── KnowledgeBase
    │
    ├── Evaluation
    │
    ├── Policy
    │
    │
    └── Event
```

---

# Architectural Responsibilities

| Resource      | Responsibility                          |
| ------------- | --------------------------------------- |
| Workspace     | Defines repository AI configuration boundary |
| Workflow      | Orchestrates execution                  |
| Task          | Defines a unit of work                  |
| Agent         | Performs cognitive reasoning            |
| Prompt        | Defines behavioral instructions         |
| Model         | Defines AI model configuration          |
| Tool          | Provides non-model external capabilities |
| KnowledgeBase | Supplies authoritative knowledge        |
| Evaluation    | Defines success criteria                |
| Policy        | Governs execution                       |
| Event         | Represents normalized external triggers |

---

# Workspace

A Workspace defines the AI configuration boundary for a repository.

Responsibilities:

* Resource ownership
* Default policies
* Repository binding
* Resource discovery
* Version scope

A Workspace does not execute workflows or perform reasoning.

---

# Workflow

A Workflow defines **how work is orchestrated**.

Responsibilities:

* Event triggers
* DAG definition
* Task dependencies
* Execution policies
* Retry strategies
* Parallelism

A Workflow **does not** define:

* prompts
* models
* repository logic
* cognitive behavior

A Workflow references Tasks.

Example:

```text
Issue Created

↓

Analyze Issue

↓

Generate Plan

↓

Implement

↓

Review

↓

Evaluate
```

---

# Task

Task is the fundamental execution unit of the platform.

A Task describes **what must be accomplished**, independent of how it is implemented.

Responsibilities include:

* Objective
* Inputs
* Outputs
* Required context
* Assigned Agent
* Evaluation hooks
* Generated output definitions
* Dependency contracts

A Task does not orchestrate execution and does not contain reasoning logic.

Instead, it specifies the contract for a unit of work.

Example:

```yaml
task:
  id: implement-feature

agent: code-generator

context:
  - repository
  - issue
  - architecture

outputs:
  - code_patch

evaluation:
  - compile
  - tests
```

Tasks are reusable across multiple workflows.

Different agents may execute the same task without modifying the workflow.

---

# Agent

An Agent encapsulates cognitive capability.

Examples include:

* Planner
* Code Generator
* PR Reviewer
* Documentation Writer
* Issue Analyzer

Responsibilities:

* Reasoning
* Structured generation
* Tool usage
* Model interaction

An Agent references:

* Prompt
* Model
* Tool Set
* Policies

Agents never retrieve repository knowledge directly.

Repository, documentation, artifact, and workflow-history context must be supplied through deterministic ContextPackages produced by the Context Builder.

Agents remain:

* Stateless
* Immutable
* Versioned
* Reusable

Agents never define orchestration.

At execution time, an Agent Resolver loads the referenced Agent, Prompt, Model, Tools, and Policies and produces a ResolvedAgent runtime object.

The Agent Resolver owns no durable state and is not a standalone runtime service.

---

# Prompt

Prompts are independent resources.

Separating prompts from agents allows prompt evolution without changing workflow definitions.

Prompt resources define:

* System instructions
* Formatting requirements
* Output guidance
* Examples

---

# Model

Model resources abstract LLM providers.

Models define:

* Provider
* Model identifier
* Parameters
* Retry policy
* Token limits
* Timeout configuration

Agents reference Models rather than providers directly.

---

# Tool

Tools represent non-model external capabilities.

Categories include:

* Data Sources
* Execution
* External Services

Tool definitions include:

* Input schema
* Output schema
* Permissions
* Execution image
* Retry policy
* Timeout

Tools remain reusable platform resources.

---

# KnowledgeBase

KnowledgeBase resources describe structured knowledge available to Context Builders.

Examples:

* Repository
* Architecture
* Coding Standards
* ADRs
* Documentation
* Runbooks

KnowledgeBase specifies:

* Ownership
* Indexing strategy
* Refresh policy
* Visibility

It intentionally does not prescribe storage implementation.

---

# Policy

Policies define governance.

Examples:

* Human approval required
* Docker execution allowed
* Tool installation restricted
* Repository write access

Policies may be attached to:

* Workflow
* Task
* Agent
* Tool
* Evaluation

---

# Evaluation

Evaluations define acceptance criteria.

Typical validators include:

* Compilation
* Unit tests
* Static analysis
* Acceptance criteria
* Security validation

Evaluations are reusable resources shared across workflows.

# Event

Events normalize external systems into platform events.

Initial provider:

* GitHub

Future providers:

* GitLab
* Slack
* Jira
* Email
* CI Systems

Events trigger Workflow reconciliation.

---

# Resource Relationships

```text
Event
   │
   ▼
Workflow
   │
   ▼
Task
   │
   ▼
Agent
   ├─────────────┐
   ▼             ▼
Prompt         Model
   │             │
   └──────┬──────┘
          ▼
        Tool*

Task
 ├────────► KnowledgeBase*
 ├────────► Evaluation*
 ├────────► Policy*

Workflow
 └────────► Policy*
```

No cyclic dependencies are permitted.

---

# Execution Model

Execution itself is **not** a Resource.

Execution is runtime state produced by reconciling Resources.

```text
Event

↓

Workflow

↓

Task DAG

↓

Context Builder

↓

Agent

↓

Artifacts

↓

Evaluation

↓

Policy

↓

Completion
```

Execution history remains fully reproducible through immutable resource versions.

---

# Versioning

Every Resource follows semantic versioning.

Examples:

```text
workflow/issue-fix:v1.2.0

task/implement-feature:v2.0.1

agent/code-generator:v3.4.0

prompt/planner:v5.0.0
```

Resources always reference explicit versions.

Floating references (e.g., `latest`) are prohibited.

---

# Repository Layout

```text
.ai/

    workspace.yaml

    workflows/

    tasks/

    agents/

    prompts/

    models/

    tools/

    evaluations/

    policies/

    knowledge/

```

Git remains the authoritative source of truth for AI configuration.

---

# Controllers

Each Resource is reconciled by a dedicated Controller.

Examples include:

* WorkflowController
* TaskController
* AgentController
* KnowledgeController
* ToolController
* PolicyController
* EvaluationController

Controllers are responsible only for:

1. Observing Resources
2. Validating Resources
3. Reconciling desired state
4. Publishing Resource status

Controllers never perform AI reasoning.

---

# Design Consequences

## Benefits

* Clear separation of orchestration, work definition, and cognition.
* Highly reusable Tasks and Agents.
* Deterministic execution model.
* Independent evolution of prompts, models, and workflows.
* Strong reproducibility through immutable resources.
* Git-native configuration and review.

## Trade-offs

Provider-neutral Agent `outputSchema` remains a complete JSON Schema contract.
When its resolved Model selects OpenAI strict Structured Outputs, Resource
loading additionally audits the provider subset recursively. Closed objects
must require every declared property; semantic optionality is represented by a
required property with an explicit nullable `anyOf` branch. Provider projection
may remove only documented generation-time validation hints and cannot alter
property names, requiredness, enums, or nullability. The complete Resource
schema remains authoritative for post-response evaluation.
For the deployed OpenAI Responses API / `gpt-5` contract, `const` is not an
accepted provider keyword. Authors use an explicit scalar `type` plus a
singleton `enum` for discriminators. Endpoint/model support is recorded in one
code-level compatibility matrix rather than inferred from Draft 2020-12.

* Increased number of resource types.
* More explicit dependency management.
* Higher initial implementation complexity.

These trade-offs are acceptable because they improve long-term maintainability, composability, and scalability.

---

# Future Extensions

The model intentionally supports additional Resources without changing the platform architecture.

Potential future Resources include:

* Memory
* Dataset
* Benchmark
* Experiment
* Secret
* Connector
* Scheduler
* Plugin
* Template

Every future capability should follow the same declarative, immutable, controller-driven resource model.
