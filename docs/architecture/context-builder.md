# Context Builder

**Project:** AI Agent Engineering Platform (AEP)

**Document:** Context Builder

**Status:** Draft

**Version:** 0.1

---

# 1. Overview

The Context Builder is responsible for constructing the **minimum sufficient context** required to execute a Task.

It is the only subsystem permitted to assemble information from repositories, knowledge bases, artifacts, and external events before invoking an Agent.

Agents never retrieve repository knowledge directly.

If an Agent needs additional repository, documentation, artifact, or workflow-history context, the workflow must create a follow-up Task that receives a new ContextPackage.

The Context Builder does **not** perform reasoning.

It prepares information for reasoning.

---

# 2. Design Goals

The Context Builder should produce context that is:

* Relevant
* Deterministic
* Explainable
* Compact
* Reproducible
* Version-aware
* Policy-compliant

---

# 3. Non-Goals

The Context Builder does not:

* execute workflows
* perform AI reasoning
* modify repositories
* execute tools
* generate code
* optimize prompts

---

# 4. Philosophy

Traditional RAG systems typically follow this pattern:

```text
Question

↓

Embedding Search

↓

Prompt

↓

LLM
```

AEP instead follows:

```text
Task

↓

Task Requirements

↓

Repository Intelligence

↓

Knowledge Selection

↓

Artifact Selection

↓

Policy Resolution

↓

Context Optimization

↓

Context Package

↓

Agent
```

Context is constructed from multiple authoritative sources rather than a single vector search.

---

# 5. Inputs

The Context Builder receives:

## Task

Defines:

* objective
* expected outputs
* required context
* evaluation hooks

---

## WorkflowExecution

Provides:

* repository revision
* triggering event
* execution metadata

---

## Repository Knowledge

Examples:

* AST
* symbols
* dependency graph
* imports
* call graph
* file ownership

---

## KnowledgeBase

Examples:

* architecture
* coding standards
* ADRs
* runbooks
* documentation

---

## Artifacts

Examples:

* implementation plans
* design docs
* review reports
* previous PRs

---

## Event

Examples:

* GitHub Issue
* Pull Request
* Commit
* Review Request

---

## Policies

Defines visibility and restrictions.

---

# 6. Outputs

The Context Builder produces exactly one output.

```
ContextPackage
```

The ContextPackage is immutable.

It contains:

* selected information
* provenance
* metadata
* token accounting

The runtime validates the ContextPackage before invoking an Agent.

---

# 7. Context Construction Pipeline

```text
Task

↓

Understand Objective

↓

Resolve Required Context

↓

Retrieve Candidates

↓

Rank Candidates

↓

Validate

↓

Optimize

↓

Assemble ContextPackage
```

Every stage is deterministic.

---

# 8. Repository Intelligence

Repository Intelligence is the primary source of context.

The Context Builder prefers structured knowledge over semantic search.

Typical retrieval order:

1. Repository graph
2. Symbols
3. AST
4. Dependency graph
5. Documentation
6. Embeddings

Embeddings are the final fallback.

---

# 9. Retrieval Sources

The Context Builder retrieves information from multiple providers.

## Repository

* source files
* AST
* symbols
* tests
* dependencies

Repository retrieval is available to the Context Builder, not directly to Agents.

---

## Documentation

* README
* ADR
* design docs
* markdown

---

## Generated Artifacts

* plans
* PR reviews
* architecture proposals

---

## Workflow History

Only when explicitly requested by the Task.

Workflow history should never dominate repository knowledge.

---

## External Events

GitHub

* Issues
* PRs
* Reviews
* Commits

---

# 10. Context Resolution Strategy

The Context Builder resolves context in layers.

```text
Task

↓

Mandatory Context

↓

Repository Context

↓

Knowledge Context

↓

Artifact Context

↓

Event Context

↓

Optional Context
```

Mandatory context is never removed.

Optional context may be discarded if budget limits are exceeded.

---

# 11. Ranking

Retrieved candidates are ranked using multiple signals.

Examples include:

Repository

* dependency distance
* symbol references
* import graph

Documentation

* explicit references
* section relevance
* recency

Artifacts

* workflow relationship
* originating Task
* repository revision

Semantic

* embedding similarity
* keyword overlap

Ranking is deterministic.

No LLM is required.

---

# 12. Context Optimization

The objective is not to maximize context.

The objective is to maximize useful information per token.

Optimization strategies include:

* deduplication
* section extraction
* symbol selection
* dependency pruning
* document compression
* artifact summarization

Compression should preserve provenance.

---

# 13. Token Budget

Every Task defines a target context budget.

Example:

```text
32K tokens
```

Budget allocation might be:

| Source     | Budget |
| ---------- | -----: |
| Repository |    50% |
| Knowledge  |    20% |
| Artifacts  |    15% |
| Event      |    10% |
| Policies   |     5% |

Budgets are configurable by Task.

---

# 14. Provenance

Every context element records its origin.

Example:

```text
Repository

↓

src/auth/user_service.py

↓

Class UserService

↓

Method authenticate()

↓

Commit 8f3b6e2
```

Or:

```text
ADR-004

↓

Section 3.2

↓

Architecture Decision
```

Every generated output can therefore explain where supporting information originated.

---

# 15. Validation

Before publishing a ContextPackage the Context Builder validates:

* schema
* provenance
* duplicate detection
* policy compliance
* token limits
* required context completeness

Invalid ContextPackages are rejected.

---

# 16. Caching

Context construction is deterministic.

Equivalent requests should reuse previous work where possible.

Examples:

* repository graph
* parsed AST
* dependency graph
* symbol index
* document summaries

Caching should occur below the Context Builder API.

The API itself remains deterministic.

---

# 17. Observability

Every ContextPackage records:

* retrieval time
* retrieval sources
* discarded candidates
* ranking scores
* token allocation
* compression ratio
* provenance completeness

This enables debugging and future optimization.

---

# 18. Future Enhancements

The architecture intentionally separates retrieval from optimization.

Future capabilities may include:

* repository-wide semantic search
* graph traversal heuristics
* personalized knowledge
* cross-repository retrieval
* learned ranking models
* adaptive token budgeting
* retrieval evaluation

These enhancements should not require changes to the ContextPackage interface.

---

# 19. Design Principles

## Structured Before Semantic

Prefer explicit repository structure over embeddings.

---

## Deterministic Retrieval

The same inputs should produce equivalent ContextPackages.

---

## Explainable Context

Every included element must have provenance.

---

## Minimum Sufficient Context

More context is not necessarily better.

The objective is to maximize relevance per token.

---

## Separation of Concerns

The Context Builder assembles knowledge.

Agents consume knowledge.

The runtime orchestrates execution.

No component should assume another's responsibility.

---

# 20. Summary

The Context Builder is the knowledge assembly engine of AEP.

It transforms Tasks and repository state into immutable ContextPackages by combining structured repository intelligence, knowledge bases, artifacts, policies, and events through a deterministic retrieval and optimization pipeline.

By treating context construction as a first-class subsystem rather than a helper function, AEP ensures that every Agent operates on compact, explainable, reproducible, and authoritative information, independent of the underlying LLM.
