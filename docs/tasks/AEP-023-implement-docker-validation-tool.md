# AEP-023: Implement Docker Validation Tool

**Status:** Completed

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

## Implementation

`src/aep/docker_validation_tool.py` implements the Docker validation adapter
and a production-capable Docker CLI executor over injectable process and log
storage boundaries. Its JSON Schema contract requires a digest-pinned image,
ordered command arguments, workspace bind mount, CPU and memory limits, while
the shared `ToolRequest` supplies the invocation timeout. The mount source must
resolve within the adapter's canonical authorized workspace root, including
after symlink resolution, and the container destination is fixed at
`/workspace`. The
adapter refuses to start without the `docker.run` capability, so the shared
Tool Runtime authorization hook must authorize that capability before the
executor is called.

The executor lifecycle remains under Tool Runtime control for waiting,
termination, forced termination, and cleanup. Successful and nonzero command
results retain stdout, stderr, exit code, duration, and logs reference for every
configured command. The invocation-scoped container disables networking,
executes commands from `/workspace`, and uses one absolute deadline across
create, start, and command execution. When a later command times out, completed
command evidence and its immutable logs reference remain on the normalized
timed-out result while the runtime terminates and cleans up the container.
Startup, timeout, and nonzero-exit failures are classified separately. The
adapter records execution evidence only; AEP-027 remains responsible for
interpreting whether that evidence satisfies build and test expectations.

`tests/test_docker_validation_tool.py` uses an injected fake executor and a
deterministic request fixture. It covers successful evidence capture, nonzero
exit, timeout termination and cleanup, policy denial before startup, startup
failure, the mandatory capability, and invalid input boundaries without
requiring a Docker daemon. Docker CLI construction, invocation-scoped
containers, mount and resource arguments, evidence, and cleanup are tested
through a fake process boundary.
