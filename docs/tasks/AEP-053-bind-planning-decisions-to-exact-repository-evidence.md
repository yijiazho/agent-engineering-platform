# AEP-053: Bind Planning Decisions To Exact Repository Evidence

**Status:** In Progress

## Context

AEP-051 supplies GeneratePatch with exact, revision-bound editable target
contents and fails closed when the Code Generator omits a file that the
implementation plan declares must change. That closes the destructive
replacement gap, but the controlled MTP-09/MTP-10 run for GitHub issue #78
exposes an earlier planning gap: BuildImplementationPlan can classify a file as
requiring a change using relevance-ranked metadata without proving the
file-specific condition that makes the change necessary.

Issue #78 requested that task documents still marked `In Progress` pending the
manual test plan be changed to `Completed`, with matching updates in
`docs/execution-plan.md`. At repository revision
`679a0c6f4eb04483aa917faae018a3037d3e82f9`, AnalyzeIssue and
`build-implementation-plan:1.7.0` succeeded. The implementation plan then
included these five task documents as required intended files:

```text
docs/tasks/AEP-005-normalize-github-issue-created-event.md
docs/tasks/AEP-021-implement-filesystem-tool.md
docs/tasks/AEP-029-implement-analyzeissue-task-handler.md
docs/tasks/AEP-033-implement-evaluateacceptance-task-handler.md
docs/tasks/AEP-044-stabilize-self-hosting-dogfood-startup.md
```

Every one of those documents already contained `**Status:** Completed`, and
every corresponding execution-plan entry was already `Completed` at the pinned
revision. They were relevant to historic implementation or manual-testing
language, but they did not satisfy the issue's required `In Progress`
predicate. Planning received ranked candidate-file evidence rather than an
exact, path-bound view sufficient to verify that predicate and produced a
plausible but false required-change set.

GeneratePatch then built `contextpackage-ceea1d9b7cea96487d2c` with seven exact
editable targets. The package used 30,971 of its 32,000-token budget with
`truncation: NONE`. Its AgentInvocation and ModelInvocation both succeeded,
OpenAI reported `finishReason: completed`, and output schema validation passed.
The Code Generator omitted the five already-completed files rather than
rewriting them. Because the evaluated plan listed those paths in
`intendedFiles` and did not list them in `noChangeFiles`, the AEP-051
completeness guard correctly failed closed:

```text
WorkflowExecution = workflowexecution-5ca67de8-31a5-5b37-98ff-f62ebab96ad2
TaskExecution = taskexecution-a10e67b2-aca5-5c0d-9931-04c7512fe726
Task = generate-patch:1.12.0
failure.class = CONFIGURATION
failure.retryable = false
failure.message = Code Generator omitted required planned files: [...]
```

This is not an AEP-052 provider-schema failure, a ContextPackage truncation, or
an incomplete model response. All three ModelInvocations in the workflow
succeeded. It is also not correct to weaken the omission guard: accepting a
missing required path without evidence would reintroduce partial and
unexplainable patches.

The contract needs two related guarantees. First, planning decisions about
whether a file must change must cite exact revision-bound evidence capable of
proving the relevant issue predicate. Second, when exact editable content
reveals that a planned path needs no mutation, the Plan-to-GeneratePatch
boundary needs an explicit, evidence-backed reconciliation outcome instead of
overloading omission as either success or failure.

Resolve the gap without giving Agents direct Repository Knowledge access,
dumping an unbounded repository into planning context, trusting a model's bare
`NO_CHANGE` assertion, or weakening revision, preimage, evaluation, and
publication controls.

## Reproduction

Provide a credential-free regression derived from issue #78. The fixture must
contain safe issue text, a bounded set of task documents, an execution plan,
expected context manifests, and expected runtime evidence. It must not contain
credentials, raw provider messages or headers, prompts from a live execution,
or unrelated webhook fields.

1. Create task documents that all mention implementation, manual testing, or
   MTP verification. Mark five historical tasks `Completed` and at least two
   genuinely pending tasks `In Progress`.
