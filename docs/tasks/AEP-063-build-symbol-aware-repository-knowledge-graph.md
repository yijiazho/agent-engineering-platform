# AEP-063: Build Symbol-Aware Repository Knowledge Graph

**Status:** Not Started

## Context

AEP's Context Builder and repository knowledge layer are central to the platform's distinction from agents that autonomously browse a checkout. The MVP scanner primarily exposes file-oriented repository evidence. File-level retrieval is insufficient for precise context construction once issues refer to symbols, call relationships, interfaces, tests, or architectural dependencies.

The next knowledge layer should remain deterministic, revision-bound, provenance-complete, and provider-neutral. It must improve minimum-sufficient context construction without giving Agents direct repository discovery access.

## Deliverable

Extend repository knowledge with a symbol-aware graph that can represent definitions and deterministic relationships among symbols, files, imports/references, callers/callees where statically available, tests, commits, issues, pull requests, and documentation references.

Add query primitives and Context Builder integration that use the graph to select bounded relevant evidence while preserving the existing immutable revision and knowledge-snapshot contracts.

## Dependencies

* AEP-015
* AEP-016
* AEP-017
* AEP-039
* AEP-045

## Acceptance Criteria

* The repository scanner produces stable symbol identities for at least the repository's primary implementation language and binds every node/edge to the exact repository revision and knowledge snapshot.
* The graph represents symbol definitions and file containment plus import/reference and test relationships where those relationships can be determined statically.
* Knowledge queries can start from a file path, symbol name/identity, or issue-derived search term and traverse a bounded, explicitly declared relationship set.
* Query results include traversal path, selection reason, source location, revision, and snapshot provenance sufficient for ContextPackage inspection.
* Context Builder can use graph queries to include a named symbol's implementation, interface/dependencies, relevant callers, and tests without requiring a full repository inventory in the model context.
* Graph traversal has deterministic ordering, depth, fan-out, and result limits and cannot expand without a configured bound.
* Unsupported or ambiguous language constructs degrade to documented file/text evidence rather than producing invented graph relationships.
* Incremental indexing, if implemented, produces the same canonical graph identity as a full rebuild for the same repository revision.
* Tests cover symbol identity stability, relationship extraction, bounded traversal, ambiguous symbols, test linkage, revision isolation, and deterministic ContextPackage selection.
* Architecture and Context Builder documentation describe the knowledge graph as platform-owned evidence and preserve the rule that Agents do not directly browse repository knowledge providers.
