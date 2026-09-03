# AEP-054: Derive Planning Evidence Read Budgets

**Status:** In Progress

## Context

AEP-053 introduced trusted, revision-bound planning evidence so relevance
metadata alone cannot classify a repository file as requiring a change. The
controlled MTP-09/MTP-10 run for GitHub issue #82 exercises that new boundary
and fails while Context Builder materializes the evidence required by
`build-implementation-plan:1.8.0`.

The execution reached the corrected Resource generation:

```text
WorkflowExecution = workflowexecution-a8606aec-d7ea-533e-b3f9-8d6fbaabcd0a
repositoryRevision = 5ac8aaf2ce6ce00b1b69b461a033456a6b4192cc
traceId = trace-3d325d0f-c7cd-528a-881c-2880537d8fbe
analyze-issue:1.3.0 = SUCCEEDED
build-implementation-plan:1.8.0 = FAILED
```

The terminal TaskExecution evidence is:

```text
failure.class = CONFIGURATION
failure.retryable = false
failure.message = planning-evidence target 'docs/execution-plan.md' failed closed: ValueError
```

The successful AnalyzeIssue output declared an exact planning predicate for
`docs/execution-plan.md` with `maxBytes: 16384`. The Git blob at the immutable
WorkflowExecution revision is 17,595 bytes. Context Builder passes the
model-generated limit directly to the dogfood repository reader. That reader
loads the entire file and raises `ValueError` whenever the byte length exceeds
the supplied limit. The exception is then normalized to its class name, so the
persisted failure does not say that the target exceeded its byte ceiling.

This is not a provider, strict-schema, rate-limit, path-safety, revision, UTF-8,
or model-completion failure. BuildImplementationPlan never reached Agent or
Model invocation. The exact path is safe, exists, and is bound to the correct
revision. The only failing condition is:

```text
17,595-byte immutable target > 16,384-byte AnalyzeIssue estimate
```

The public AnalyzeIssue schema currently lets the model choose any `maxBytes`
value from 1 through 262,144. That untrusted estimate becomes a hard
correctness boundary even though the model does not have authoritative blob
size information. As repository documents grow, identical predicates can begin
failing solely because the model guessed a familiar power-of-two limit.

The current reader also conflates two separate resources: how many source bytes
may be inspected deterministically and how many tokens of evidence may enter a
ContextPackage. A `STATUS_EQUALS` predicate generally needs only one
well-defined field near the start of a task document; it does not require the
complete file body in model context. Conversely, a `TEXT_PRESENT` predicate
that can match anywhere may require a complete bounded scan even though its
persisted evidence contains only a digest, occurrence count, and selected
location. Reading for deterministic evaluation is not equivalent to exposing
the read bytes to an Agent.

Resolve this boundary without trusting model-selected byte ceilings, restoring
an unbounded repository dump, weakening immutable revision checks, silently
truncating predicate evaluation, or persisting file bodies in runtime evidence.

## Reproduction

Provide a credential-free regression derived from issue #82 using a temporary
revision-bound repository and the production Context Builder reader path.

1. Create `docs/execution-plan.md` as valid UTF-8 with an exact size of 17,595
   bytes and place the relevant status text on a deterministic line.
2. Index the file at an immutable 40-character repository revision and record
   its size and content digest in Repository Knowledge provenance.
3. Produce a valid AnalyzeIssue artifact containing an exact planning
   declaration for that path with `maxBytes: 16384`, `maxPaths: 1`, and a
   deterministic predicate/postcondition pair.
4. Build the `build-implementation-plan:1.8.0` ContextPackage through the
   production checkout-bound repository reader.
5. Demonstrate the current result: the reader loads the blob, compares 17,595
   to 16,384, raises `ValueError`, and the Task records only
   `planning-evidence target ... failed closed: ValueError`.
6. Demonstrate that BuildImplementationPlan creates no ContextPackage,
   AgentInvocation, or ModelInvocation after the read failure.
7. Demonstrate the corrected behavior using a runtime-derived inspection
   allowance or a predicate-specific bounded scanner. The same target must
   produce body-free planning evidence without placing the complete file in
   model context.

Add adjacent cases for a target exactly at the limit, one byte over a declared
estimate, above the operator hard ceiling, missing, non-UTF-8, revision-mismatched,
and changed between repository indexing and evidence materialization.

Fixtures may contain synthetic repository text and safe planning declarations.
They must not contain live prompts, artifact bodies, credentials, raw provider
messages, or unrestricted logs.

## Deliverable

Implement a deterministic planning-evidence inspection budget that:

* treats model-produced size hints as non-authoritative preferences or removes
  them from Agent output entirely; a model estimate cannot impose the hard
  repository read ceiling;
* derives enforceable limits from trusted Task/Workspace configuration,
  immutable repository metadata, predicate type, and the ContextPackage token
  budget;
* distinguishes source inspection bytes from evidence serialization tokens and
  documents both limits independently;
* obtains immutable blob size before body materialization and chooses a
  deterministic full-scan, predicate-specific scan, or explicit unsupported
  outcome before allocating or reading the target;
