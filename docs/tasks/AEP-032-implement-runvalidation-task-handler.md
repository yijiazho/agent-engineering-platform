# AEP-032: Implement RunValidation Task Handler

**Status:** Completed

## Context

`RunValidation` performs deterministic build and test commands against the generated working tree. It is intentionally non-cognitive: execution occurs through the Docker Tool, while technical interpretation occurs through Build and Test Evaluations.

The handler must preserve command evidence even on failure, distinguish Tool failure from evaluation failure, and stop downstream publication when required validation is incomplete or unsuccessful.

## Deliverable

Implement the `RunValidation` Task handler that:

* resolves configured image, commands, mount, limits, and timeout from Task context;
* authorizes and invokes the Docker validation Tool;
* persists a validation-report GeneratedArtifact and complete ToolInvocation evidence;
* creates build and test EvaluationResults and updates TaskExecution state; and
* tests passing commands, build/test failure, timeout, denial, and malformed output.

## Dependencies

* AEP-023
* AEP-027
* AEP-031

## Acceptance Criteria

* Handler invokes Docker validation Tool.
* Handler persists validation report GeneratedArtifact.
* Handler creates build and test EvaluationResults.
* Handler classifies tool failures.
* Tests cover pass, fail, and timeout.

## Implementation

`src/aep/run_validation.py` implements the non-cognitive Task handler. A
`RunValidation` Task declares `spec.validation` with one explicit Docker Tool
version, a digest-pinned image, exactly one labeled build command and one
labeled test command, the fixed container mount, resource limits, and a
timeout. The execution-specific host checkout remains runtime state supplied
by `TaskExecution.workspacePath`; it is not embedded in the versioned Task.

The handler requires one successful `GeneratePatch` dependency with one
revision-matched, evaluated `PATCH` artifact. It invokes the policy-gated
Docker Tool as the TaskExecution, attaches the terminal ToolInvocation, and
always derives separate build and test EvaluationResults from the captured
evidence. It then publishes an `EVALUATION_REPORT` GeneratedArtifact that
summarizes both evaluations and any Tool failure while retaining the patch as
input provenance. Denial, timeout, startup, adapter, malformed-output, and
nonzero-exit outcomes remain distinct failure classes; technical build and
test failures block downstream work as Evaluation failures.

`DockerValidationTool` adds deterministic request fingerprinting, atomic
invocation identity claims, immutable terminal evidence, and replay reuse to
the Docker adapter. Focused tests use an injected executor and cover passing
commands, build and test failure, timeout, policy denial, malformed executor
evidence, retry reuse, and the new Resource fixture without requiring Docker.
The retry cases include a concurrent validation lasting longer than the former
fixed replay wait and recovery of an abandoned invocation owner. Negative tests
also reject duplicate/extra command labels and PATCH evaluations whose target
or workflow provenance does not match the consumed artifact, plus dependency
TaskExecutions and PATCH records whose workflow, trace, or producer identity is
inconsistent.
