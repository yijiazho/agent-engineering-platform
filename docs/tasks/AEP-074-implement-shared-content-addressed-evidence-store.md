# AEP-074: Implement Shared Content-Addressed Evidence Store

**Status:** Not Started

## Context

GeneratedArtifact bodies currently live in a filesystem content-addressed store
on the worker host. A replacement worker cannot rely on another node's
filesystem, and a host loss can make published artifact metadata point at
missing bytes.

ADR-006 and ADR-008 require shared, immutable, content-addressed evidence in
Cloudflare R2 or another S3-compatible store. Metadata and provenance stay in
PostgreSQL; bytes live in the shared store.

## Deliverable

Add an S3-compatible `ContentAddressedStore` adapter for R2, integrate it with
the artifact catalog and content reservations, and make artifact, tool-log, and
evidence bytes readable by any authorized worker.

## Dependencies

* AEP-018
* AEP-073

## Acceptance Criteria

* An S3-compatible adapter implements `ContentAddressedStore` with immutable
  put/get by SHA-256 content address.
* `put` is idempotent for identical bytes; a conflicting digest is a terminal
  integrity failure.
* Bytes are committed before metadata. A failed write leaves no metadata record
  pointing at missing content.
* Every put, including reuse of an existing digest, reserves the digest in the
  transactional catalog before writing or returning bytes.
* Collection claims a digest only after checking that no metadata reference and
  no active reservation exists; a concurrent put and collection cannot delete
  referenced bytes.
* Deletion tombstones make late reads fail closed and allow a later identical
  put to recreate the object safely.
* Object keys are scoped by deployment and registration. Cross-registration
  reads are denied by default.
* GeneratedArtifacts, ToolInvocation logs, ModelInvocation evidence, validation
  reports, and compressed logs use the shared evidence boundary where their
  contracts expose content references.
* R2 bucket, prefix, lifecycle, versioning, token, and object-lock guidance is
  documented. Tokens are bucket-scoped; prefix isolation is enforced by the
  application.
* Integration tests run against a local S3-compatible fixture and cover
  idempotency, corruption, missing content, reservation/collection races,
  retries, and cross-registration access.
* The filesystem adapter remains the local/test default. No artifact metadata
  is written without its bytes, and no artifact body is written to lifecycle
  logs.

## Out Of Scope

* The transactional runtime store (AEP-073).
* The publication service and worktree reconstruction (AEP-076).
* Cross-deployment artifact import.

## Design References

* [ADR-006](../adr/ADR-006-distributed-execution-and-shared-artifacts.md)
* [ADR-008](../adr/ADR-008-hetzner-postgresql-r2-small-scale.md)
* [Small-Scale Deployment](../architecture/small-scale-deployment.md)
