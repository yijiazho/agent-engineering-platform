# AEP-051: Bind Patch Generation To Editable Target Context

**Status:** In Progress

## Context

AEP-045 made ContextPackage construction bounded and relevance-ranked so a
narrow issue no longer receives a repository-scale prompt. That optimization
correctly separates minimum relevant context from dumping an entire repository,
but the live `GeneratePatch` path now exposes a different contract gap:
relevance results identify candidate files without materializing the exact
revision-bound contents required to edit them.

The Code Generator output contract requires complete replacement contents:

```json
{
  "changes": [
    {"path": "README.md", "content": "<complete file contents>"}
  ]
}
```

`ContextBuilder` currently satisfies `candidate-files` with
`CandidateFileQuery`. A returned FILE element records identity, path, language,
score, selection reasons, repository revision, knowledge snapshot, and traversal
provenance, but not the file body. Ranking can also omit an exact path already
selected by the evaluated implementation plan because the query is driven by
general relevance terms rather than by `IMPLEMENTATION_PLAN.intendedFiles`.
The Code Generator cannot recover the missing material: Agents must not query
Repository Knowledge directly, Agent callers cannot use Filesystem reads for
repository retrieval, and the patch-generation prompt requires use of only the
supplied ContextPackage.

Two controlled self-hosting executions demonstrate the result.

Issue #69, `[Test] Add deploy directory to README repository layout`, allowed
only `README.md` and required adding four layout lines while preserving every
existing entry and all surrounding guidance. AnalyzeIssue and
BuildImplementationPlan preserved those requirements and selected only
`README.md`. The GeneratePatch ContextPackage
`contextpackage-603de75efeb3204dbfe9` did not contain the root README body or
even a FILE element for the root README; it contained ranked metadata for other
README files. AgentInvocation `agentinvocation-5823bf0dba6d2737e29ff7e6`
therefore generated a plausible new 28-line README as complete replacement
content. Pull request #70 deleted 664 lines and added 12.

Issue #71, `[Dogfood] Update the task status`, produced the same failure shape.
Its patch ContextPackage `contextpackage-585d8b43fee58f441643` identified some
task-document paths but supplied metadata rather than their bodies.
AgentInvocation `agentinvocation-4ccc8574733ba95cec0f039b` synthesized short
replacement documents, deleting substantial implementation and acceptance
evidence in pull request #72.

Both executions passed patch, build, test, acceptance, and publication gates.
Patch Evaluation proves applicability and changed-path authorization, not
preservation or semantic compliance. Build and repository tests need not fail
after destructive documentation changes. EvaluateAcceptance verifies that
required artifacts and EvaluationResults are present, revision-consistent, and
passing; it does not execute the normalized issue acceptance criteria against
the final diff. Publication Policy consequently receives internally consistent
PASS evidence even when the patch contradicts the requested change.

This is not primarily a model-selection defect. The live `gpt-5` invocations
produced accurate issue analyses and implementation plans in both runs. No model
can reliably reproduce an unseen 680-line file from path metadata. A different
model may refuse or fail differently, but it cannot repair a missing edit-input
contract.

Resolve the gap without reverting to an unbounded folder dump, allowing Agents
to retrieve repository knowledge directly, weakening revision or provenance
binding, or treating a successful build as proof that the requested change was
implemented.

## Reproduction

Provide credential-free regressions derived from the two live executions.
Fixtures must contain only safe issue text, repository files, expected context
manifests, and expected evaluation outcomes; do not copy credentials, provider
headers, unrestricted logs, or unrelated webhook payload fields.

### Exact-file omission

1. Create a repository fixture with a multi-section `README.md` whose Repository
   Layout block lacks `deploy/`.
2. Analyze an issue equivalent to #69 and produce an evaluated implementation
   plan whose sole intended file is `README.md`.
3. Build the GeneratePatch ContextPackage using the production relevance
   ranking and token budget.
4. Demonstrate the current failure: the package either omits `README.md` or
   includes only FILE metadata, while the Code Generator contract still asks
   for complete replacement content.
5. Demonstrate that a shortened replacement applies, stays within the allowed
   path, passes build/tests, and reaches acceptance despite violating the
   preservation criteria.

### Ranked metadata without editable content

1. Create several task documents with similar status and manual-testing terms,
   including completed tasks with substantial implementation evidence and one
   genuinely in-progress task.
2. Produce an evaluated plan equivalent to the #71 shape.
3. Show that ranked FILE identities alone do not provide sufficient material to
   preserve the selected documents during whole-file generation.
4. Show that current patch evaluation accepts destructive replacements when
   every changed path is listed in the plan.

The regression must inspect persisted ContextPackage and EvaluationResult
evidence rather than relying only on a fake model's expected output. It must
prove both the missing input and the downstream false-positive acceptance.

## Deliverable

Implement a bounded edit-context and change-compliance contract that:

* preserves relevance-ranked discovery for AnalyzeIssue and planning rather
  than adding an unbounded repository or folder dump;
* introduces an explicit editable-target context type, or an equivalently clear
  contract, distinct from candidate-file metadata;
* deterministically resolves every normalized
  `IMPLEMENTATION_PLAN.intendedFiles` path at the WorkflowExecution's immutable
  repository revision after the plan has passed evaluation;
* supplies the exact content needed by the selected patch representation, with
  path, content address or preimage digest, revision, knowledge snapshot, source
  provenance, byte count, token estimate, and deterministic ordering;
* fails GeneratePatch before Model invocation when any required target is
  missing, unreadable, non-text when only text edits are supported, outside the
  checkout boundary, stale, duplicated, or too large for the declared input
  budget;
* defines deterministic behavior when the complete required edit set cannot fit
  the configured budget, such as bounded source slices with edit anchors or a
  separate per-file edit strategy, without silently dropping a planned file;
