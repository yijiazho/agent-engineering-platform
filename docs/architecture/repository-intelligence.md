# Repository Intelligence

**Project:** AI Agent Engineering Platform (AEP)

**Document:** Repository Intelligence

**Status:** Draft

Planning-evidence inspection is a trusted, revision-bound inventory consumer,
not an Agent retrieval capability. Context Builder selects exact or bounded
prefix candidates, then the checkout reader checks regular-file kind and size
against versioned Task ceilings before materialization. Predicate evidence is
bound to the complete blob digest and size and persists no repository body.

**Version:** 0.1

---

# 1. Overview

Repository Intelligence transforms a software repository into a structured, queryable knowledge model.

Rather than treating source code as text, the platform compiles the repository into a **Repository Knowledge Graph (RKG)** that captures structural, semantic, and historical relationships.

The Repository Knowledge Graph becomes the authoritative representation consumed by the Context Builder.

The repository itself remains the source of truth.

---

# 2. Goals

Repository Intelligence should provide:

* Fast repository queries
* Multi-language support
* Incremental updates
* Explainable relationships
* Deterministic analysis
* Rich semantic navigation

---

# 3. Non-Goals

Repository Intelligence does **not**:

* perform AI reasoning
* build prompts
* execute workflows
* invoke models
* retrieve context for Tasks

Its responsibility ends at producing and maintaining the Repository Knowledge Graph.

---

# 4. High-Level Architecture

```text
              Git Repository
                     │
                     ▼
              Repository Scanner
                     │
                     ▼
             Language Analyzer
                     │
                     ▼
                AST Generator
                     │
                     ▼
              Symbol Extractor
                     │
                     ▼
           Relationship Builder
                     │
                     ▼
      Repository Knowledge Graph
                     │
                     ▼
               Query Engine
                     │
                     ▼
              Context Builder
```

---

# 5. Design Philosophy

Traditional RAG systems model repositories as collections of documents.

AEP models repositories as graphs.

The platform answers questions through graph traversal before semantic retrieval.

For example:

Instead of asking

> Find "UserService"

the platform resolves

Repository

↓

File

↓

Class

↓

Method

↓

Callers

↓

Dependencies

↓

Tests

↓

Recent Commits

This enables deterministic navigation independent of embedding quality.

---

# 6. Analysis Pipeline

Repository analysis occurs in stages.

## Stage 1 — Repository Scanner

Discovers:

* files
* directories
* language
* build systems
* configuration

Produces a repository inventory.

---

## Stage 2 — Language Analyzer

Language-specific analyzers parse supported languages.

Initially supported:

* Python
* TypeScript
* JavaScript
* Go
* Java
* C#
* Rust

Each analyzer produces a normalized intermediate representation.

---

## Stage 3 — AST Generation

Each source file is parsed into an Abstract Syntax Tree.

ASTs remain immutable snapshots tied to a repository revision.

ASTs are not queried directly by higher-level systems.

---

## Stage 4 — Symbol Extraction

Extracts semantic entities such as:

* modules
* namespaces
* classes
* interfaces
* structs
* enums
* functions
* methods
* variables
* constants

Each symbol receives a stable identifier.

---

## Stage 5 — Relationship Builder

Builds explicit relationships between symbols.

Examples include:

* imports
* inheritance
* implementation
* calls
* references
* ownership
* containment
* dependency

Relationships become graph edges.

---

## Stage 6 — Knowledge Graph Publication

The completed graph is published as a new Repository Knowledge Graph version.

Older versions remain available for reproducibility.

---

# 7. Repository Knowledge Graph

The Repository Knowledge Graph is the canonical semantic model of the repository.

Every node represents a software entity.

Every edge represents an explicit relationship.

Example node types include:

* Repository
* Directory
* File
* Module
* Class
* Interface
* Function
* Method
* Test
* Document
* Issue
* Pull Request

---

# 8. Relationship Types

Example relationships include:

Repository

→ contains

Directory

Directory

→ contains

File

File

→ defines

Class

Class

→ defines

Method

Method

