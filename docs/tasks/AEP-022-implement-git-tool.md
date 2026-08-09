# AEP-022: Implement Git Tool

**Status:** Completed

## Context

The issue-to-PR workflow needs deterministic Git operations against one execution-specific working branch. Git must be exposed as a non-model Tool so branch creation, diff inspection, status, and push are capability-controlled and fully recorded rather than issued directly by an Agent.

Local read operations and externally mutating push operations have different risk. The adapter must produce structured repository evidence, preserve workspace boundaries, avoid leaking credentials, and leave PR creation to the GitHub Tool.

## Deliverable

Implement a Git Tool adapter that:

* defines structured operations for create branch, status, diff, publication commit, and push branch;
* runs only against the configured repository and expected revision/working branch;
* requires `git.push` authorization before remote mutation;
* returns changed-file, diff, branch, revision, and command-result metadata with redacted logs; and
* tests operations in a local fixture repository, including invalid state, denied push, and command failure.

## Dependencies

* AEP-019
* AEP-020

## Acceptance Criteria

* Supports create branch, diff, status, publication commit, and push branch operations.
* Push requires Pre-Execution Capability Policy.
* Outputs structured changed file and diff metadata.
* ToolInvocation records command logs without leaking secrets.
* Tests use a local fixture repository.

## Implementation

`src/aep/git_tool.py` defines the Git Tool input and output schemas and a
repository-bound `GitToolAdapter`. Every request names the configured immutable
base revision and execution-specific working branch. Branch creation starts at
that revision and requires a clean index and worktree. Status, diff, and push
require both the configured branch and the base revision as an ancestor of
`HEAD`. Push targets only the configured remote and branch and refuses to start
unless the request declares `git.push`, allowing the shared Tool Runtime
authorization boundary to deny the external mutation before adapter startup.

Git commands execute only through an injected `GitSandbox` supplied by the Tool
Runtime. That boundary mounts only the configured repository, receives a
minimal explicit environment instead of inheriting the host environment,
terminates commands at their deadline, and supplies hook and null-device paths
outside the repository mount. The adapter disables repository hooks for every
command. A `GitCredentialProvider` leases scoped environment entries only for
the configured push attempt and revokes them in a `finally` block. Credential
acquisition receives only the remaining Tool deadline; an acquisition timeout
returns `TIMED_OUT` with `NOT_ATTEMPTED` mutation state before Git push begins.

The read-only `check_patch` operation supports Patch Evaluation by streaming
patch content to isolated `git apply --numstat` and `git apply --check --cached`
commands. It requires a clean branch whose HEAD exactly matches the configured
revision, reports changed paths and applicability diagnostics, and does not
modify the index or worktree.

The `commit_changes` operation materializes the already accepted working-tree
patch as a new immutable head before publication. It requires the same
`git.push` publication capability, stages repository-confined changes, uses a
controlled AEP author identity with hooks and signing disabled, rejects an
empty worktree, verifies the current diff against the accepted patch SHA-256,
and records both the immutable base and new head revisions. The controlled
commit includes that digest as an AEP trailer, allowing a later scheduler
attempt to reconcile the same clean head without creating another commit. The
retry reconstructs and hashes the actual binary `base..HEAD` patch as the
authority; the trailer is supplemental provenance and cannot substitute a
different committed tree.

Successful results include repository, branch, base and current revisions,
porcelain-derived changed-file records, diff content and digest metadata when
requested, and per-command exit and byte-count metadata. Full stdout and stderr
are redacted before being persisted through a command-log store, and the
returned `logsRef` identifies that immutable evidence without exposing
credentials in structured output.
Sandbox timeouts retain redacted partial command logs and return a `TIMED_OUT`
result with `logsRef` after the isolated command has been terminated.

`GitTool` composes the adapter with runtime persistence. It atomically creates
a pending ToolInvocation before execution and binds the identity to a canonical
request fingerprint. Matching retries and concurrent duplicates reuse the
terminal result, while a conflicting reuse is rejected. The fingerprint binds
the full trace, WorkflowExecution, and TaskExecution correlation, and a caller
whose supplied task identity conflicts with the request is rejected before the
claim. Adapter-native log
references remain available for replay; content-addressed references also use
the runtime schema's `logsAddress` field.

Every adapter result reports `remoteMutationState`. It is `NOT_ATTEMPTED`
before a push starts, becomes `UNKNOWN` immediately before the push command,
and becomes `CONFIRMED` as soon as that command succeeds. A push timeout or
failure remains `UNKNOWN` until a future reconciliation contract can establish
the remote state. Once `CONFIRMED`, later local evidence-collection failures do
not erase the observed external effect.

`tests/test_git_tool.py` creates temporary local working and bare fixture
repositories. It covers branch creation, status, diff, publication commit with
remote content verification, denied and authorized push, repository-state
mismatches, omitted push capability, command failure,
dirty and unrelated histories, hook and ambient-environment isolation, scoped
credential cleanup, read and push timeouts, structured evidence, and
command-log secret redaction without network access. Push tests distinguish an
ambiguous in-flight timeout from a confirmed push followed by an evidence
timeout. Persistence tests cover matching replay, conflicting identities, and
concurrent duplicate execution.
