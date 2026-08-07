# Product Requirements Document (PRD)

# Project Name

**AI Agent Engineering Platform (AEP)**

---

# Vision

Build a declarative, event-driven AI workflow platform that automatically designs, executes, evaluates, and evolves AI workflows in response to software engineering events.

Rather than acting as a traditional chatbot or coding assistant, the platform serves as an **AI Control Plane** that manages workflows, agents, knowledge, tools, prompts, policies, and evaluations as versioned resources.

The long-term vision is to become **"Kubernetes for AI Workflows"** while initially focusing on GitHub-centric software engineering automation.

---

# Problem Statement

Existing AI coding assistants are largely session-based and reactive. They lack:

* Persistent project understanding
* Structured repository knowledge
* Deterministic execution
* Workflow orchestration
* Declarative configuration
* Continuous evaluation
* Lifecycle management
* Version-controlled AI behavior

Engineering teams need a platform that can continuously observe repository events, build accurate context, execute AI workflows deterministically, and safely produce engineering artifacts such as pull requests, documentation, and design proposals.

---

# Product Scope

## Initial Scope

* Internal platform
* Single engineer
* GitHub repositories only
* No authentication system
* Kubernetes deployment
* Docker-based execution
* Human approval for production-impacting actions

## Long-Term Direction

A declarative AI control plane capable of managing reusable AI resources across workflows, repositories, and organizations.

---

# Product Goals

## Primary Goals

* Declarative AI workflows
* Event-driven execution
* Structured repository understanding
* Reusable AI agents
* Context-aware reasoning
* Automated artifact generation
* Safe autonomous execution
* Version-controlled AI configuration

---

# Success Criteria

The platform should be capable of:

* Automatically responding to GitHub events.
* Understanding repository structure without relying solely on embeddings.
* Producing implementation plans.
* Generating code changes.
* Generating documentation.
* Opening pull requests.
* Reusing agents across workflows.
* Evaluating outputs before promotion.
* Reconstructing every execution from version-controlled resources.

---

# Non-Goals

Initial versions will not support:

* Multi-tenant SaaS
* Enterprise authentication
* Continuous autonomous deployment
* Fine-tuning foundation models
* Long-running conversational agents
* Non-software engineering workflows

---

# Product Philosophy

The platform follows five core principles.

## 1. Everything is Declarative

AI behavior is described through version-controlled resources rather than imperative code.

## 2. Everything is Event Driven

External events trigger deterministic workflows.

## 3. Knowledge Over Conversation

Repository knowledge is the source of truth. Conversation history is not treated as persistent memory.

## 4. Context is a First-Class Capability

Every AI task receives context assembled from authoritative sources rather than arbitrary chat history.

## 5. Every Autonomous Action is Governed

Potentially destructive actions require evaluation and, where appropriate, human approval.

---

# High-Level Architecture

```text
GitHub Events
        │
        ▼
 Event Controller
        │
        ▼
 Workflow Scheduler
        │
        ▼
 Workflow Graph
        │
 ┌──────┼───────────────┐
 │      │               │
 ▼      ▼               ▼
Context Agent      Evaluation
Builder Graph         Graph
 │      │               │
 └──────┼───────────────┘
        ▼
 Artifact Store
        │
 Human Approval
        │
 Deployment
```

---

# Core Concepts

## Workflow

A deterministic DAG describing execution.

Responsibilities:

* orchestration
* scheduling
* dependency resolution
* retries
* state transitions

A workflow owns execution but contains no domain intelligence.

---

## Task

A Task is the fundamental unit of work within a Workflow.

Responsibilities:

* objective definition
* input and output contracts
* required context declaration
* assigned Agent reference
* evaluation hooks
* generated output definitions

A Task describes what must be accomplished, while the Workflow determines when it runs.

---

## Agent

An Agent performs cognitive work.

Examples:

* Planner
* Coder
* Reviewer
* Documentation Writer
* Issue Analyzer

Agents are reusable across workflows.

Agents are immutable and versioned.

Different workflows may reuse the same model while providing different prompts, tools, and policies.

---

## Context Builder

The Context Builder is responsible for assembling the minimum sufficient context for every AI task.

Possible inputs include:

* Repository graph
* Source files
* Documentation
* ADRs
* Coding standards
* Policies
* Previous artifacts
* Acceptance criteria
* Related GitHub issues
* Pull requests

The LLM never retrieves knowledge directly.

Instead, every execution begins with deterministic context construction.

---

## Repository Knowledge

The repository is continuously analyzed into structured knowledge.

The platform stores:

* File hierarchy
* AST
* Symbols
* Imports
* Dependency graph
* Test relationships
* Documentation
* Git history
* Issues
* Pull requests

Embeddings are treated as an optimization layer rather than the primary representation.

---

# Persistent Data Model

Instead of conversational memory, the platform persists four categories of knowledge.

## Repository Knowledge

Continuously updated project model derived from source code.

## Organizational Knowledge

Coding standards, architecture principles, policies, and documentation.

## Workflow History

Execution records including:

* Inputs
* Outputs
* Tool usage
* Costs
* Duration
* Errors
* Agent versions
* Workflow versions