2. Create a matching `docs/execution-plan.md` whose task rows and dependency
   rows use the same statuses.
3. Normalize an issue equivalent to #78: update only tasks currently marked
   `In Progress` because their manual testing is now complete, and synchronize
   the execution plan.
4. Run the production AnalyzeIssue and BuildImplementationPlan context paths
   with the current relevance ranking and token limits.
5. Demonstrate the current failure by allowing ranked path metadata to select
   completed files that contain relevant terms without supplying path-bound
   content evidence proving their current status.
6. Produce an evaluated plan that marks those completed files as required
   `intendedFiles`, with none in `noChangeFiles`.
7. Materialize all planned paths as exact editable targets and return model
   changes only for files that actually need status updates.
8. Demonstrate that model invocation and schema validation succeed, then
   GeneratePatch fails with `Code Generator omitted required planned files`.
9. Demonstrate the corrected behavior: completed files are excluded before the
   plan becomes authoritative, or are reconciled through an explicit
   evidence-backed no-change disposition without weakening required changes.

The regression must inspect persisted ContextPackage provenance, plan-selection
evidence, per-path dispositions, EvaluationResults, and terminal TaskExecution
evidence. A fake model returning a predetermined path list is insufficient by
itself.

## Deliverable

Implement a bounded, evidence-backed planning and reconciliation contract that:

* distinguishes candidate relevance from proof that a candidate requires a
  change; relevance score or keyword overlap alone cannot authorize a required
  planned mutation;
* lets a Task declare file predicates needed for planning, such as an exact
  status field, required text, absent text, or another deterministic condition,
  without hard-coding task-document semantics into the general Context Builder;
* materializes only the bounded path content or deterministic slices needed to
  evaluate those predicates at the WorkflowExecution's immutable repository
  revision;
* records path, revision, content/preimage digest, source provenance, selected
  evidence range or structured field, predicate, result, and selection reason
  for every file admitted to or excluded from the required-change set;
* rejects or explicitly marks unsupported predicates when they cannot be
  evaluated from bounded evidence rather than guessing from filenames or
  relevance metadata;
* separates plan-authorized paths, required-change paths, and verified
  no-change paths in the public implementation-plan contract;
* requires every planned path to have one unambiguous disposition and forbids a
  path from appearing in conflicting required-change and no-change sets;
* extends the Code Generator output, or introduces an equivalent deterministic
  reconciliation boundary, so each planned path produces an explicit `CHANGE`
  or `NO_CHANGE` disposition rather than relying on array omission;
* accepts a late `NO_CHANGE` disposition only when exact freshly verified
  editable content proves every path-bound deterministic criterion is already
  satisfied and no required insertion, deletion, or status transition remains;
* fails closed when a model claims `NO_CHANGE` without supporting evidence,
  when evidence is stale or revision-mismatched, or when any required criterion
  remains unsatisfied;
* records whether a no-change decision originated in evaluated planning or was
  reconciled after exact editable-target materialization, without rewriting the
  immutable original plan artifact;
* creates a separate immutable reconciliation/evaluation record when the exact
  target evidence narrows the plan, preserving both original and effective
  path sets for audit;
* ensures patch evaluation, EvaluateAcceptance, and Publication Policy consume
  the effective reconciled dispositions and deny publication on missing,
  conflicting, unsupported, or unproven paths;
* preserves deterministic ordering, token accounting, content-addressed
  identities, revision binding, and the rule that Agents cannot retrieve
  repository knowledge directly;
* versions every changed Task, Agent, Prompt, Evaluation, Policy, Workflow,
  KnowledgeBase, schema, and exact Resource reference atomically; and
* updates Context Builder, Repository Intelligence, Workflow Runtime,
  evaluation, observability, Resource authoring, and self-hosting operations
  documentation to describe the same planning-evidence and reconciliation
  contract.

Do not resolve this task solely with stronger prompt wording, a larger context
budget, a retry, a list of special-case task filenames, or by deleting the
GeneratePatch missing-file check. Those approaches do not prove why a file
does or does not require a change.

