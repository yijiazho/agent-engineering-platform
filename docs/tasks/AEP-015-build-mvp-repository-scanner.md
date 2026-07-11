# AEP-015: Build MVP Repository Scanner

**Status:** Not Started

## Context

The long-term Repository Intelligence design compiles ASTs, symbols, and relationships into a versioned Repository Knowledge Graph. ADR-003 deliberately permits a simpler MVP scanner, provided its output remains revision-bound, deterministic, provenance-rich, and replaceable behind a stable query API.

This task establishes that initial compiled knowledge snapshot. It inventories repository structure and useful engineering signals without performing reasoning, prompt construction, or semantic retrieval.

## Deliverable

Implement an MVP repository scanner that:

* walks a repository at a specified revision using deterministic inclusion and exclusion rules;
* records files, detected languages, documentation, dependency manifests, and test-command hints;
* attaches source paths, revision, timestamps, and scanner version as provenance;
* publishes a versioned immutable snapshot suitable for AEP-016 queries; and
* tests common layouts, ignored vendor/generated paths, and repeatable output.

## Dependencies

None.

## Acceptance Criteria

* Scanner produces a versioned repository knowledge snapshot.
* Snapshot is tied to repository revision.
* Scanner ignores generated and vendor directories by default.
* Scanner detects common dependency manifests.
* Tests use small fixture repositories.
