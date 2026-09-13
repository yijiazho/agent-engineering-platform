# AEP-066: Generate Localized Evidence-Bound Patches

**Status:** In Progress

## Context

The controlled MTP-09/MTP-10 run for GitHub issue #88 proved that the
multi-insertion and region-scoped planning work in AEP-055 operates through
BuildImplementationPlan, but exposed a downstream GeneratePatch failure:

```text
GitHub issue = #88
WorkflowExecution = workflowexecution-9d286f5a-769a-5b8e-bda4-6644aca452ea
repositoryRevision = 3afb148335f27a49f4d8ec0625b89bc22d1b5714
traceId = trace-40a065a8-341c-585c-a2e5-0ce578d91021
analyze-issue:1.4.0 = SUCCEEDED
build-implementation-plan:1.10.0 = SUCCEEDED
generate-patch:1.13.0 = FAILED
```

The issue requested four lines in the `README.md` Repository Layout code block.
GeneratePatch supplied the complete 710-line, 37,516-character editable
preimage with `truncation: NONE`. The model invocation completed normally,
using 8,784 output tokens under a 32,000-token allowance. Nevertheless, the
model returned a 593-line full-file replacement: it inserted the requested
four lines but omitted one unrelated 121-line range containing `Key Documents`
and most of `Current Status`.

Patch Evaluation correctly rejected the proposed change:

```text
addedLines = 4
deletedLines = 121
replacementRatio = 0.968
DESTRUCTIVE_REWRITE
SURROUNDING_CONTENT_NOT_PRESERVED
UNAUTHORIZED_DELETION
```

This is not input or output truncation. The current Code Generator contract
requires a complete replacement string for every `write`, even for a localized
four-line insertion. The schema accepts any textual replacement, and the
prompt requires a preimage-bound change but does not make byte-for-byte
preservation of unrelated regions structurally enforceable. Plan reconciliation
checks requested postconditions and required insertions against the proposed
content; it does not establish that every unrelated preimage region survived.
The later Git diff is therefore the first component that detects the omission.

The same execution exposed two additional evidence inconsistencies:

* plan reconciliation found the canonical multiline insertion in the proposed
  file, while Patch Evaluation reconstructed added diff blocks without their
  terminal newline and falsely emitted `REQUIRED_INSERTION_MISSING`; and
* the Planner classified preservation, scope, and `git diff --check` criteria
  as unsupported, after which Patch Evaluation unconditionally converted each
  unsupported criterion into an error without distinguishing a deterministic
  validation responsibility from a genuinely unevaluable requirement.

The correct response is not to broadly weaken destructive-change rejection.
Model internals are not controllable or observable, but proposed mutations are.
An unexplained deletion that contradicts an explicit preservation requirement
must remain blocked before push. Human review should supplement deterministic
safety rather than replace it. A broad rewrite may become approval-eligible
only when the issue and evidence-bound plan explicitly authorize that rewrite
and a human approval is bound to the exact artifact under AEP-059.

AEP also needs to preserve rejected proposals as inspectable internal evidence.
Operators should be able to determine what the model proposed and why the
platform rejected it through AEP-056 without publishing a branch, creating a
pull request, printing unrestricted artifact content, or claiming access to
hidden model reasoning.

Preserve issue #88 and its failed WorkflowExecution as immutable historical
evidence. Do not replay or rewrite the execution.

## Subsequent Dogfood Discovery

The server-derived trusted-region authorization follow-up is tracked by
[AEP-067](AEP-067-derive-trusted-region-authorization-server-side.md). It
removes the residual model-label equality constraint while retaining AEP-066's
aggregate postimage reconciliation and preservation boundaries.

