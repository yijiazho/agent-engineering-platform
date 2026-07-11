# AEP-012: Implement Agent Resolver

**Status:** Not Started

## Context

An Agent Resource is declarative, not invocation-ready. Before reasoning begins, the Agent Resolver binds the Agent to exact Prompt, Model, non-model Tool, and Policy versions so an invocation cannot observe floating configuration.

The resulting `ResolvedAgent` is immutable and scoped to execution. The resolver owns no durable state, invokes no model, and preserves the distinction between Model providers and Tools.

## Deliverable

Implement a stateless Agent resolver that:

* accepts Task and Agent references;
* resolves explicit Agent, Prompt, Model, Tool, and Policy versions;
* validates resource kinds, tool allowlists, and policy constraints;
* returns an immutable `ResolvedAgent` with model parameters and output contract; and
* tests successful, missing, floating, wrong-kind, and denied references.

## Dependencies

* AEP-003
* AEP-011

## Acceptance Criteria

* Resolver accepts Task and Agent references.
* Resolver loads explicit Agent, Prompt, Model, Tool, and Policy versions.
* Resolver produces immutable ResolvedAgent.
* Resolver rejects missing or floating references.
* Resolver rejects model providers listed as Tools.
* Tests cover valid resolution, missing prompt, missing model, and denied tool reference.