* supports bounded streaming or structured-field extraction for predicates
  such as `STATUS_EQUALS` without placing the complete source body in the
  ContextPackage;
* evaluates `TEXT_PRESENT` and `TEXT_ABSENT` only over a scope whose completeness
  is proven; a truncated prefix must never be reported as proof of absence;
* defines deterministic behavior when a complete scan is necessary but the
  immutable blob exceeds the trusted operator ceiling, including an explicit
  unsupported or size-limit classification rather than a generic exception;
* binds predicate results to the complete immutable blob identity even when
  only a bounded slice is selected as model-visible evidence;
* rejects size, digest, revision, path, encoding, or file-kind drift before the
  plan becomes authoritative;
* preserves the rule that Agents receive immutable ContextPackages and cannot
  retrieve repository knowledge or file contents directly;
* records safe failure metadata including normalized reason, path, declared
  hint when present, immutable blob size, applied trusted ceiling, predicate
  type, inspection strategy, and whether evaluation was complete;
* does not persist selected text, complete file bodies, prompts, model output
  bodies, credentials, or raw provider messages in runtime evidence or logs;
* versions every changed Event, Task, Agent, Prompt, Evaluation, Workflow,
  KnowledgeBase, schema, fixture, and exact Resource reference atomically; and
* updates Context Builder, Repository Intelligence, Workflow Runtime,
  observability, Resource authoring, and self-hosting operations documentation
  to describe the same inspection-budget and failure contract.

Do not satisfy this task solely by raising the generated value from 16 KiB to
another constant. A larger guess postpones the same failure and still lets
untrusted model output control a runtime safety boundary.

## Dependencies

* AEP-015
* AEP-016
* AEP-017
* AEP-029
* AEP-030
* AEP-036
* AEP-039
* AEP-040
* AEP-045
* AEP-053

## Acceptance Criteria

* An issue #82-derived regression evaluates the 17,595-byte
  `docs/execution-plan.md` target successfully even when the historical
  AnalyzeIssue artifact contains `maxBytes: 16384`; the model-selected value is
  not used as the hard runtime read limit.
* The enforceable inspection ceiling comes only from trusted, versioned
  configuration and is validated as a positive bounded integer during Resource
  loading. It is recorded in safe ContextPackage selection evidence.
* Immutable blob size is checked before body allocation. A target above the
  trusted ceiling produces a stable classified outcome with its path, size,
  ceiling, and predicate type and creates no AgentInvocation or ModelInvocation.
* `STATUS_EQUALS` uses a deterministic structured-field scanner with an
  explicit scan bound and rejects missing or ambiguous status fields. Evidence
  records the field identity and line location without retaining its surrounding
  source text.
* `TEXT_PRESENT` and `TEXT_ABSENT` results record whether the entire declared
  search scope was inspected. An incomplete scan can never produce `NO_MATCH`
  or prove absence.
* Predicate evaluation remains bound to the complete repository blob's
  revision, path, byte size, and content digest even when evaluation reads in
  bounded chunks or exposes only structured metadata to the ContextPackage.
* Planning-evidence inspection bytes do not count as model-context tokens unless
  their content is actually serialized into the ContextPackage. Token accounting
  remains deterministic and accurately counts all serialized evidence.
* A file exactly at the trusted ceiling succeeds. Files one byte over and far
  over the ceiling fail or become explicitly unsupported according to the same
  documented deterministic rule.
* Missing, non-regular, non-UTF-8, binary, unsafe, revision-mismatched,
  digest-mismatched, and concurrently changed targets each produce distinct
  stable safe classifications rather than the generic word `ValueError`.
* The checkout-bound production reader, Context Builder, deterministic harness,
  and unit fixtures exercise the same size and inspection semantics; tests do
  not substitute a more permissive fake reader for the relevant integration
  path.
* Prefix declarations account for the sum of inspected bytes and candidate
  count under trusted ceilings, preventing many individually valid targets from
  creating an unbounded aggregate scan.
* Repeated execution with identical revision, Resources, predicates, and source
  blobs produces identical selection ordering, predicate outcomes, digests,
  inspection metadata, and ContextPackage identity.
* The existing AEP-045 token-efficiency, AEP-051 editable-target, AEP-052 strict
  provider-schema, and AEP-053 planning-evidence/reconciliation regressions
  continue to pass.
* The complete `python -m pytest` suite and validation-image verification pass
  without network access or live credentials.
* A new controlled MTP-09/MTP-10 issue equivalent to #82 progresses through
  BuildImplementationPlan without a planning-evidence size failure. It either
  completes all six Tasks and creates exactly one open, unmerged pull request,
  or fails later with independently actionable evidence.
* GitHub issue #82 and WorkflowExecution
  `workflowexecution-a8606aec-d7ea-533e-b3f9-8d6fbaabcd0a` remain immutable
  historical evidence and are not replayed or rewritten.
* `README.md`, Context Builder and Repository Intelligence architecture,
  workflow-runtime and observability guidance, Resource authoring guidance, the
  self-hosting runbook, schemas, fixtures, Resource bundle, this task, and
  `docs/execution-plan.md` describe the same deterministic planning-evidence
  inspection-budget behavior.