A later controlled MTP-09/MTP-10 execution, WorkflowExecution
`workflowexecution-5f315522-c2d6-59ca-90d6-25d9d57ee7de`, revealed that the
localized-operation generation now has an inverse failure mode. AnalyzeIssue
and BuildImplementationPlan succeeded at repository revision
`4db056a6b9aa83b2a9b33ea35baa7c6250b7ca43`, but GeneratePatch
`1.14.0` failed before it could create a patch, EvaluationResult, or
RunValidation TaskExecution. The inspection summary consequently reported
RunValidation as blocked by a prerequisite; RunValidation and its Docker
validation image were not the failure source.

The plan authorized `README.md`, the `Repository Layout` region, and four
required values: `deploy/`, `deploy/local/`, `deploy/self-hosting/`, and
`deploy/validation/`. The model proposed one revision-bound, anchored,
region-scoped insertion containing the complete `deploy/` subtree. That is a
reasonable implementation of the issue's requested outcome. It did not alter
an unauthorized path, request a rewrite, replacement, or deletion, or evade
the supplied preimage and anchor controls.

GeneratePatch nevertheless rejected the proposal with the non-retryable
`CONFIGURATION` error `localized operation is not bound to an immutable
required insertion`. The current guard requires the number of `insert`
operations to equal the number of required values and the set of each
operation's complete `content` strings to equal the set of required values.
A single multiline operation therefore cannot satisfy multiple required
values, even when deterministic application would make every value present in
the authorized final region.

This rule was added in commit `1214934` (`Bind localized inserts to plan
evidence`) and made one-to-one in commit `e93271a` (`Support multi-insertion
localized edits`). It is an operation-shape constraint, not a safety property
or an issue acceptance criterion. It also conflicts with the localized-edit
goal of this task: an issue author should state intended outcomes and safety
boundaries, not predict an agent's exact operation partitioning or final code
shape.

The replacement contract must retain immutable plan authorization, exact
preimage/revision binding, unique anchors, region confinement, preservation
checks, and deterministic evaluation. It must instead prove required
insertions and other literal outcomes against the applied postimage. One or
more ordered localized operations may jointly satisfy any number of planned
values, provided their aggregate postimage satisfies every authorized
postcondition and does not introduce an unauthorized mutation. A
schema-valid-but-unsatisfied candidate is a rejected proposal with evaluation
evidence, not a configuration failure of the immutable Resource graph.

## Reproduction

Create credential-free regressions derived from issue #88:

1. Build an exact editable target containing at least 100 unrelated lines
   outside one named Markdown section.
2. Request a four-line insertion within that section and provide valid
   region-scoped planning evidence and a canonical multiline insertion.
3. Return a model-authored full-file replacement that makes the requested
   insertion but omits a large unrelated range.
4. Demonstrate that reconciliation currently passes the positive postcondition
   before Patch Evaluation rejects the destructive diff.
5. Demonstrate the newline disagreement in which reconciliation reports the
   multiline insertion as present but Patch Evaluation reports it missing.
6. Include deterministic preservation and validation criteria currently
   classified as unsupported and demonstrate how they become actionable
   evaluator responsibilities.
7. Create a plan that requires the four `deploy/` layout values in one
   authorized README region, then return one safe multiline insert operation
   containing the complete subtree.
8. Demonstrate that the current one-to-one operation/content comparison
   rejects that proposal before patch application, despite its satisfying the
   intended final layout.
9. Demonstrate the corrected postimage-based representation, application,
   evaluation, rejected-artifact retention, and terminal explanation.

Add adjacent cases for a legitimate explicit rewrite, a small replacement, an
anchor mismatch, ambiguous anchors, stale preimage evidence, overlapping edits,
line-ending variants, and repeated idempotent application.

Fixtures must remain deterministic and credential-free. They must not contain
live prompts, provider response bodies, secrets, or unrestricted logs.

## Deliverable

Implement a localized, evidence-bound GeneratePatch contract and a
deterministic patch disposition model that:

* allows the Code Generator to propose bounded insert, replace, and delete
  operations against exact preimage evidence instead of reproducing an entire
  existing file for every localized change;
