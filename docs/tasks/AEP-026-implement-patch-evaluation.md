# AEP-026: Implement Patch Evaluation

**Status:** Not Started

## Context

GeneratePatch must produce changes that apply cleanly and stay within allowed workspace boundaries.

## Deliverable

Implement patch validation Evaluation.

## Dependencies

* AEP-002
* AEP-022

## Acceptance Criteria

* Evaluation verifies patch applies cleanly to the expected repository revision.
* Evaluation verifies changed files are within allowed paths.
* Evaluation records changed file list as evidence.
* Evaluation fails before validation if patch is invalid.
* Tests cover clean patch, conflicting patch, and disallowed path.
