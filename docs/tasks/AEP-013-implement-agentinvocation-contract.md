# AEP-013: Implement AgentInvocation Contract

**Status:** Not Started

## Context

AgentInvocation binds ResolvedAgent to ContextPackage and records model-backed cognitive work.

## Deliverable

Implement AgentInvocation orchestration with a mock model provider.

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
