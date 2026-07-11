# AEP-021: Implement Filesystem Tool

**Status:** Not Started

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
