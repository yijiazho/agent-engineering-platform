# AEP-022: Implement Git Tool

**Status:** Not Started

## Context

The issue-to-PR workflow needs deterministic Git operations against one execution-specific working branch. Git must be exposed as a non-model Tool so branch creation, diff inspection, status, and push are capability-controlled and fully recorded rather than issued directly by an Agent.

Local read operations and externally mutating push operations have different risk. The adapter must produce structured repository evidence, preserve workspace boundaries, avoid leaking credentials, and leave PR creation to the GitHub Tool.

## Deliverable

Implement a Git Tool adapter that:

* defines structured operations for create branch, status, diff, and push branch;
* runs only against the configured repository and expected revision/working branch;
* requires `git.push` authorization before remote mutation;
* returns changed-file, diff, branch, revision, and command-result metadata with redacted logs; and
* tests operations in a local fixture repository, including invalid state, denied push, and command failure.

## Dependencies

* AEP-019
* AEP-020

## Acceptance Criteria

* Supports create branch, diff, status, and push branch operations.
* Push requires Pre-Execution Capability Policy.
* Outputs structured changed file and diff metadata.
* ToolInvocation records command logs without leaking secrets.
* Tests use a local fixture repository.
