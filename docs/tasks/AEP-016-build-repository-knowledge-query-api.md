# AEP-016: Build Repository Knowledge Query API

**Status:** Completed

## Context

The Context Builder must consume repository knowledge through a stable, deterministic API rather than inspect the checkout directly. The MVP backend is the simplified snapshot from AEP-015, but callers must not depend on that representation so AST and graph-backed implementations can replace it later.

Queries perform structured lookup and candidate selection without LLM calls. Every result must explain its repository revision and source location and use stable ordering.

## Deliverable

Implement a repository knowledge query API that:

* defines provider-neutral query and result types over a versioned snapshot;
* supports file, documentation, dependency manifest, test hint, and candidate-file queries;
* returns provenance and revision metadata for every result;
* guarantees deterministic filtering and ordering; and
* includes an in-memory MVP implementation and tests that leave room for graph-backed results.

## Dependencies

* AEP-015

## Acceptance Criteria

* API supports file lookup, docs lookup, dependency manifest lookup, test hint lookup, and candidate file search.
* API returns provenance for every result.
* API does not rely on LLM calls.
* API contract can later support AST-backed results without caller changes.
* Tests verify deterministic result ordering.
