# AEP-026: Implement Patch Evaluation

**Status:** Not Started

## Context

A generated patch must be technically safe to validate before build or test commands run. Patch Evaluation verifies that the patch applies to the expected repository revision and that every changed path remains inside the configured workspace and Task scope.

This is deterministic correctness checking, not permission to write, push, or publish. Its immutable EvaluationResult supplies evidence to later acceptance and Publication Policy decisions.

## Deliverable

Implement patch Evaluation that:

* accepts a patch artifact, expected repository revision, and allowed path rules;
* uses the Git adapter to test clean applicability without publishing changes;
* records changed files, applicability diagnostics, and boundary checks as evidence;
* fails deterministically for conflicts, malformed patches, revision mismatch, or disallowed paths; and
* tests clean, conflicting, empty, and out-of-scope patches.

## Dependencies

* AEP-002
* AEP-022

## Acceptance Criteria

* Evaluation verifies patch applies cleanly to the expected repository revision.
* Evaluation verifies changed files are within allowed paths.
* Evaluation records changed file list as evidence.
* Evaluation fails before validation if patch is invalid.
* Tests cover clean patch, conflicting patch, and disallowed path.
