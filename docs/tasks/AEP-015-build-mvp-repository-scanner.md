# AEP-015: Build MVP Repository Scanner

**Status:** Not Started

## Context

The MVP may use simplified Repository Intelligence before full AST and symbol graph support exists.

## Deliverable

Implement repository scanning for file inventory, language detection, README/docs index, dependency manifests, and test command hints.

## Dependencies

None.

## Acceptance Criteria

* Scanner produces a versioned repository knowledge snapshot.
* Snapshot is tied to repository revision.
* Scanner ignores generated and vendor directories by default.
* Scanner detects common dependency manifests.
* Tests use small fixture repositories.
