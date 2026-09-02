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

For `GeneratePatch`, `editable-targets` is a required context category distinct
from relevance-ranked `candidate-files`. After the implementation plan passes
evaluation, the trusted Context Builder reads every normalized `intendedFiles`
path from the revision-bound execution checkout. Each target appears exactly
once, in path order, with its complete UTF-8 preimage, SHA-256 content address,
byte count, token estimate, repository revision, and source provenance.
Editable targets are mandatory budget inputs. An intended path absent at the
bound revision is represented explicitly as an `ABSENT` preimage with empty
content and the SHA-256 digest of empty bytes so a planned file creation remains
possible and is still race-checked before mutation. Unreadable, non-text,
duplicate, stale, or over-budget targets fail before model invocation. No
planned target is silently pruned.

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

Every cognitive Task defines `spec.inputContextTokenBudget` as a positive
integer no greater than 1,000,000. `spec.optionalContext` declares the
lower-priority categories that may be pruned. These fields are immutable Task
configuration; the Task schema requires the budget whenever `agentRef` is
present, and callers do not choose a handler-local budget.

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

Budgets are configurable by Task and are independent of
`Model.spec.tokenLimit`, which limits model output. The deterministic
provider-neutral estimate remains canonical UTF-8 JSON bytes divided by four;
provider tokenizers do not participate in selection.

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

---

# 21. MVP Implementation Contract

The MVP implementation is `aep.context_builder.ContextBuilder`. Callers supply
an explicit Task Resource, its TaskExecution and WorkflowExecution, the
normalized Event when applicable, explicitly versioned KnowledgeBase and
Policy Resources, prior producer TaskExecution identifiers, and a deterministic
creation timestamp. The builder consumes `inputContextTokenBudget` and
`optionalContext` from the Task. A direct caller override is accepted only
when it exactly matches the declared value.

Repository elements are obtained only through the Repository Knowledge query
API. Prior GeneratedArtifacts and their verified content are obtained only
through the GeneratedArtifact store. The builder rejects mismatched revisions,
execution relationships, floating or unsupported context requirements, and
missing mandatory context. Supplied KnowledgeBase and Policy Resources must
exactly match the immutable references declared by the Task. Repository query
results must match both the WorkflowExecution repository revision and its
bound knowledge-graph version. Prior artifacts must come from successful
dependency TaskExecutions in the same WorkflowExecution and trace, and artifact
elements retain the producer TaskExecution in their provenance. Normalized
Event input must match `WorkflowExecution.eventId` and its immutable Event
reference; the MVP `github.issue.created` payload is validated before its issue
context can enter a package. Event input without a WorkflowExecution binding is
rejected.

Candidate-file retrieval applies issue-derived search terms and a hard limit
of 20 results. Repository inventory is a stable path-ordered sample limited to
20 results, so it can still serve Tasks that require structural context even
when issue terms match no path. Documentation retrieval applies issue terms
and is limited to 8 results. Every KnowledgeBase source has an explicit
positive `limit` in the self-hosting bundle; the builder applies its search
terms and falls back to a bound of 8 for older valid Resources. No
self-hosting AnalyzeIssue query uses an unbounded limit.

The same repository identity is a repository revision, knowledge snapshot,
path, and optional line/symbol slice. Identities selected through multiple
categories are emitted once before token accounting. The survivor retains all
sorted `selectionReasons`, traversal paths, Resource references, and immutable
revision/snapshot provenance. Optional duplicate metadata is merged only when
its incremental token cost fits; otherwise the mandatory representation stays
unchanged and the optional contribution is recorded as discarded.

Mandatory elements are assembled before optional candidates. Optional
candidates are selected in stable provider order while budget remains and are
otherwise recorded as discarded with a `TOKEN_BUDGET` reason. Mandatory
elements are never truncated; if they exceed the budget, construction fails.
The package records the estimator algorithm, selected and discarded context,
safe per-category element/token counts, element estimates, aggregate token
estimate, and provenance. This operational evidence contains counts and
reasons rather than issue bodies, source bodies, prompts, or credentials. Its
identifier is derived from canonical construction inputs, and the returned
value is recursively immutable.

## Planning Evidence

Candidate-file results are discovery hints, not mutation authority. A prior
evaluated issue analysis supplies bounded path predicates under the planning
Task's `PRIOR_ISSUE_ANALYSIS` contract (`STATUS_EQUALS`,
`TEXT_PRESENT`, or `TEXT_ABSENT`). Before such output becomes authoritative,
the Context Builder must materialize only the selected UTF-8 target or
slice at the WorkflowExecution revision and record its digest, source,
selected field/range, predicate result, and deterministic selection identity.
Ambiguous fields, binary or oversized content, stale revisions, duplicate
paths, and unsupported semantic predicates fail closed or remain explicitly
unsupported. Planner-returned evidence must match the independently trusted
record byte-for-byte by selection identity; its shape alone is insufficient.
Exact paths are looked up directly. Prefix scopes are enumerated independently
of relevance ranking and carry a mandatory `maxPaths`; exceeding it fails
closed instead of silently truncating. Multiple predicates use conjunction:
all preconditions must match for a required change. Planning-time no-change is
valid only when the precondition does not match and every requested
postcondition already matches; all other states remain unsupported.
Agents receive immutable evidence and never query the repository provider.