→ calls

Method

Method

→ tested_by

Test

Issue

→ implemented_by

Pull Request

Document

→ references

Class

The graph intentionally models software semantics rather than syntax.

---

# 9. Incremental Analysis

Repository analysis is incremental.

GitHub events determine affected files.

Only impacted portions of the graph are rebuilt.

Typical flow:

```text
Git Push

↓

Changed Files

↓

Reparse

↓

Update Graph

↓

Publish New Graph Version
```

The entire repository should rarely require rebuilding.

---

# 10. Query Engine

The Query Engine provides deterministic access to the Repository Knowledge Graph.

The public `RepositoryKnowledgeProvider` contract supports exact file lookup,
documentation lookup, dependency manifest lookup, test hint lookup, and
candidate-file search. Typed queries use structured terms, repository-relative
path prefixes, language or ecosystem filters, and result limits. They do not
invoke an LLM.

All providers return the same immutable `KnowledgeResult` shape. Each result
includes the repository revision, snapshot version and producer, source path and
optional line or symbol span, and a traversal path explaining its selection.
Snapshot record attributes are restricted to JSON-compatible values and are
recursively frozen when published so later queries cannot observe mutation.
The MVP `InMemoryRepositoryKnowledgeProvider` evaluates these queries over the
flat scanner snapshot. A future graph or AST provider may use richer traversal
internally while preserving this caller-facing contract.

Results use a stable ordering: descending deterministic match score, followed by
case-normalized source path, source path, and record identifier. Exact ties
therefore do not depend on snapshot insertion order.

Supported query types include:

Structural

* locate symbol
* parent hierarchy
* child hierarchy

Dependency

* callers
* callees
* imports
* references

Testing

* associated tests
* impacted tests

Documentation

* related ADRs
* related markdown

History

* recent commits
* related issues
* previous pull requests

The Query Engine owns bounded candidate retrieval. Its lexical and graph query
paths remain deterministic. A revision-bound semantic-index query is permitted
only as an advisory candidate signal and is available to Context Builder through
the same provider boundary; it never replaces exact lookup, planning-evidence
reads, graph traversal, or typed structural queries.

---

# 11. Semantic Layer

Embeddings complement the graph and may nominate revision-bound source-code
files or structure-aware chunks for bounded candidate discovery when issue
vocabulary differs from repository vocabulary.

They are used for bounded candidate retrieval over:

* documentation
* ADRs
* markdown
* design documents
* issue discussions
* source-code files and structure-aware chunks

Embeddings are **not** used as authority for:

* trusted structural navigation
* dependency resolution
* symbol lookup

Graph traversal and lexical exact matches remain the deterministic path for
structural and identifier queries. Semantic similarity is advisory, cannot
prove a repository fact, and never authorizes planning or mutation.

---

# 12. Language Plugin Architecture

Each language implements a common analyzer interface.

Responsibilities include:

* parsing
* symbol extraction
* relationship generation

Language plugins produce a normalized intermediate model consumed by the Relationship Builder.

This enables consistent behavior across languages.

---

# 13. Repository Revisions

Every Repository Knowledge Graph corresponds to exactly one repository revision.

```text
Repository

Commit

abc123

↓

Knowledge Graph

v87
```

Workflow executions always reference a specific graph version.

This guarantees reproducibility.

---

# 14. Observability

Repository analysis records:

* analysis duration
* parsed files
* changed files
* extracted symbols
* relationship counts
* graph version
* analysis errors

These metrics support debugging and performance optimization.

---

# 15. Future Extensions

The architecture supports additional graph nodes without changing the query model.

Potential future nodes include:

* Kubernetes manifests
* Database schemas
* API specifications
* Infrastructure resources
* Build pipelines
* Deployment topology

The Repository Knowledge Graph should evolve alongside the repository.

---

# 16. Design Principles

## Source Code Is Structured Data

Source code should never be treated as plain text when structural information is available.

---

## Graph Before Embeddings

Graph traversal precedes semantic retrieval.

Embeddings supplement but never replace explicit relationships.

