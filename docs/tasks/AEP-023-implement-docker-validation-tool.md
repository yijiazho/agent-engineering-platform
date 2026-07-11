# AEP-023: Implement Docker Validation Tool

**Status:** Not Started

## Context

`RunValidation` executes deterministic build and test commands in an isolated environment. Docker is a privileged non-model capability and must be invoked through the Tool Runtime with explicit image, workspace mount, command, limits, policy, and timeout information.

The adapter captures execution evidence but does not decide whether a build or test satisfies acceptance criteria; AEP-027 converts its results into EvaluationResults. Tests should not require a production Docker environment.

## Deliverable

Implement a Docker validation Tool adapter that:

* accepts validated image, command sequence, workspace mount, timeout, and resource settings;
* requires `docker.run` authorization before starting execution;
* captures stdout, stderr, exit code, duration, and logs reference per command;
* classifies startup, timeout, and nonzero-exit failures and performs cleanup; and
* provides an injectable executor with fake or lightweight tests for pass, failure, timeout, and denial.

## Dependencies

* AEP-019
* AEP-020

## Acceptance Criteria

* Accepts image, commands, timeout, and workspace mount configuration.
* Requires Pre-Execution Capability Policy for docker.run.
* Captures stdout, stderr, exit code, and duration.
* Classifies timeout and nonzero exit failures.
* Tests use a fake executor or lightweight local fixture.
