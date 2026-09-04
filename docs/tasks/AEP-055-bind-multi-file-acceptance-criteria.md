# AEP-055: Bind Multi-Insertion Acceptance Criteria To Planning Evidence

**Status:** Not Started

## Context

The controlled MTP-09/MTP-10 self-hosting run for GitHub issue #85 avoided
the prefix-cardinality failure observed for issue #84 by naming each intended
task document explicitly. Context Builder successfully materialized twelve
revision-bound planning-evidence records, and both live model calls completed.
The execution nevertheless failed in `build-implementation-plan:1.9.0` after
the Planner returned schema-valid output:

```text
GitHub issue = #85
WorkflowExecution = workflowexecution-7d076c6f-89d9-5061-bc71-fa6f85cd86a2
repositoryRevision = 7630bb139ad50872b3d72f8e0e8e37c3731ec5dd
traceId = trace-323e795e-2a2d-55a8-96a3-a4b576cc0e72
analyze-issue:1.3.0 = SUCCEEDED
build-implementation-plan:1.9.0 = FAILED
```

The terminal TaskExecution evidence is:

```text
failure.class = CONFIGURATION
failure.retryable = false
failure.message = each required-insertion classification must bind its own insertion evidence
```

AnalyzeIssue represented the requested updates to eleven task documents as one
acceptance criterion:

```text
All 11 listed task files have '**Status:** Completed'.
```

The Planner classified that criterion as `REQUIRED_INSERTION` and produced a
list of per-file `requiredInsertions`, but set the classification's singular
`requiredInsertion` field to `null`. The JSON Schema permits that value because
the same shape serves `UNSUPPORTED` classifications. A later deterministic
check requires every `REQUIRED_INSERTION` classification to bind one exact
entry from `requiredInsertions`, so the output passed provider and schema
validation and then failed the Task contract. More importantly, the singular
field cannot faithfully bind a criterion that requires the same insertion in
multiple files. The contract currently has no valid representation for one
acceptance criterion backed by several required insertions.

The run also exposed a separate status-evidence error. AnalyzeIssue declared
whole-file predicates for every task document:

```text
predicate = TEXT_PRESENT "**Status:** In Progress"
postcondition = TEXT_PRESENT "**Status:** Completed"
```

For `docs/tasks/AEP-053-bind-planning-decisions-to-exact-repository-evidence.md`,
both predicates matched: its authoritative top-level status was `In Progress`,
while narrative text later in the document mentioned the literal
`**Status:** Completed`. The Planner therefore placed AEP-053 in
`noChangeFiles`, omitted it from `requiredChangePaths`, and generated only ten
task-file insertions. This contradicts the structured status-field semantics
introduced by AEP-054. A status transition must use `STATUS_EQUALS` evidence
bound to the unique top-level status field, not unrestricted substring
presence anywhere in the file.

The execution-plan requirements were additionally emitted as
`UNSUPPORTED_SEMANTIC`, causing the Planner to classify deterministic requested
changes as unsupported even though the exact file was available. The platform
needs an explicit, deterministic contract for multi-location updates without
allowing a model to invent evidence relationships, silently discard required
paths, or pass schema validation only to fail an avoidable post-model check.

A follow-up controlled run for GitHub issue #86 reduced the request to one
exact file, `README.md`, and still reproduced the same failure. The requested
change was to add `deploy/` and its `local/`, `self-hosting/`, and `validation/`
children to the Repository Layout code block:

```text
GitHub issue = #86
WorkflowExecution = workflowexecution-7eb3faff-fc83-5ef7-8157-ec270b914563
repositoryRevision = 7630bb139ad50872b3d72f8e0e8e37c3731ec5dd
traceId = trace-cf553ff5-b1bc-5e70-a511-b7df39b81e72
analyze-issue:1.3.0 = SUCCEEDED
build-implementation-plan:1.9.0 = FAILED
```

Issue #86 proves that file count is not the governing cardinality. One
acceptance criterion required three insertions in the same file. The Planner
listed all three in `requiredInsertions`, but its singular classification
binding was again `null`, producing the same terminal configuration failure.
The contract defect is therefore criterion-to-insertion cardinality, whether
the insertions target one file or many files.

Issue #86 also demonstrates that an exact path does not make whole-file text
predicates semantically precise. AnalyzeIssue emitted `TEXT_ABSENT` predicates
for `deploy/`, `deploy/local/`, `deploy/self-hosting/`, and
`deploy/validation/`. Those strings were absent from the requested Repository
Layout block but already appeared elsewhere in README 27, 12, 9, and 6 times,
respectively. Every change predicate returned `NO_MATCH`, and every
`TEXT_PRESENT` postcondition returned `MATCH`. The Planner consequently
produced the contradictory combination:

