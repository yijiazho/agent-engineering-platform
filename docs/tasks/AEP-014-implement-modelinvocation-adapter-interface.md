# AEP-014: Implement ModelInvocation Adapter Interface

**Status:** Not Started

## Context

Model providers are represented by Model Resources and are not Tools.

## Deliverable

Define a model provider adapter interface and fake implementation.

## Dependencies

* AEP-001
* AEP-002

## Acceptance Criteria

* Adapter accepts Model configuration and assembled model input.
* Adapter returns structured output, usage, latency, and provider metadata.
* ModelInvocation records modelRef and execution metadata.
* Adapter errors are classified as recoverable or permanent.
* Tests do not require network access.
