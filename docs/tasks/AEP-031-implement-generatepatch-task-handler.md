# AEP-031: Implement GeneratePatch Task Handler

**Status:** Completed

## Context

`GeneratePatch` is the first workflow Task that mutates an execution checkout. It consumes the implementation plan, invokes the Code Generator within its resolved Tool allowlist, and produces a reviewable patch rather than publishing changes.

All reads and writes must use scoped Tool adapters and pass capability policy. The resulting patch and changed-file evidence must be immutable and pass Patch Evaluation before validation begins.

## Deliverable

Implement the `GeneratePatch` Task handler that:

* builds context from the plan, repository knowledge, policies, and allowed workspace;
* resolves and invokes the Code Generator with only authorized Filesystem and Git capabilities;
* persists the patch and changed-file metadata as GeneratedArtifacts;
* runs Patch Evaluation and records ToolInvocations and lifecycle transitions; and
* tests success, denied capability, disallowed path, invalid patch, and Tool failure.

## Dependencies

* AEP-021
* AEP-022
* AEP-026
* AEP-030

## Acceptance Criteria

* Handler consumes implementation plan GeneratedArtifact.
* Handler uses only Tools allowed by ResolvedAgent and policies.
* Handler persists patch GeneratedArtifact.
* Handler records changed files.
* Handler runs patch Evaluation.
* Tests cover successful patch and disallowed file change.

## Implementation Notes

`src/aep/generate_patch.py` consumes exactly one evaluated
`IMPLEMENTATION_PLAN`, supplies it through the immutable `ContextPackage`, and
invokes the resolved Code Generator. Structured file writes are checked against
the plan before mutation and then executed through capability-authorized
Filesystem Tool invocations. Authorized Git diff and applicability-check
invocations produce immutable patch, changed-file, and evaluation evidence.

Patch Evaluation runs against a separate clean, revision-bound checkout so its
applicability check cannot be confused by the execution checkout's intended
uncommitted changes. The handler publishes the `PATCH` GeneratedArtifact only
after applicability and path-boundary checks pass; scheduling remains
responsible for the TaskExecution's terminal transition.
