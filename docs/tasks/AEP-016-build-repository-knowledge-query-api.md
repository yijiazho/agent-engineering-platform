# AEP-016: Build Repository Knowledge Query API

**Status:** Not Started

## Context

The Context Builder should query repository knowledge through a stable API, even if the MVP backend is simplified.

## Deliverable

Implement query interface over the MVP repository knowledge snapshot.

## Dependencies

* AEP-015

## Acceptance Criteria

* API supports file lookup, docs lookup, dependency manifest lookup, test hint lookup, and candidate file search.
* API returns provenance for every result.
* API does not rely on LLM calls.
* API contract can later support AST-backed results without caller changes.
* Tests verify deterministic result ordering.
