# AEP-014: Implement ModelInvocation Adapter Interface

**Status:** Not Started

## Context

Model providers are accessed through Model Resources during AgentInvocation and are not part of the Tool Platform. A provider-neutral boundary keeps vendor SDKs and network behavior outside the orchestration contract.

The adapter consumes assembled model input and immutable Model configuration, then normalizes output, usage, latency, metadata, and errors for persistence and retry decisions.

## Deliverable

Define a ModelInvocation adapter package that:

* specifies provider-neutral request and response types;
* returns structured output, usage, latency, and provider metadata;
* classifies errors as recoverable or permanent;
* supplies data needed for the ModelInvocation runtime record; and
* includes a deterministic configurable fake and extension guidance for real providers.

## Dependencies

* AEP-001
* AEP-002

## Acceptance Criteria

* Adapter accepts Model configuration and assembled model input.
* Adapter returns structured output, usage, latency, and provider metadata.
* ModelInvocation records modelRef and execution metadata.
* Adapter errors are classified as recoverable or permanent.
* Tests do not require network access.