* binds each operation to normalized repository path, immutable revision,
  preimage digest, supported region or anchor identity, expected match count,
  and the applicable plan/postcondition identities;
* treats those immutable plan identities as authorization and verification
  boundaries, rather than as a prescribed number, ordering, or textual
  partitioning of model operations;
* applies model-proposed operations in deterministic control-plane code and
  fails closed on missing, ambiguous, overlapping, out-of-order, stale, unsafe,
  or non-UTF-8 targets;
* proves that bytes or logical regions outside authorized edit spans remain
  unchanged, including stable preservation statistics and content digests;
* retains full-file replacement only behind an explicit plan-authorized rewrite
  contract with deterministic scope, deletion, and preservation expectations;
* keeps unexpected removal of unrelated content a rejection even when the
  resulting patch applies cleanly and changes only an allowed path;
* normalizes canonical insertion comparison consistently across proposed
  content, reconciliation evidence, unified-diff added blocks, LF, CRLF, a
  final newline, and no-final-newline cases without weakening exact content
  requirements;
* verifies each literal required insertion and other deterministic acceptance
  criterion against the aggregate applied postimage in its authorized region;
  permits one or more safe localized operations to jointly satisfy multiple
  values, and records which operations and postimage spans supplied that
  proof;
* maps preservation, allowed-scope, formatting, and configured validation
  criteria to their owning deterministic evaluators instead of classifying
  them as unsupported merely because they are not literal insertions;
* distinguishes `PASS`, `REVIEW_REQUIRED`, and `REJECT` dispositions without
  changing the meaning of existing EvaluationResult pass/fail evidence;
* permits `REVIEW_REQUIRED` only for an explicitly authorized risky operation
  whose exact patch digest, revision, target, and reason can be bound to an
  AEP-059 Approval; absence of that contract remains `REJECT`;
* prevents branch push, pull-request creation, CI dispatch, or any other remote
  mutation for rejected or unapproved review-required proposals;
* preserves rejected patch proposals and their evaluation evidence as internal,
  content-addressed runtime artifacts with an explicit non-publishable state;
* exposes through AEP-056 the complete causal chain from editable preimage and
  model output through reconciliation and Patch Evaluation, clearly separating
  observed facts, deterministic decisions, inferred contributing conditions,
  and unknowable model-internal reasoning;
* records bounded changed/unchanged region statistics, first unexpected
  divergence, context completeness, provider finish reason, token usage, and
  evaluator disagreements without printing source or artifact bodies by
  default;
* preserves rollback, retry, revision, path, policy, and artifact-integrity
  guarantees across failure and restart; and
* versions every changed Task, Agent, Prompt, Evaluation, Workflow, schema,
  fixture, and exact Resource reference atomically, with synchronized
  architecture, operator, security, and self-hosting documentation.

Do not implement this task by increasing the destructive-rewrite threshold,
silently ignoring unsupported criteria, assuming a schema-valid model response
preserved unrelated content, reintroducing a one-operation-per-required-value
or exact-operation-content rule, or pushing a rejected patch solely so a human
can inspect it.

## Dependencies

* AEP-002
* AEP-013
* AEP-017
* AEP-018
* AEP-020
* AEP-025
* AEP-026
* AEP-029
* AEP-030
* AEP-031
* AEP-033
* AEP-036
* AEP-039
* AEP-040
* AEP-051
* AEP-053
* AEP-054
* AEP-055

## Acceptance Criteria

* The issue #88 regression expresses the README change as localized operations
  and produces a four-line addition without requiring the model to reproduce
  the other 710 preimage lines.
* The `workflowexecution-5f315522-c2d6-59ca-90d6-25d9d57ee7de` regression
  accepts one anchored, revision-bound insertion of the complete `deploy/`
  subtree when its applied README postimage contains all four planned layout
  values in the authorized `Repository Layout` region.