---

## Incremental Compilation

Repository analysis behaves like a compiler.

Only changed components are recompiled.

---

## Immutable Knowledge

Every graph corresponds to a specific repository revision.

Knowledge never changes retroactively.

---

## Explainable Queries

Every query result must identify:

* originating file
* repository revision
* graph traversal path

No result should appear without provenance.

---

# 17. Summary

Repository Intelligence functions as the knowledge compiler of AEP.

It continuously transforms Git repositories into immutable Repository Knowledge Graphs through deterministic analysis, AST parsing, symbol extraction, and relationship modeling.

The resulting graph provides a language-agnostic semantic representation of the repository that enables the Context Builder to assemble precise, explainable, and reproducible ContextPackages without relying on source code parsing or vector search during workflow execution.
Candidate-file results remain bounded discovery metadata. They do not authorize
editing and do not satisfy patch input requirements. Exact editable preimages
are materialized separately by the trusted Context Builder from the immutable
execution checkout; Agents never call repository queries or Filesystem reads.

## Evidence-Bound Planning Queries

Relevance ranking finds bounded candidates but never proves a requested file
condition. Exact planning evidence is a separate revision-bound read whose
persisted result contains no full file body: path, revision, SHA-256 preimage,
source identity, selected field or match count, predicate, and result. The same
snapshot, predicates, and contents produce the same selection identity.

Planning evidence is keyed by `(path, scope)`, not only by path. A path can
therefore carry multiple independently selected editable Markdown regions and
whole-file evaluator-owned criteria. Each record persists its scope class,
selection identity, authorization role, and evaluator ownership without a
source body. A `null` scope is evaluator evidence only; it cannot authorize a
localized operation or substitute for a uniquely resolved scoped selection.

# Semantic And Hybrid Candidate Retrieval

AEP's repository knowledge layer may use multiple bounded retrieval signals to
nominate repository evidence before an Agent reasons about an issue. Lexical
retrieval remains authoritative for exact strings such as paths, identifiers,
Resource names, configuration keys, issue numbers, and error text. A
revision-bound semantic index may additionally retrieve files or
structure-aware chunks whose meaning is related even when their vocabulary does
not overlap the issue.

Semantic index entries are content-bound and provider-neutral. An entry records
the source content digest, path and bounded source identity, revision/snapshot
membership, exact versioned embedding `Model` Resource reference, vector
dimensions, similarity metric, and normalization rule. Identical content may
reuse an embedding across Git revisions, but query membership is always
evaluated against the exact WorkflowExecution revision. A small local persistent
backend is sufficient for the initial implementation; the repository knowledge
API, not Context Builder, owns the backend contract. It stages membership for a
revision and atomically publishes a complete index generation; a query rejects
an unavailable or incomplete generation instead of observing partial results.

Embedding calls remain outside Agent and `ModelInvocation` execution. A durable
`SemanticIndexBuild` owns revision indexing and its `EmbeddingInvocation`
records persist the resolved Model Resource, timing, classified outcome, and
safe usage metadata for every attempt, including failures before an entry is
created. Inspection exposes that evidence without credentials or source bodies.

Candidate selection should use deterministic hybrid rank fusion rather than
treating vector similarity as a replacement for lexical retrieval. Every
selected candidate preserves its component retrieval reasons and ranks plus the
fused rank and configured bounds. The exact versioned requesting `Task`
Resource supplies candidate and source limits, path/type filters, fusion
parameters, thresholds, and the semantic-index degraded-mode choice; query
provenance records that Resource reference and normalized policy.

The evidence-strength boundary is:

```text
lexical / semantic / graph retrieval
                |
                v
        candidate relevance
                |
                v
          Agent reasoning
                |
                v
       typed evidence request
                |
                v
 exact revision-bound repository read
                |
                v
         planning evidence
                |
                v
       implementation plan
                |
                v
         editable target
```

A semantic score never proves that a path requires modification and never
grants write authority. Agents receive selected candidates only through
ContextPackage construction and do not query vector, lexical, or graph
providers directly.
