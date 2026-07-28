# AEP-018: Implement GeneratedArtifact Store

**Status:** Completed

## Context

`GeneratedArtifact` is immutable runtime evidence produced by a TaskExecution, not declarative configuration. Artifact metadata participates in the runtime object model, while potentially large content such as plans, patches, reports, and model outputs belongs behind a content-addressed storage boundary.

The store must support later ContextPackages and evaluations without allowing published evidence to be overwritten. The MVP may use local or in-memory storage, but the interface must remain compatible with durable object storage.

## Deliverable

Implement GeneratedArtifact storage that:

* separates runtime metadata from content-addressed bytes or structured content;
* records artifact type, producer, repository revision, content digest, provenance, and trace data;
* provides immutable create and lookup operations, including lookup by TaskExecution;
* handles duplicate content without duplicating mutable evidence; and
* includes an in-memory/local adapter plus tests for integrity, retrieval, deduplication, and mutation rejection.

## Dependencies

* AEP-002
* AEP-004

## Acceptance Criteria

* Store writes immutable artifact content by content address.
* Metadata records producer TaskExecution, artifact type, repository revision, and provenance.
* Store rejects mutation after publication.
* Context Builder can retrieve previous GeneratedArtifacts by TaskExecution.
* Tests cover duplicate content, metadata lookup, and immutability.

## Implementation

`src/aep/generated_artifact_store.py` defines separate provider-neutral
interfaces for immutable content-addressed bytes and GeneratedArtifact metadata.
The thread-safe in-memory adapters canonicalize structured JSON, deduplicate
content by SHA-256 digest, validate completed metadata against the authoritative
GeneratedArtifact runtime schema, verify content integrity on write and read,
publish metadata through the runtime-object store, and query its persistent
producer-TaskExecution index. Republishing identical evidence is idempotent;
changing content or metadata under a published artifact identifier is rejected
before content storage is mutated. A persistent atomic publication claim
coordinates adapter instances so concurrent conflicting publishers cannot leave
unreferenced content.