* The same regression rejects a candidate only when the aggregate postimage
  lacks a required value, changes an unauthorized path or region, violates
  anchor/preimage/revision binding, overlaps another edit, or fails a relevant
  deterministic evaluator. It does not reject solely because several required
  values are supplied by one operation or one value is supplied by multiple
  operations.
* Deterministic application of a localized operation produces the expected
  postimage digest and proves that all content outside the authorized spans is
  unchanged.
* Missing, duplicate, ambiguous, stale, overlapping, unsafe, and out-of-order
  anchors fail with distinct stable diagnostics before filesystem mutation.
* A proposed postimage that adds the requested block but omits the historical
  121-line range is rejected as unrelated content loss and cannot reach any
  remote mutation boundary.
* An explicitly requested broad rewrite is distinguishable from accidental
  destructive change through immutable issue, plan, path, and operation
  evidence; it is never inferred from replacement size alone.
* A risky but explicitly authorized rewrite can produce `REVIEW_REQUIRED` and
  can proceed only after AEP-059 records approval of the exact patch digest.
  A changed patch invalidates that approval.
* Rejected and approval-pending proposals remain internally inspectable but
  create no remote branch, pull request, check run, or provider mutation.
* Multiline required-insertion evidence produces the same result during plan
  reconciliation and Patch Evaluation for LF, CRLF, final-newline, and
  no-final-newline variants. The issue #88 insertion is not falsely reported
  missing.
* Preservation, path-boundary, requested-scope, and configured validation
  criteria are assigned to deterministic evaluators and do not enter the plan
  as generic unsupported criteria when the platform has an owning check.
* Genuinely unsupported criteria retain stable explicit evidence and continue
  to block publication; they are not silently treated as satisfied.
* A structurally valid candidate that fails postimage reconciliation records a
  rejected-candidate Evaluation or Policy outcome with the decisive missing or
  violated condition. It is not classified as `CONFIGURATION` unless the
  immutable Resource graph itself is inconsistent or unusable.
* AEP-056 explains the issue #88 failure using runtime evidence: complete input,
  completed provider response, four inserted lines, 121 unexpected deleted
  lines, decisive rejection checks, rollback, and absence of remote mutation.
  It labels the full-file generation interface as a possible contributing
  factor rather than claiming it was the model's hidden reason.
* Rejected-artifact metadata contains repository revision, preimage and proposal
  digests, producing invocation, plan and reconciliation identities,
  EvaluationResult IDs, publication eligibility, and retention state without
  exposing the artifact body in ordinary logs or summaries.
* Existing patch applicability, path boundary, editable-target,
  planning-evidence, region-scoping, insertion ownership, rollback, validation,
  acceptance, and publication-policy tests continue to pass.
* Unit, schema, fixture, deterministic harness, and complete `python -m pytest`
  coverage includes minimal edits, explicit rewrites, accidental omissions,
  newline normalization, unsupported criteria, review-required approval, and
  rejected-artifact inspection.
* A corrected immutable self-hosting generation completes the issue #88-derived
  six-Task MTP-10 path and creates exactly one open, unmerged pull request whose
  diff contains only the requested localized README addition.
* GitHub issue #88 and WorkflowExecution
  `workflowexecution-9d286f5a-769a-5b8e-bda4-6644aca452ea` remain immutable
  historical evidence and are not replayed or rewritten.
* WorkflowExecution `workflowexecution-5f315522-c2d6-59ca-90d6-25d9d57ee7de`
  remains immutable diagnostic evidence. A later controlled issue, rather than
  replaying that execution, verifies the corrected contract through
  GeneratePatch and then RunValidation.
* `README.md`, relevant ADRs and architecture documents, Resource authoring,
  evaluation and policy guidance, schemas, fixtures, the self-hosting runbook,
  this task, and `docs/execution-plan.md` describe the same localized patch and
  disposition contract.
