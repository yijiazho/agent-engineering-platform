# AEP-021: Implement Filesystem Tool

**Status:** Not Started

## Context

GeneratePatch needs controlled filesystem read/write against a workspace checkout.

## Deliverable

Implement filesystem Tool adapter.

## Dependencies

* AEP-019
* AEP-020

## Acceptance Criteria

* Supports read and write operations declared by schema.
* Enforces workspace path boundaries.
* Requires Pre-Execution Capability Policy for write operations.
* Records ToolInvocation logs and outputs.
* Tests cover allowed read, allowed write, path traversal denial, and schema failure.