## Generated Artifacts

Durable outputs such as:

* Plans
* Design documents
* Pull request descriptions
* Reviews
* Generated code
* Evaluation reports

---

# Functional Requirements

## Workflow Runtime

Provide a deterministic DAG execution engine supporting:

* dependency scheduling
* retries
* shared execution state
* checkpointing
* cancellation
* parallel execution
* structured logging

---

## Event Controller

Normalize GitHub events into platform events.

Supported events:

* Issue Created
* Issue Updated
* Pull Request
* Push
* Review Request
* Release
* Scheduled Jobs

Future integrations should be possible without changing workflow definitions.

---

## Tool Platform

Every non-model external capability is represented as a Tool resource.

Tool categories include:

* Data Sources
* Execution
* External Services

Model providers are represented by Model resources, not Tool resources.

Every tool exposes:

* Metadata
* Input schema
* Output schema
* Permissions
* Version
* Cost metadata

Tools execute within Docker containers orchestrated by Kubernetes.

Permissions are administrator-controlled and may be delegated to agents.

---

## Planning Engine

Generate deterministic execution plans.

Responsibilities:

* Task decomposition
* Dependency analysis
* Prioritization
* Structured output

Reflection is optional and may be enabled per workflow.

---

## Repository Intelligence

Support multiple programming languages.

Repository analysis should primarily rely on:

* AST
* Symbol extraction
* Dependency analysis

Embeddings supplement semantic search for documentation and unstructured knowledge.

---

## Evaluation Engine

Every workflow may define acceptance criteria.

Evaluations may verify:

* Compilation
* Test execution
* Acceptance criteria
* Knowledge compliance
* Generated artifacts

Promotion depends on workflow-defined evaluation policies.

---

## Human Approval

Human approval is required before actions that directly affect local or production environments, including:

* Merging pull requests
* Deployments
* Installing new tools
* Other policy-defined privileged operations

---

# Declarative Resource Model

Every platform capability is represented as a versioned resource.

## Workspace

Defines repository-level AI configuration boundaries.

## Workflow

Defines event triggers and execution graphs.

## Task

Defines a unit of work, including objective, inputs, outputs, required context, assigned agent, and evaluations.

## Agent

Defines reusable cognitive behavior.

## Prompt

Versioned instructions consumed by agents.

## Tool

Non-model external capability exposed to workflows.

## Model

References an LLM or embedding model independent of agent definitions.

## KnowledgeBase

Structured knowledge available to the Context Builder.

## Policy

Permission and governance rules.

## Evaluation

Acceptance criteria and validation logic.

## Event

Normalized events consumed by controllers.

---

# Repository Layout

Every repository may contain AI configuration alongside application code.

```text
.ai/

    workspace.yaml
    workflows/
    tasks/
    agents/
    prompts/
    evaluations/
    knowledge/
    policies/
    tools/
    models/
```

Generated artifacts are persisted as immutable runtime outputs, not declarative resources in the repository.

This allows AI behavior to evolve through normal Git workflows, ensuring reproducibility and code review.

---

# Development Roadmap

| Phase | Deliverable                    |
| ----- | ------------------------------ |
| 1     | DAG Workflow Runtime           |
| 2     | Repository Knowledge Service   |
| 3     | Tool Platform                  |
| 4     | Context Builder                |
| 5     | Planning Engine                |
| 6     | GitHub Event Controller        |
| 7     | Agent Resource Framework       |
| 8     | Evaluation Engine              |
| 9     | Human Approval & Policy Engine |
| 10    | AI Control Plane               |

---

# MVP

The initial MVP should demonstrate the complete engineering loop:

1. Receive a GitHub Issue event.
2. Build repository context.
3. Generate an implementation plan.
4. Produce code changes.
5. Generate tests.
6. Execute validation in Docker.
7. Evaluate acceptance criteria.
8. Open a pull request.
9. Persist execution history and generated artifacts.

## Self-Hosting Dogfood Milestone

After the generic credential-free MVP harness succeeds, the first live
repository integration is this project itself. A pinned AEP deployment observes
authenticated issue-created events for
`github:yijiazho/agent-engineering-platform`, executes the versioned
`issue-to-pr` Workflow in an isolated revision-bound checkout, and opens an
unmerged pull request back to this repository.

This milestone requires authenticated repository-bound ingress, a complete
repository-local `.ai/` Resource bundle, execution-checkout provisioning, live
GitHub and Model provider integrations, durable runtime evidence, and an
operator-controlled deployment. The running version may propose its successor
but must not modify itself, merge generated pull requests, or deploy generated
changes. ADR-004 records the decision and AEP-038 through AEP-043 track the
implementation.

---

# Long-Term Vision

The AI Agent Engineering Platform evolves into a declarative AI operating layer for software engineering. Every workspace, workflow, task, agent, prompt, tool, policy, knowledge source, evaluation, and model is managed as a version-controlled resource. Controllers continuously reconcile repository events into deterministic AI workflows, while Context Builders assemble authoritative knowledge and governance policies ensure autonomous changes remain safe, explainable, reproducible, and reviewable.
