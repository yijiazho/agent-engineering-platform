# AEP-072: Bind Documentation Scope Constraints to Patch Evidence

**Status:** Not Started

## Context

The live test for [GitHub issue #120](https://github.com/yijiazho/agent-engineering-platform/issues/120)
requested a README example and explicitly required a documentation-only change
with no runtime modifications. At repository revision
`294b3e8ebf98a791c8d2569dae9ae0b12d37d1a4`, WorkflowExecution
`workflowexecution-97e02476-453a-59d6-a8f7-34e3226c3e8e` failed its first
AnalyzeIssue Task. Its AgentInvocation recorded:

```text
EVALUATION: structured output does not match outputSchema:
every acceptance criterion requires document or evaluator ownership
```

The model gave the issue's `AC-DOCS-ONLY-NO-RUNTIME` criterion no planning
predicate or evaluator requirement. It also invented a separate
`git diff --check` criterion that the issue did not request. Exact replay
`workflowexecution-replay-379a07e2-c3e1-4bc7-8af7-629b3d792e65` reused
the historical ContextPackage and dispatched a new AnalyzeIssue attempt, but
its model output instead attached `PATCH_EVALUATION/DIFF_CHECK_PASSES` to
`ER-DIFF-CHECK-README`, which was not an acceptance criterion. That invocation
failed with:

```text
EVALUATION: structured output does not match outputSchema:
unsupported evaluator-owned requirement; expected PATCH_EVALUATION/DIFF_CHECK_PASSES
```

Both outputs passed the declared JSON Schema Evaluation. The later deterministic
AnalyzeIssue callback rejected their criterion ownership, so both
AgentInvocations recorded `outputSchemaValidation: FAILED` and no
`ISSUE_ANALYSIS` artifact was published. The source and replay records are
immutable diagnostic evidence; do not rewrite, reclassify, or replay them to
make this task appear complete.

The current contract supports document predicates and only one typed evaluator
requirement: an explicitly requested `git diff --check`. A constraint on the
*set of changed files* is neither a document predicate nor that diff check.
The Issue Analyzer prompt says to leave unsupported outcomes explicit, but its
output schema has no explicit unsupported criterion disposition, while
`_validate_evaluator_owned_requirements` requires every criterion to have one
of the two existing owners. The model therefore cannot produce an accepted,
truthful analysis for this issue. The invalid replay requirement is a second
model response to the same contract gap, not evidence that the issue requested
a diff check.

## Deliverable

Add a typed, deterministic ownership path for acceptance criteria that restrict
which files a patch may change. Bind an exact documentation target such as
`README.md` to a Patch Evaluation requirement that checks the final diff's
changed paths. If an issue's scope cannot be established from its immutable
input and trusted repository evidence, represent the criterion explicitly as
unsupported and keep publication closed. Align the Issue Analyzer, Planner,
Patch Evaluation, Resources, and inspection evidence with that contract.

## Dependencies

* AEP-025
* AEP-027
* AEP-029
* AEP-030
* AEP-040
* AEP-052
* AEP-055
* AEP-056
* AEP-066
* AEP-068
* AEP-057

## Acceptance Criteria

* A credential-free regression derived from issue #120 reproduces both stored
  semantic failures while showing that the declared JSON Schema Evaluations
  pass. It distinguishes model output validation from later schema evaluation.
* AnalyzeIssue can bind the explicit `README.md`-only criterion to a versioned,
  typed changed-path requirement. The requirement cites the issue's stable
  criterion ID and does not authorize a mutation, select an editable region,
  or turn a model-proposed path into trusted authority.
* Patch Evaluation checks actual changed paths against the trusted scope at the
  pinned revision. A patch that changes runtime code or another undeclared
  path fails even if a model plan lists that path; a README-only patch can pass.
* A criterion with no safe document or evaluator owner has an explicit
  unsupported disposition that survives planning and prevents publication.
  Missing, duplicate, unknown, or conflicting criterion references still fail
  closed during AnalyzeIssue validation.
* The Issue Analyzer Prompt does not encourage filler evaluator requirements.
  Deterministic validation rejects an invented `git diff --check` criterion or
  requirement like the replay's `ER-DIFF-CHECK-README`; a model-authored
  criterion is not proof that the source issue requested that check. Invalid
  bindings retain decisive semantic failure evidence.
* Strict provider schemas, AnalyzeIssue Task and Agent output contracts,
  versioned Prompt and Evaluation Resources, Planner classifications, Patch
  Evaluation, deterministic fixtures, and documentation agree on the new
  requirement and unsupported disposition.
* Tests cover a supported documentation scope, an out-of-scope changed file,
  an unrepresentable constraint, an invented evaluator criterion, source/replay
  output variation, and source execution immutability. A controlled follow-up
  issue reaches at least BuildImplementationPlan without this ownership failure;
  AEP-043 retains the separate six-Task live publication gate.

## Implementation Notes

Preserve AEP-068's fail-closed rule for unknown evaluator pairs and AEP-067's
server-derived edit spans. A changed-path requirement is an evaluation of the
final patch, not permission to edit every file in its allowed set. Use the
existing path normalization and immutable issue, plan, and diff evidence where
possible. Do not treat a model's invented `git diff --check` criterion as proof
that the user requested one.
