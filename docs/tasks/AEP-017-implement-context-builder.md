# AEP-017: Implement Context Builder

**Status:** Not Started

## Context

The Context Builder constructs deterministic ContextPackages. Agents never retrieve repository knowledge directly.

## Deliverable

Implement ContextPackage construction for MVP Tasks.

## Dependencies

* AEP-003
* AEP-004
* AEP-016
* AEP-018

## Acceptance Criteria

* ContextPackage includes Task, Event, repository, knowledge, policy, and prior GeneratedArtifact context as applicable.
* Every context element includes provenance.
* Required context constraints are validated.
* Token budget metadata is recorded, even if approximate.
* Context construction is deterministic for identical inputs.
* Tests cover each MVP Task type.
