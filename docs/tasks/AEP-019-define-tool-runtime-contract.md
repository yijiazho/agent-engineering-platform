# AEP-019: Define Tool Runtime Contract

**Status:** Completed

## Context

A Tool Resource describes a reusable non-model capability; a `ToolInvocation` records one controlled execution of that capability. The Tool Runtime contract is the common boundary through which schema validation, capability policy, sandboxing, execution, evidence collection, and failure classification occur.

The contract must not contain provider-specific business logic or Model calls. It should support Filesystem, Git, Docker, and GitHub adapters uniformly while preserving traceability and least-privilege capability declarations.

## Deliverable

Define the Tool Runtime contract package with:

* typed invocation requests containing tool reference, caller, input, capabilities, timeout, and trace context;
* typed results containing status, structured output, logs reference, metrics, timing, and failure class;
* input/output schema-validation hooks and lifecycle states;
* explicit exclusion of Model provider invocations; and
* fixtures and tests for success, invalid input, policy denial, timeout, and adapter failure.

## Dependencies

* AEP-001
* AEP-002

## Acceptance Criteria

* Contract includes toolRef, input, caller, capabilities, timeout, and traceId.
* Response includes status, output, logs reference, metrics, and failure class.
* Contract excludes model provider calls.
* Schema validation is part of the contract.
* Fixtures cover success, validation failure, policy denial, and timeout.

## Implementation

`src/aep/tool_runtime.py` defines immutable Tool request, caller, result, metrics,
lifecycle, status, and failure-class types. Requests accept only versioned `Tool`
references, explicitly excluding Model provider invocation. `ToolAdapter` is the
uniform extension boundary for Filesystem, Git, Docker, and GitHub implementations.

`ToolSchemaValidator` provides input/output validation hooks, with a Draft 2020-12
JSON Schema implementation. `invoke_tool` validates input before authorization,
avoids adapter execution when policy denies a request, and validates successful
structured output. It enforces the request deadline and converts validation,
timeout, and adapter exceptions into typed failure results. Contract evidence is
recursively immutable, and Tool references require immutable semantic versions.
Deterministic fixtures and tests cover success, invalid input and output, policy
denial, timeout, and adapter failure without network access.
