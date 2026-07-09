# AEP-019: Define Tool Runtime Contract

**Status:** Not Started

## Context

Tools are non-model external capabilities executed through Tool Runtime.

## Deliverable

Define ToolInvocation request and response contract.

## Dependencies

* AEP-001
* AEP-002

## Acceptance Criteria

* Contract includes toolRef, input, caller, capabilities, timeout, and traceId.
* Response includes status, output, logs reference, metrics, and failure class.
* Contract excludes model provider calls.
* Schema validation is part of the contract.
* Fixtures cover success, validation failure, policy denial, and timeout.
