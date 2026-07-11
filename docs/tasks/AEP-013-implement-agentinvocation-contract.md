# AEP-013: Implement AgentInvocation Contract

**Status:** Not Started

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
