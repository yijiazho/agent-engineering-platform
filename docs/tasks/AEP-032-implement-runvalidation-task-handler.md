# AEP-032: Implement RunValidation Task Handler

**Status:** Not Started

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
