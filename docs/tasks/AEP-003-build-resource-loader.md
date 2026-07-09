# AEP-003: Build Resource Loader

**Status:** Not Started

## Context

Resources are declared in repository-local `.ai/` configuration. Controllers need a deterministic way to load and validate them.

## Deliverable

Implement a Resource Loader that reads `.ai/`, validates schemas, resolves explicit versions, and returns typed Resource objects.

## Dependencies

* AEP-001

## Acceptance Criteria

* Loader discovers `workspace.yaml` and resource directories.
* Loader validates all supported Resource schemas.
* Loader fails on missing references.
* Loader fails on floating versions.
* Loader returns stable ordering for deterministic reconciliation.
* Unit tests cover valid resources, invalid schemas, missing files, and duplicate versions.
