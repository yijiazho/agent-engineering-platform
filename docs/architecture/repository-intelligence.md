# Repository Intelligence

**Project:** AI Agent Engineering Platform (AEP)

**Document:** Repository Intelligence

**Status:** Draft

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

The Query Engine never performs semantic search.

---

# 11. Semantic Layer

Embeddings complement the graph.

They are used primarily for:

* documentation
* ADRs
* markdown
* design documents
* issue discussions

Embeddings are **not** used for:

* source code navigation
* dependency resolution
* symbol lookup

Graph traversal always takes precedence.

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
