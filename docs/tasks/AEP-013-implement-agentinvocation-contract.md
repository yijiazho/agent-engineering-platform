# AEP-013: Implement AgentInvocation Contract

**Status:** Completed

## Context

`AgentInvocation` is the runtime boundary for one bounded unit of model-backed reasoning. It combines an immutable `ResolvedAgent` and `ContextPackage`, assembles model input, records ModelInvocations, and validates structured output.

Agents cannot bypass deterministic context construction. Repository knowledge retrieval is prohibited during invocation, and any allowed non-knowledge Tool remains constrained by the ResolvedAgent and policy.

## Deliverable

Implement the AgentInvocation coordinator and contract that:

* persists invocation identity, inputs, status, and trace fields;
* assembles deterministic model input and calls the Model adapter;
* records usage, latency, cost, provider metadata, and structured output;
* validates output schema and blocks direct knowledge retrieval; and
* tests success, provider failure, and invalid output with a deterministic fake.

## Dependencies

* AEP-012
* AEP-014
* AEP-017

## Acceptance Criteria

* AgentInvocation records resolvedAgentId and contextPackageId.
* AgentInvocation creates ModelInvocation records.
* AgentInvocation validates structured output schema.
* AgentInvocation records token and cost metadata when provided.
* AgentInvocation cannot retrieve repository knowledge directly.
* Tests use a deterministic fake model provider.

## Implementation Notes

`src/aep/agent_invocation.py` implements the provider-neutral coordinator. It
validates that the immutable `ResolvedAgent`, `ContextPackage`, Prompt, and
Model configuration share the expected identities before persisting a running
`AgentInvocation` and `ModelInvocation`. Model input is assembled solely from
the resolved Prompt, output schema, and supplied ContextPackage; the boundary
has no repository-knowledge query dependency.

Successful provider evidence includes content addresses, token usage, latency,
cost, and provider metadata. Structured-output validation is deterministic:
invalid output leaves the successful provider call recorded while failing the
owning AgentInvocation with an `EVALUATION` failure. Normalized provider errors
fail both runtime records with their retry classification. Focused tests cover
success, provider failure, schema-invalid and non-JSON output, immutable context
and correlation continuity, concurrency-safe paired identity claims and
collision handling, and lifecycle telemetry with the deterministic fake
adapter from AEP-014.
