# AEP-021: Implement Filesystem Tool

**Status:** Completed

## Context

Patch generation requires controlled access to a WorkflowExecution's working checkout. Direct host filesystem access would bypass Tool schemas, policy, audit, and workspace isolation, so reads and writes must pass through the Tool Runtime boundary.

The MVP adapter must resolve paths relative to an explicitly configured workspace, reject traversal and symlink escapes, and require capability authorization for mutation. It does not provide repository knowledge retrieval to Agents.

## Deliverable

Implement a Filesystem Tool adapter that:

* defines structured read and write inputs and outputs;
* confines all resolved paths to the configured workspace boundary;
* evaluates `filesystem.write` before mutation and records ToolInvocation evidence;
* classifies schema, boundary, missing-file, and I/O failures; and
* tests allowed operations, traversal and escape denial, invalid input, logs, and output metadata.

## Dependencies

* AEP-019
* AEP-020

## Acceptance Criteria

* Supports read and write operations declared by schema.
* Enforces workspace path boundaries.
* Requires Pre-Execution Capability Policy for write operations.
* Records ToolInvocation logs and outputs.
* Tests cover allowed read, allowed write, path traversal denial, and schema failure.

## Implementation

`src/aep/filesystem_tool.py` implements UTF-8 `read` and `write` operations
through the provider-neutral Tool Runtime. The adapter is configured with one
existing workspace directory. It rejects absolute paths, traversal components,
missing parents, and paths whose opened file handle leaves that workspace.
POSIX adapters walk from a pinned workspace directory descriptor with
no-follow opens. Windows adapters pin and verify the workspace and each parent
directory handle, reject reparse points, and create or open the final child
relative to the pinned parent through the native API. The kernel-resolved final
handle is verified before reading, truncating, or writing. This closes the
validation/open symlink replacement window, including races that replace an
intermediate directory before a new file is created.
Writes require the request to declare `filesystem.write`; the shared
Pre-Execution Capability Policy authorization hook runs before adapter startup
and therefore before mutation.

Repository reads reject `AgentInvocation` callers even when they hold
`filesystem.read`; only the explicit `ContextBuilder`, `TaskExecution`, and
`WorkflowRuntime` control-plane caller contracts may read. Agents continue to
receive repository knowledge only through immutable ContextPackages.

The adapter returns normalized relative paths, byte counts, and SHA-256 content
digests. It stores content-addressed structured logs without logging file
contents. Schema, boundary, missing-file, and I/O failures remain distinct in
Tool results and map to the platform runtime failure classes in persisted,
terminal `ToolInvocation` evidence.

Before any effect, one atomic persistence operation creates pending evidence
that binds the invocation id to a SHA-256 fingerprint of all immutable request
inputs and an ownership token. Identical retries and concurrent duplicates
return the prior terminal result without repeating file effects; reuse with
different inputs is rejected. If that atomic create fails, it leaves neither a
claim nor evidence, so a retry can safely acquire ownership without duplicating
an effect.

The public JSON Schemas are
`schemas/tools/v1/filesystem-input.schema.json` and
`schemas/tools/v1/filesystem-output.schema.json`. Focused tests cover reads,
writes, authorization allow and denial, trusted read callers, Agent read
denial, traversal and symlink escape, read/write replacement races, malformed
input, missing files, invalid UTF-8 I/O, capability mismatch, invocation retry,
concurrency and identity conflicts, logs, output metadata, persistence, and
runtime-schema compatibility. A raced intermediate link with a nonexistent
outside target proves that Windows-relative creation cannot escape, and an
injected atomic-create failure proves retry recovery before any effect.
