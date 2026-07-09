# AEP-018: Implement GeneratedArtifact Store

**Status:** Not Started

## Context

GeneratedArtifact is a runtime object and durable output, not a Resource.

## Deliverable

Implement GeneratedArtifact metadata and content-addressed storage interface.

## Dependencies

* AEP-002
* AEP-004

## Acceptance Criteria

* Store writes immutable artifact content by content address.
* Metadata records producer TaskExecution, artifact type, repository revision, and provenance.
* Store rejects mutation after publication.
* Context Builder can retrieve previous GeneratedArtifacts by TaskExecution.
* Tests cover duplicate content, metadata lookup, and immutability.