* replaces or strengthens the complete-file replacement contract with
  preimage-bound unified diffs, structured edits, or another representation
  that cannot overwrite unseen content;
* verifies every edit against the supplied preimage before mutation and rejects
  stale, ambiguous, overlapping, malformed, or non-applicable edits without
  leaving a partial workspace mutation;
* distinguishes plan-authorized paths from required planned changes and records
  an explicit disposition for every intended file so omissions cannot pass
  unnoticed;
* adds deterministic destructive-change evidence, including added/deleted line
  counts, replacement ratio, and preservation checks appropriate to the
  normalized issue criteria, while allowing explicitly requested deletions;
* evaluates the final revision-bound diff against machine-checkable issue
  acceptance criteria and records unsupported semantic criteria explicitly
  rather than treating artifact completeness as semantic success;
* prevents Publication Policy from allowing `git.push` or `github.create_pr`
  unless editable-target completeness, preimage verification, required-file
  disposition, patch safety, and change-compliance evidence all pass;
* retains the architecture rule that Agents receive immutable ContextPackages
  and never retrieve repository knowledge directly;
* versions every changed Task, Agent, Prompt, Evaluation, Policy, Workflow,
  KnowledgeBase, or other Resource and updates all exact references and bundle
  fixtures atomically; and
* updates Context Builder, Repository Knowledge, patch generation, evaluation,
  publication, observability, and self-hosting documentation to describe the
  same bounded edit contract and failure evidence.

Do not satisfy this task solely by changing the model, increasing the token
budget, adding a stronger prompt, or applying a deletion-count heuristic after
publication. Those may be defense-in-depth measures, but they do not supply the
missing source material or prove compliance before mutation.

## Dependencies

* AEP-016
* AEP-017
* AEP-026
* AEP-031
* AEP-033
* AEP-040
* AEP-045
* AEP-048

## Acceptance Criteria

* A #69-derived regression supplies the exact base-revision `README.md` content
  to GeneratePatch and produces a localized addition of `deploy/`, `local/`,
  `self-hosting/`, and `validation/` without deleting or rewriting any other
  README line.
* A #71-derived regression supplies exact contents for every planned task file
  and cannot replace already-completed task documents or remove their existing
  implementation and acceptance evidence merely because their paths ranked as
  relevant.
* Candidate-file discovery remains bounded and relevance-ranked. GeneratePatch
  does not receive a complete directory or repository inventory unless every
  item is explicitly required by the evaluated plan and fits the declared
  bounded edit-context contract.
* Every planned editable target appears exactly once in the ContextPackage with
  its normalized path, immutable repository revision, source provenance,
  content/preimage digest, size, and sufficient source material for the chosen
  edit representation. Ranked metadata alone cannot satisfy this requirement.
* Editable-target reads use a byte ceiling derived from the Task input-context
  token budget, reject non-canonical POSIX path spellings, and run only after a
  path-scoped Git preflight rejects tracked, untracked, or ignored target state.
* Filesystem mutations and terminal ToolInvocation evidence are atomic: an
  evidence-persistence failure restores prior bytes and file mode before it
  escapes, and multi-file rollback preserves the original mode of deleted files.
* A missing, stale, unreadable, unsupported, duplicate, or over-budget target
  fails closed before Model invocation and before Filesystem mutation with
  stable, non-sensitive evidence identifying the failed target and reason.
* Identical issue, plan, repository revision, Resource versions, and source
  contents produce identical editable-target ordering, content addresses,
  token accounting, and ContextPackage identity.
* The Code Generator cannot submit an unbound complete-file replacement for a
  file whose original content was not supplied. Preimage mismatch, malformed
  edit, overlapping edit, and partial-application cases fail without changing
  the execution workspace.
* GeneratePatch records a disposition for every `intendedFiles` entry. A
  required file omitted from model output or from the final diff causes a
  terminal evaluation failure unless the evaluated plan explicitly marks it as
  no-change with deterministic supporting evidence.
* Patch Evaluation rejects an unexplained destructive rewrite even when the
  path is allowed and the patch applies. Explicit issue instructions requesting
  deletion remain representable and testable.
* Final change-compliance evidence evaluates deterministic criteria such as
  allowed paths, required insertions, forbidden unrelated changes, preservation
  of surrounding content, and `git diff --check`. Unsupported semantic criteria
  remain visible and cannot be silently counted as passed.
* EvaluateAcceptance distinguishes evidence completeness from change
  correctness. A complete set of revision-consistent artifacts with a failed or
  missing change-compliance result produces `FAIL`.
* Publication Policy denies push and pull-request creation when editable-target
  context is incomplete, preimage verification fails, a required planned change
  is missing, destructive-change checks fail, or change compliance fails.
* Regression coverage includes targeted Context Builder, Repository Knowledge,
  GeneratePatch, patch-evaluation, EvaluateAcceptance, Publication Policy,
  deterministic harness, and dogfood runtime tests, followed by the complete
  `python -m pytest` and validation-image verification gates.
* One new controlled MTP-09/MTP-10 issue requesting a localized edit completes
  all six Tasks and creates exactly one open, unmerged pull request whose diff
  preserves unrelated content. Persisted evidence explains the selected target
  contents, edit application, compliance result, and publication decision.
* Pull requests #70 and #72 remain historical evidence and are not merged,
  rewritten, replayed, or treated as successful quality validation by this
  task. Any operator cleanup is separately authorized and recorded.
* `README.md`, `docs/architecture/context-builder.md`, repository-intelligence,
  workflow-runtime and evaluation architecture, the self-hosting runbook,
  schemas, fixtures, Resource bundle, this task, and `docs/execution-plan.md`
  describe the same final edit-context, evaluation, and fail-closed publication
  behavior.
