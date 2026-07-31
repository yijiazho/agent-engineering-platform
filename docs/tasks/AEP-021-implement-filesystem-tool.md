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
missing parents, and paths whose resolved symlink target leaves that workspace.
Writes require the request to declare `filesystem.write`; the shared
Pre-Execution Capability Policy authorization hook runs before adapter startup
and therefore before mutation.

The adapter returns normalized relative paths, byte counts, and SHA-256 content
digests. It stores content-addressed structured logs without logging file
contents. Schema, boundary, missing-file, and I/O failures remain distinct in
Tool results and map to the platform runtime failure classes in persisted,
terminal `ToolInvocation` evidence.

The public JSON Schemas are
`schemas/tools/v1/filesystem-input.schema.json` and
`schemas/tools/v1/filesystem-output.schema.json`. Focused tests cover reads,
writes, authorization allow and denial, traversal and symlink escape, malformed
input, missing files, invalid UTF-8 I/O, capability mismatch, logs, output
metadata, persistence, and runtime-schema compatibility.