```text
requiredChangePaths = []
noChangeFiles = ["README.md"]
verifiedNoChangePaths = ["README.md"]
requiredInsertions = four additions to README.md
```

The planning-evidence vocabulary can currently scope by path but not by a
structured region within a file. A request about a named Markdown section or
code block therefore cannot be proven by unrestricted file-wide substring
search when the same text occurs elsewhere. The corrected contract must either
support deterministic region-scoped evidence or classify that predicate as
unsupported before it can authorize `NO_CHANGE`; unrelated occurrences must
never satisfy the requested postcondition.

Preserve issues #85 and #86 and their failed WorkflowExecutions as immutable
historical evidence. Do not replay or rewrite either execution.

## Reproduction

Add a credential-free regression derived from issue #85 at the immutable
repository revision above:

1. Supply an analyzed acceptance criterion that requires the same status
   insertion in at least two exact task files.
2. Supply trusted planning evidence proving each file's unique top-level
   status is `In Progress` and its requested postcondition is `Completed`.
3. Include a narrative occurrence of `**Status:** Completed` in one source
   file while leaving its top-level status `In Progress`.
4. Demonstrate the current AnalyzeIssue behavior when it chooses
   `TEXT_PRESENT`: both the predicate and postcondition match that file.
5. Demonstrate the current Planner output shape with multiple entries in
   `requiredInsertions` but a `REQUIRED_INSERTION` classification whose
   singular `requiredInsertion` is `null`.
6. Demonstrate the current terminal configuration failure and absence of an
   `IMPLEMENTATION_PLAN` artifact.
7. Demonstrate the corrected representation, deterministic validation, and
   disposition of every exact requested path.

Add a second minimal regression derived from issue #86:

1. Use one exact `README.md` target and one acceptance criterion requiring at
   least three insertions in that file.
2. Place each requested string outside the Repository Layout block while
   keeping it absent inside that block.
3. Demonstrate that the current file-wide predicates report the postconditions
   as satisfied and incorrectly authorize `NO_CHANGE`.
4. Demonstrate that the current singular classification cannot bind all three
   canonical insertions and fails with the same configuration error.
5. Demonstrate corrected criterion-to-insertion accounting and region-scoped
   evidence, or a stable pre-planning unsupported result when the requested
   region cannot be evaluated deterministically.

The regression must use body-free persisted planning evidence and deterministic
fixtures. It must not contain live prompts, model bodies, credentials, webhook
payloads, or provider messages.

## Deliverable

Implement a coherent acceptance-criterion and planning-evidence contract that:

* represents one acceptance criterion backed by zero, one, or multiple exact
  required insertions without ambiguity;
* makes the classification-to-insertion relationship structurally valid in
  the Task, Agent, Evaluation, runtime, fixture, and JSON Schema contracts;
* rejects `REQUIRED_INSERTION` classifications with no evidence binding,
  unknown bindings, duplicate bindings, or only a partial subset of the
  insertions required by that criterion;
* requires `UNSUPPORTED` classifications to have no insertion bindings and to
  remain represented exactly once in `unsupportedAcceptanceCriteria`;
* defines deterministic ownership when multiple criteria refer to the same
  insertion, including whether deduplication is allowed and how it is recorded;
* applies the same multi-insertion representation when all insertions target
  one file; no contract rule may infer insertion cardinality from file count;
* derives status transitions with `STATUS_EQUALS` predicates and
  `STATUS_EQUALS` postconditions whenever the target exposes the documented
  structured status field;
* prevents narrative mentions, examples, historical evidence, or acceptance
  criteria elsewhere in a document from satisfying a structured status
  predicate or postcondition;
* defines deterministic region-scoped evidence for requests about a named
  Markdown section, fenced code block, or other supported structural region,
  including unambiguous region identity and completeness semantics;
* ensures occurrences outside the declared region cannot satisfy its change
  predicate or postcondition, authorize `NO_CHANGE`, or provide required
  insertion evidence;
* returns a stable actionable unsupported classification before planning when
  a requested region is missing, duplicated, malformed, or not supported by
  the declared evidence vocabulary;
* preserves exact path, immutable revision, blob digest, field identity, line
  location, inspection strategy, and selection identity for every status
  decision without persisting source bodies;
* makes every evidence-proven `In Progress` task an intended and required-change
  path, and permits `noChangeFiles` only when the structured postcondition is
  already satisfied and the change predicate is not;
* provides a deterministic representation for exact execution-plan status and
  count updates, or records a stable, actionable unsupported reason before the
  Planner is asked to produce an implementable plan;
