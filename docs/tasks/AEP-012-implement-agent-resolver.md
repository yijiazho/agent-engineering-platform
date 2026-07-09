# AEP-012: Implement Agent Resolver

**Status:** Not Started

## Context

There is no standalone Agent Runtime service. The Agent Resolver loads Agent, Prompt, Model, Tools, and Policies and produces ResolvedAgent.

## Deliverable

Implement Agent Resolver.

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