## Dependencies

* AEP-016
* AEP-017
* AEP-025
* AEP-030
* AEP-031
* AEP-033
* AEP-040
* AEP-045
* AEP-048
* AEP-051

## Acceptance Criteria

* An issue #78-derived regression selects every genuinely `In Progress` task
  document and excludes all five already-completed historical files listed in
  the live failure, despite their similar manual-testing and MTP terminology.
* Every required-change selection cites exact repository evidence at the
  WorkflowExecution revision proving the current value and requested
  transition; path identity or relevance metadata alone cannot satisfy the
  requirement.
* Planning context remains bounded. It includes only the exact fields, slices,
  or files required to evaluate declared predicates and does not restore a
  complete `docs/tasks/` or repository dump.
* A missing, unreadable, binary, oversized, ambiguous, stale, duplicated, or
  revision-mismatched planning-evidence target fails closed with stable,
  non-sensitive evidence before the plan becomes authoritative.
* Deterministic predicates cover at least exact status-field equality, required
  text presence, required text absence, and unsupported semantic criteria.
  Identical issue, revision, Resources, and repository contents produce the
  same predicate results and plan-selection identity.
* `IMPLEMENTATION_PLAN` distinguishes authorized paths, required-change paths,
  planning-time no-change paths, and unsupported decisions. Schema evaluation
  rejects overlap, omission, unsafe paths, duplicates, and contradictory
  dispositions.
* Every exact editable target receives one terminal disposition. A generated
  change is preimage-bound; a no-change disposition is content-bound and
  contains deterministic proof that all applicable path criteria are already
  satisfied.
* The original evaluated implementation plan remains immutable. Any narrowing
  discovered from exact editable content creates correlated immutable
  reconciliation evidence containing the original set, effective set, reason,
  revision, target digest, and evaluator identity.
* The five already-completed files from issue #78 may be reconciled as
  evidence-backed no-change only if their exact pinned contents prove the
  requested status is already present. A bare model omission or assertion is
  rejected.
* A genuinely required task-file status transition omitted by the Code
  Generator still produces a terminal failure. The new reconciliation path
  cannot turn an incomplete patch into success.
* The existing AEP-051 destructive-rewrite, preimage, atomic mutation,
  required-insertion, disposition, change-compliance, and publication-denial
  regressions continue to pass.
* Patch Evaluation and EvaluateAcceptance fail when reconciliation evidence is
  missing, stale, conflicting, incomplete, or shows an unsatisfied required
  criterion, even if model output and repository tests otherwise pass.
* Publication Policy cannot allow `git.push` or `github.create_pr` until every
  planned path has a passing effective disposition and all required changes
  appear in the final revision-bound diff.
* Persisted runtime evidence and structured logs explain candidate selection,
  predicate evaluation, plan disposition, reconciliation, and terminal outcome
  without exposing full file bodies, prompts, model output bodies, credentials,
  or raw provider messages.
* Regression coverage includes Repository Knowledge, Context Builder,
  BuildImplementationPlan, schema evaluation, GeneratePatch, patch evaluation,
  EvaluateAcceptance, Publication Policy, deterministic harness, and dogfood
  runtime tests, followed by the complete `python -m pytest` and validation
  image verification gates.
* A new controlled MTP-09/MTP-10 issue equivalent to #78 completes all six
  Tasks and creates exactly one open, unmerged pull request whose diff updates
  only task documents genuinely requiring the transition and the corresponding
  execution-plan entries.
* GitHub issue #78 and WorkflowExecution
  `workflowexecution-5ca67de8-31a5-5b37-98ff-f62ebab96ad2` remain immutable
  historical evidence and are not replayed or rewritten.
* `README.md`, Context Builder and Repository Intelligence architecture,
  workflow-runtime and evaluation architecture, observability guidance, the
  self-hosting runbook, schemas, fixtures, Resource bundle, this task, and
  `docs/execution-plan.md` describe the same bounded planning-evidence,
  disposition, and reconciliation behavior.