* validates cross-field accounting at the earliest deterministic boundary so
  schema-valid but semantically impossible output cannot be published as a
  successful AgentInvocation result;
* preserves retry safety, immutable runtime evidence, content-addressed
  artifacts, and the rule that Agents cannot retrieve repository knowledge;
* versions all changed Task, Agent, Prompt, Evaluation, Workflow, schema, and
  fixture references atomically; and
* updates architecture, authoring, observability, and self-hosting guidance to
  describe the same multi-insertion and structured-field contract.

Do not solve this task by weakening the deterministic accounting check,
removing required-insertion evidence, treating every criterion as unsupported,
or adding issue-specific prompt wording. Do not use unrestricted whole-file
text matching for structured status fields.

## Dependencies

* AEP-002
* AEP-013
* AEP-017
* AEP-025
* AEP-029
* AEP-030
* AEP-036
* AEP-040
* AEP-051
* AEP-052
* AEP-053
* AEP-054

## Acceptance Criteria

* The implementation-plan contract can represent one acceptance criterion
  bound to multiple `{path, value}` required insertions, and the representation
  passes both JSON Schema and deterministic cross-field validation.
* Every `REQUIRED_INSERTION` classification binds at least one insertion and
  all bound insertions exist exactly in the canonical `requiredInsertions`
  collection; missing, unknown, duplicate, and partial bindings fail closed
  with stable safe messages.
* Every analyzed acceptance criterion is classified exactly once, every
  canonical required insertion has deterministic criterion ownership, and
  unsupported criteria cannot carry insertion bindings.
* A credential-free issue #85 regression with at least two task files produces
  a valid implementation plan instead of
  `each required-insertion classification must bind its own insertion evidence`.
* A credential-free issue #86 regression with one target file and at least
  three required insertions also produces a valid implementation plan; the
  result does not depend on distributing insertions across multiple files.
* The issue #85 regression represents all eleven qualifying task documents as
  required changes; it does not omit AEP-053 or classify it as `NO_CHANGE`.
* A task document whose top-level status is `In Progress` and whose narrative
  mentions `**Status:** Completed` produces `STATUS_EQUALS(In Progress) = MATCH`
  and `STATUS_EQUALS(Completed) = NO_MATCH`.
* README text that occurs outside the Repository Layout code block does not
  satisfy a predicate or postcondition scoped to that block. The issue #86
  regression does not classify README as `NO_CHANGE` merely because
  `deploy/` paths occur in commands and prose elsewhere in the file.
* Supported region-scoped evidence records the exact path, deterministic region
  identity, match count, evaluation completeness, revision, blob digest, and
  selection identity without persisting the region body.
* Missing, duplicate, malformed, and unsupported region selectors produce
  distinct stable results and cannot authorize a change or no-change decision.
* Missing, duplicate, malformed, or ambiguous structured status fields fail
  with distinct deterministic classifications and create no authoritative
  implementation plan.
* Planning evidence for structured status checks contains field identity, line
  location, revision, complete blob digest, inspection limits, and selection
  identity, but no selected line text or complete file body.
* `requiredChangePaths`, `intendedFiles`, `noChangeFiles`, `unsupportedPaths`,
  `requiredInsertions`, and acceptance-criterion classifications reconcile to
  the same exact path set before an implementation-plan artifact is published.
* Execution-plan row and summary updates are either represented with
  deterministic evidence sufficient for later reconciliation or rejected with
  a specific unsupported classification that cannot be mistaken for success.
* Existing AEP-051 editable-target, AEP-052 strict-schema, AEP-053 planning
  evidence, and AEP-054 inspection-budget regressions continue to pass.
* Unit, schema, fixture, deterministic harness, and complete `python -m pytest`
  tests cover single-insertion, multi-insertion, shared-insertion, unsupported,
  narrative false-positive, partial-binding, and duplicate-binding cases.
* New controlled MTP-09/MTP-10 runs derived from issues #85 and #86 progress
  beyond BuildImplementationPlan. Each either completes all six Tasks and
  creates exactly one open, unmerged pull request or fails later with
  independently actionable evidence.
* GitHub issues #84, #85, and #86 and WorkflowExecutions
  `workflowexecution-3f371765-47a3-5b4a-8603-5fa7e58af783` and
  `workflowexecution-7d076c6f-89d9-5061-bc71-fa6f85cd86a2` and
  `workflowexecution-7eb3faff-fc83-5ef7-8157-ec270b914563` remain immutable
  historical evidence and are not replayed or rewritten.
* `README.md`, relevant architecture and operations documents, Resource
  authoring guidance, schemas, fixtures, Resource bundle, this task, and
  `docs/execution-plan.md` describe the same corrected contract.
