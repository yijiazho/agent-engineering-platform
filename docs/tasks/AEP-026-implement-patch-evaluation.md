# AEP-026: Implement Patch Evaluation

**Status:** Completed

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

## Implementation

`src/aep/patch_evaluation.py` validates PATCH GeneratedArtifact identity,
content address, and repository revision before requesting the repository-bound
Git adapter's read-only `check_patch` operation. The adapter runs
`git apply --numstat` and `git apply --check --cached` through its injected
isolated sandbox with patch content on standard input. It requires HEAD at the
immutable expected revision and a clean index and worktree, and never applies
the patch.

When a caller supplies a ToolInvocation identity and the persisted `GitTool`
boundary, Patch Evaluation atomically claims that identity before executing
`check_patch`, records terminal Tool evidence, and cites it from the
EvaluationResult. Matching retries reuse terminal evidence; conflicting
request fingerprints fail closed.

The evaluator normalizes allowed repository-relative roots, checks both current
and previous paths for renamed files (including Git C-style octal-quoted UTF-8
paths), and persists an immutable
`EvaluationResult` containing sorted changed files, applicability diagnostics,
boundary checks, Git log provenance, and stable failure codes. Revision,
content-integrity, empty, malformed, conflicting, and out-of-scope failures are
technical `FAIL` outcomes rather than publication decisions.

Deterministic fixtures under `fixtures/patch-evaluation/` and
`tests/test_patch_evaluation.py` cover clean, conflicting, malformed, empty,
revision-mismatched, disallowed, renamed, and Git-quoted Unicode patches,
path-root semantics, worktree non-mutation, immutable persistence, and unsafe
rule rejection.
