# AEP-022: Implement Git Tool

**Status:** Completed

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

## Implementation

`src/aep/git_tool.py` defines the Git Tool input and output schemas and a
repository-bound `GitToolAdapter`. Every request names the configured immutable
base revision and execution-specific working branch. Branch creation starts at
that revision; status and diff require the configured branch; push targets only
the configured remote and branch and refuses to start unless the request
declares `git.push`, allowing the shared Tool Runtime authorization boundary to
deny the external mutation before adapter startup.

Successful results include repository, branch, base and current revisions,
porcelain-derived changed-file records, diff content and digest metadata when
requested, and per-command exit and byte-count metadata. Full stdout and stderr
are redacted before being persisted through a command-log store, and the
returned `logsRef` identifies that immutable evidence without exposing
credentials in structured output.

`tests/test_git_tool.py` creates temporary local working and bare fixture
repositories. It covers branch creation, status, diff, denied and authorized
push, repository-state mismatches, omitted push capability, command failure,
structured evidence, and command-log secret redaction without network access.
