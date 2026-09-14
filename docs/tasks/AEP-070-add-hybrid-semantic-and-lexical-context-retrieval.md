# AEP-070: Add Hybrid Semantic And Lexical Context Retrieval

**Status:** Not Started

## Context

AEP-069 adds revision-bound semantic candidate retrieval, while the existing
repository knowledge layer already provides deterministic lexical candidate
selection. Neither signal is sufficient by itself for software repositories.

Lexical retrieval is strong for exact paths, identifiers, Resource names, issue
numbers, configuration keys, and error strings. Semantic retrieval is useful
when natural-language intent and implementation vocabulary differ. Replacing
lexical search with vectors would regress exact-identifier cases; simply
concatenating both result sets would waste ContextPackage budget and make
selection difficult to explain.

AEP therefore needs one deterministic hybrid candidate-selection contract.
Hybrid retrieval belongs before AnalyzeIssue or another explicit discovery
Task. It must improve candidate recall without collapsing the distinction among
candidate evidence, exact planning evidence, and editable targets.

Context Builder remains a deterministic materializer. Agents may reason over
the bounded candidates and later emit typed planning predicates or evidence
requests, but they do not issue arbitrary vector, lexical, or graph queries
against repository providers.

## Deliverable

Extend the repository knowledge query API and Context Builder with a
revision-bound hybrid retrieval mode that combines lexical and semantic
candidate rankings under explicit budgets.

The implementation must:

* execute lexical and semantic retrieval against the same repository revision
  and compatible knowledge snapshot;
* combine rankings with a deterministic, documented rank-fusion algorithm that
  does not depend on opaque model judgment;
* preserve the component lexical rank/score and semantic rank/score alongside
  the fused rank and selection reason;
* support explicit candidate-count, per-source, byte/token, and optional
  path/type bounds so retrieval cannot expand without configuration. These
  bounds, filters, fusion parameters, thresholds, and degraded-mode behavior
  must come from the exact versioned requesting `Task` Resource rather than
  mutable runtime configuration;
* deduplicate file/chunk candidates by canonical source identity while
  preserving all retrieval reasons that contributed to selection;
* give exact identifier/path matches an inspectable path through the lexical
  signal rather than allowing semantic similarity to hide or replace them;
* integrate hybrid candidate retrieval into AnalyzeIssue ContextPackage
  construction without changing the later exact planning-evidence and
  editable-target contracts;
* define deterministic degradation when the semantic index is unavailable,
  stale, incompatible, or disabled; any lexical-only fallback must be explicit
  in ContextPackage provenance rather than silent;
* own issue-query embedding attempts with a durable `SemanticQuery` runtime
  record (or equivalent execution-scoped owner), including successes and
  failures before retrieval, rather than attaching them to an index build;
* derive the semantic query from a versioned canonical field projection and
  normalization with a pre-provider byte/token ceiling, and persist its
  content digest and derivation version in provenance;
* validate the query vector and persist its immutable content-addressed identity
  for the logical query. Retries and deterministic context rebuilds must reuse
  that identity rather than re-embedding the same query;
* bind `SemanticQuery` to the selected index generation's exact versioned
  embedding `Model` Resource, provider parameters, dimensions, metric, and
  normalization configuration. Reject any mismatch before nearest-neighbor
  ranking;
* route every query-side embedding attempt through the shared AEP-046 model
  admission coordinator and reserve/debit the requesting Workflow/Task
  execution budgets under AEP-062, with durable consumption evidence;
* expose candidate retrieval provenance through AEP inspection, including query
  identity, revision, retrieval modes, configured bounds, component ranks, and
  fused rank without printing source bodies by default. Provenance must retain
  the resolved Task Resource reference and normalized retrieval policy; and
* add regression fixtures comparing lexical-only and hybrid candidate
  selection for exact-identifier and vocabulary-mismatch issues.

## Dependencies

* AEP-016
* AEP-017
* AEP-029
* AEP-045
* AEP-053
* AEP-054
* AEP-069
* AEP-046
* AEP-062

## Acceptance Criteria

* AnalyzeIssue can request hybrid candidate context through the existing
  repository-knowledge/Context Builder boundary; the Agent has no direct
  access to lexical, vector, or graph providers.
* Lexical and semantic queries are guaranteed to target the WorkflowExecution's
  exact repository revision, and mixed-revision results are rejected.
* Rank fusion is deterministic for fixed component result sets and has stable
  tie ordering independent of provider response ordering.
* Each selected candidate records whether it was selected lexically,
  semantically, or by both, plus component ranks/scores, fused rank, revision,
  content identity, and bounded source location.
* Duplicate hits for the same canonical chunk/file consume ContextPackage
  budget once while retaining all contributing retrieval reasons.
* Exact path, symbol-like identifier, Resource name, configuration key, and
  issue/task-number fixtures do not regress relative to lexical-only retrieval.
* At least one controlled vocabulary-mismatch fixture that lexical-only
  retrieval misses or ranks outside the configured candidate window is brought
  into that window by hybrid retrieval.
* Hybrid retrieval obeys configured candidate, byte/token, and source bounds
  before model invocation; truncation or exclusion is deterministic and
  inspectable.
* Candidate bounds, path/type filters, rank-fusion parameters, score thresholds,
  and degraded-mode behavior are resolved from the immutable requesting Task
  version. Query provenance records that Task reference and the normalized
  policy, so identical execution inputs cannot silently use a different
  retrieval configuration.
* A missing, stale, disabled, or incompatible semantic index either fails with
  a stable configured error or uses an explicitly configured lexical-only
  fallback whose degraded mode is persisted in ContextPackage provenance.
  Once a logical query has produced a ContextPackage, an exact retry or replay
  must reuse its persisted retrieval mode and selected results; it must not
  switch from hybrid to lexical-only. If those results are unavailable, the
  rebuild fails closed with a stable error.
* A retry or rebuild with the same logical query reuses the validated query
  embedding identity and does not dispatch a second embedding request unless
  the prior attempt definitively failed; the identity is inspectable without
  exposing the vector or query text.
* A query using a Model Resource, provider configuration, dimension, metric, or
  normalization rule different from the pinned index generation is rejected
  before ranking and records the incompatibility without candidate evidence.
* Query embedding attempts reserve and debit the requesting execution budget and
  use shared model admission; pre-call rejection and post-call consumption are
  covered for success, failure, retry, and resume.
* Hybrid candidate selection alone cannot populate `intendedFiles`,
  `planningPredicates`, planning-evidence truth values, editable targets, or
  write permissions.
* Existing exact revision-bound planning evidence under AEP-053/AEP-054 and
  editable-target binding remain required before a candidate can become an edit
  target.
* Tests compare lexical-only and hybrid retrieval using fixed repository
  fixtures and assert candidate recall, deterministic ordering, budget
  enforcement, revision isolation, provenance, and degraded-mode behavior.
* Documentation clearly distinguishes semantic similarity, candidate
  relevance, exact repository evidence, and edit authorization and explains
  when hybrid retrieval is used in the issue-to-PR context pipeline.
