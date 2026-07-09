# AEP-022: Implement Git Tool

**Status:** Not Started

## Context

The MVP needs branch creation, diff inspection, and push.

## Deliverable

Implement Git Tool adapter.

## Dependencies

* AEP-019
* AEP-020

## Acceptance Criteria

* Supports create branch, diff, status, and push branch operations.
* Push requires Pre-Execution Capability Policy.
* Outputs structured changed file and diff metadata.
* ToolInvocation records command logs without leaking secrets.
* Tests use a local fixture repository.
