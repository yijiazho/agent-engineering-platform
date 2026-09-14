# AEP-069: Build Revision-Bound Semantic Repository Index

**Status:** Not Started

## Context

AEP currently converts issue and Task language into bounded repository candidates
primarily through deterministic lexical matching over the repository-knowledge
snapshot. That preserves revision binding, provenance, and control-plane
ownership, but it has weak recall when issue vocabulary differs from source
paths, identifiers, comments, or documentation. An issue such as "login state
disappears after reconnecting" may need evidence from `session`, `persistence`,
or `state_cache` code without sharing those exact terms.

Semantic retrieval can improve this first discovery step, but it must not become
a second source of truth or grant an Agent repository-browsing authority.
Vector similarity establishes candidate relevance only. Exact planning evidence,
editable-target binding, Evaluation, and Policy remain authoritative for
deciding what must change and what may be published.

The index must also preserve AEP's immutable-revision model. A WorkflowExecution
bound to revision A cannot silently query embeddings built from revision B.
Embedding computation may be reused across revisions when identical content has
the same content digest, but every query result must be attributable to the
requested revision, index configuration, embedding configuration, and source
content.

AEP should begin with a small local provider-neutral index suitable for one
repository. A separately operated vector-database service is not required for
this task. The storage and query contracts must allow a future implementation
to replace the local backend without changing Context Builder or Task
semantics. Embeddings are selected through an exact versioned `Model` Resource;
the index does not introduce an unversioned provider configuration outside the
immutable Resource graph.

## Deliverable

Add a provider-neutral semantic repository index behind the existing repository
knowledge boundary.

The implementation must:

* define canonical semantic-index records for repository files or
  structure-aware chunks, including path, source range or structural identity,
  content digest, repository revision/snapshot membership, embedding `Model`
  Resource reference, vector dimensions, similarity metric, and vector
  normalization rule;
* define an embedding-provider boundary separate from ModelInvocation and Agent
  execution. It must consume the exact versioned embedding `Model` Resource,
  including its provider, model, timeout, retry, and rate-limit configuration,
  with bounded batch behavior and no runtime credentials persisted in index
  evidence;
* define durable `SemanticIndexBuild` and `EmbeddingInvocation` runtime records
  and inspection paths. Each embedding attempt, including one that fails before
  an index record is produced, must retain its owning build, requested
  revision/snapshot, resolved Model Resource, timing, classified outcome, and
  safe usage metadata without source bodies or credentials;
* provide a small persistent local index suitable for the self-hosting
  repository without requiring a network vector-database service;
* stage all membership changes for a repository revision and atomically publish
  a complete index generation only after it is complete. Queries must reject an
  absent, incomplete, or superseded generation rather than return a partial
  candidate set labeled as the requested revision;
* use content-addressed embedding reuse so unchanged content does not require a
  new embedding merely because a new Git revision references it;
* exclude unsupported, binary, generated, vendored, secret-bearing, and
  explicitly ignored content according to deterministic repository-scanner
  rules;
* use deterministic structure-aware chunk boundaries where supported and a
  documented bounded fallback for small or unsupported text files;
* expose semantic nearest-neighbor query primitives through the repository
  knowledge API with explicit `topK`, score threshold, result-size, and token or
  byte bounds. Query semantics and provenance must include the configured
  similarity metric and normalization rule;
* return candidate evidence with similarity score, path, source range or chunk
  identity, content digest, revision, knowledge snapshot, embedding
  configuration, and selection reason;
* persist enough query provenance to explain which semantic index and
  configuration produced a candidate set without persisting unrestricted source
  bodies in ordinary logs; and
* keep semantic results advisory: they may nominate candidate evidence but may
  not directly create planning facts, editable targets, write authority,
  EvaluationResult, or PolicyDecision evidence.

## Dependencies

* AEP-003
* AEP-004
* AEP-015
* AEP-016
* AEP-017
* AEP-018
* AEP-039
* AEP-045

## Acceptance Criteria

* A repository revision can be indexed and queried locally without deploying a
  separate vector-database service.
* Every semantic record is bound to immutable source content and can be mapped
  back to an exact repository revision, path, and bounded source region.
* Unchanged content referenced by two revisions reuses the same canonical
  embedding record while each revision retains correct path/snapshot
  membership.
* Changed, renamed, deleted, and newly added files produce correct revision
  membership without leaking candidates from another revision.
* The embedding-provider contract records provider/model/configuration identity,
  exact Model Resource reference, vector dimensions, similarity metric,
  normalization rule, timing/failure evidence, and safe usage metadata while
  excluding runtime credentials and unrestricted repository content from
  ordinary logs.
* `SemanticIndexBuild` and `EmbeddingInvocation` schemas persist and expose
  success and failure evidence independently of AgentInvocation and
  ModelInvocation; a failed request before index-record creation remains
  inspectable with a stable classification.
* A failed or interrupted build cannot publish partial membership for a
  revision. Crash/retry and concurrent-query tests prove that queries see either
  the previously complete generation or the newly complete generation, never a
  partial one.
* The local backend supports deterministic bounded top-K semantic queries and
  stable tie ordering for a fixed stored index and query vector.
* Structure-aware chunking is implemented for the repository's primary source
  and documentation formats, with deterministic whole-file or bounded text
  fallback where structural parsing is unavailable.
* Binary, generated, vendored, ignored, oversized, and configured sensitive
  paths are excluded deterministically and covered by tests.
* A fixture whose issue language has little or no exact lexical overlap with the
  relevant implementation still retrieves the expected file or chunk within a
  configured top-K result set.
* Query results expose similarity as candidate-ranking evidence only; no
  semantic score is treated as proof that a file requires modification.
* Index/query failures have stable classifications and cannot cause Context
  Builder to silently substitute evidence from another revision or an
  incompatible embedding configuration.
* Unit and integration tests cover content-addressed reuse, revision isolation,
  atomic generation publication, crash/retry and concurrent query behavior,
  chunk identity stability, deterministic ordering, exclusion rules, provider
  failure, durable invocation evidence, bounded retrieval, and provenance
  inspection.
* Repository-intelligence, Context Builder, ADR, execution-plan, and operator
  documentation describe the semantic index as a platform-owned candidate
  retrieval signal and preserve the rule that Agents do not query it directly.
