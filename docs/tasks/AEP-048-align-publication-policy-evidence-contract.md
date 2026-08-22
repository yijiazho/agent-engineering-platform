# AEP-048: Align Publication Policy Evidence Contract

**Status:** In Progress

## Implementation Status

Credential-free implementation is complete: the canonical evidence vocabulary
is enforced in production and the PolicyDecision schema; the self-hosting graph
is versioned as `publication-evidence:1.1.0` ->
`create-pull-request:1.2.0` -> `issue-to-pr:1.3.0`; semantic Resource and
production-path fake-provider regressions cover allow and fail-closed paths;
and Publication Policy now runs before Git mutation. The full local suite and
the source/published exact-image verification pass. Publishing the corrected
immutable generation and recording the controlled MTP-09/MTP-10 execution
remain operator actions, so this task is not yet complete.

## Context

Publication Policy is the final deterministic evidence gate before AEP may
commit, push, or create a pull request. The evaluator implemented by AEP-028
fails closed: it verifies persisted, revision-bound artifacts, evaluations,
and prior policy decisions; constructs a canonical evidence summary; and then
matches that summary against explicitly versioned `publication` Policy
Resources. AEP-034 separately applies `git.push` and `github.create_pr`
capability policy only after Publication Policy allows the candidate.

The controlled MTP-09/MTP-10 run for issue #63 reached this boundary after the
new AEP-047 validation gate passed. The correlated execution was:

```text
Event:              event-a3c87134-d0ad-5e11-babf-91233167b1a0
WorkflowExecution:  workflowexecution-cf824219-31b6-563f-b126-63cd2798f765
Trace:              trace-387deddb-d686-5186-8690-2f8d21412446
Repository revision: 33ec6f25daf9771bedb599efda5c35a8a5cc3330
PolicyDecision:     policydecision-4c7507734701bf69c6ea38aa
```

AnalyzeIssue, BuildImplementationPlan, GeneratePatch, RunValidation, and
EvaluateAcceptance all succeeded. Validation image readiness reported `PASS`,
and the offline build and complete repository test commands both exited `0`.
CreatePullRequest then failed before publication with:

```text
Publication Policy DENY: No applicable publication rule authorizes action
github.create_pr.
```

The denial contained no evidence failures. Its persisted evidence summary was:

```json
{
  "patchGenerated": true,
  "validationRan": true,
  "requiredArtifactsPresent": true,
  "requiredEvaluationsPresent": true,
  "allRequiredEvaluationsPassed": true,
  "noPriorPolicyViolation": true,
  "failures": []
}
```

However, `.ai/policies/publication-evidence.yaml` version `1.0.0` requires the
undeclared keys `allArtifactsValidated` and `allEvaluationsPassed`. The
evaluator produces neither key. JSON Schema condition evaluation therefore
matches zero rules, leaves `evaluatedRule` unset, and correctly defaults to
`DENY` even though all trusted evidence passed.

This drift escaped credential-free coverage for two reasons. Publication
Policy unit tests use synthetic Policies and already exercise the runtime key
`allRequiredEvaluationsPassed`, while CreatePullRequest tests use an
unconditional synthetic allow rule. Self-hosting bundle tests prove that the
Policy Resource is schema-valid and referenced, but do not evaluate the actual
repository Policy against the actual runtime evidence summary. The end-to-end
harness therefore does not prove that the versioned production rule can match
the evidence emitted by the production evaluator.

The failure is a Resource/runtime contract defect, not a reason to weaken the
fail-closed default, bypass Publication Policy, or replace the rule with an
unconditional allow. Terminal evidence for issue #63 must remain immutable;
redelivering its GitHub delivery must deduplicate rather than mutate or rerun
that failed WorkflowExecution.

## Reproduction

The live reproduction is the issue #63 execution recorded above. Inspect only
safe PolicyDecision metadata and require these observations:

* the action is `github.create_pr` and the referenced Policy is
  `publication-evidence:1.0.0`;
* `evidence.failures` is empty and every positive evidence field is `true`;
* `matchedRules` is empty and `evaluatedRule` is absent;
* the decision is `DENY` with the no-applicable-rule reason;
* the Docker ToolInvocation and build/test EvaluationResults succeeded; and
* no GitHub pull-request mutation or pull-request artifact was created.

Add a credential-free regression that loads the real repository Resources,
persists a same-workflow PATCH artifact, passing schema/patch/build/test and
acceptance EvaluationResults, and the required prior ALLOW decisions, then
invokes the production Publication Policy path for `github.create_pr`. Before
the fix, the actual `publication-evidence:1.0.0` rule must reproduce the empty
`matchedRules` denial. The regression must not substitute an unconditional
test Policy or rename evidence only inside the test fixture.

Also reproduce the boundary through CreatePullRequest with fake Git and GitHub
providers but the repository's real Task and Policy Resources. This proves the
same Resource resolution and condition matching used by the dogfood runtime
without another live issue or external mutation.

## Deliverable

Implement and enforce one canonical Publication Policy evidence contract that:

* defines the meaning and stable names of `patchGenerated`, `validationRan`,
  `requiredArtifactsPresent`, `requiredEvaluationsPresent`,
  `allRequiredEvaluationsPassed`, `noPriorPolicyViolation`, and `failures` at
  one authoritative production boundary rather than duplicating an
  unvalidated vocabulary in repository fixtures;
* updates the self-hosting publication rule to match the canonical runtime
  evidence and require all evidence necessary for an allowed
  `github.create_pr` action, including a generated patch, completed validation,
  present required artifacts and evaluations, passing required evaluations,
  and no prior denial;
* preserves `DENY` when no rule matches, any required evidence is missing or
  false, evidence belongs to another workflow or revision, a prior policy
  decision denies, or the action is not exactly `github.create_pr`;
* keeps Publication Policy separate from the subsequent `git.push` and
  `github.create_pr` Pre-Execution Capability Policy decisions and evaluates
  all three gates before their corresponding external mutations;
* versions `publication-evidence` and propagates its exact reference through
  CreatePullRequest, the `issue-to-pr` Workflow, deterministic inventory
  fixtures, and every other affected immutable Resource without introducing
  floating versions;
* adds a semantic self-hosting bundle check that evaluates the actual
  publication Policy against representative canonical evidence, rather than
  proving only that its JSON Schema shape is valid;
* adds a production-path CreatePullRequest integration test using the actual
  repository Policy Resource and fake providers, proving that valid evidence
  reaches both capability gates and creates exactly one fake PR;
* covers every false or missing canonical evidence field, an unknown action,
  prior denial, malformed conditions, and stale or cross-revision evidence so
  contract alignment cannot weaken fail-closed publication behavior;
* persists an explainable PolicyDecision containing the exact Policy version,
  evidence summary, matched/evaluated rule, repository revision, artifact and
  evaluation identities, and reason without artifact bodies or unrestricted
  logs; and
* updates Publication Policy, CreatePullRequest, self-hosting bundle, testing,
  and operator documentation to describe the same evidence vocabulary and
  versioned rule.

Prefer correcting the declarative Resource to the evaluator's established
canonical fields. If implementation instead changes the runtime vocabulary,
it must migrate schemas, all callers, persisted-evidence expectations,
fixtures, and documentation together and justify why that broader contract
change is safer. Do not emit compatibility aliases merely to make one stale
rule match.

## Dependencies

* AEP-028
* AEP-034
* AEP-037
* AEP-040
* AEP-047

## Acceptance Criteria

* One documented canonical evidence contract defines every Publication Policy
  summary field, its boolean/list semantics, and whether it is required for an
  allow decision. Production code, Resource conditions, tests, fixtures, and
  operator documentation use those exact names.
* The versioned self-hosting publication Policy matches
  `candidateAction.action == github.create_pr` only when `patchGenerated`,
  `validationRan`, `requiredArtifactsPresent`,
  `requiredEvaluationsPresent`, `allRequiredEvaluationsPassed`, and
  `noPriorPolicyViolation` are all true and `failures` is empty.
* A focused test loads the actual `.ai/policies/publication-evidence.yaml`
  Resource and proves that representative trusted passing evidence produces
  `ALLOW`, a non-empty `matchedRules`, and a populated `evaluatedRule` naming
  the expected Policy version and rule index.
* Parameterized negative tests set each required evidence field to false or
  omit it and prove publication is denied. Additional tests cover a non-empty
  failure list, an unsupported action, prior `DENY` and `REQUIRE_APPROVAL`,
  missing evidence, malformed conditions, unpersisted evidence, and
  workflow/repository-revision mismatch.
* CreatePullRequest integration coverage resolves the repository's real Task,
  publication Policy, and capability Policy Resources. With valid persisted
  evidence it authorizes Publication Policy, `git.push`, and
  `github.create_pr` in order and creates exactly one PR through fakes.
* The same integration test proves that a Publication Policy denial causes no
  commit, push, GitHub call, or `PULL_REQUEST_DESCRIPTION` artifact. Capability
  denial and ambiguous provider outcomes retain their existing idempotent,
  fail-closed behavior.
* Tests fail if the evaluator adds, removes, or renames a canonical evidence
  field without a matching versioned Resource and fixture update. Generic
  Resource schema validity alone is not sufficient evidence of compatibility.
* The corrected `publication-evidence` Resource receives a new semantic
  version. CreatePullRequest and `issue-to-pr` receive new versions for their
  changed exact references, and deterministic bundle fixtures and resolution
  tests name the complete consistent graph with no `latest` reference.
* PolicyDecision schema validation and structured lifecycle tests prove that
  the persisted decision retains safe correlation, exact evidence identities,
  matched rule, reason, and revision while excluding artifact bodies,
  credentials, webhook payloads, and unrestricted command output.
* `tests/test_publication_policy.py`, `tests/test_create_pull_request.py`,
  `tests/test_self_hosting_resource_bundle.py`, and
  `tests/test_mvp_harness.py` cover the corrected contract. The full local
  suite and `python deploy/validation/verify.py verify` exact-image gate pass.
* The self-hosting runbook's MTP-10 audit distinguishes evidence-integrity
  denial from a no-applicable-rule contract mismatch and prints only safe
  PolicyDecision metadata needed to diagnose either outcome.
* After credential-free validation and publication of one corrected immutable
  generation, one controlled MTP-09/MTP-10 execution reaches all six Tasks,
  records `ALLOW` for Publication Policy and both capability decisions, and
  creates exactly one open, unmerged PR. Replay of that delivery creates no
  second Event, WorkflowExecution, branch mutation, or PR.
* `README.md`, Publication Policy and Workflow Runtime architecture, the
  self-hosting runbook, schemas, Resources, deterministic fixtures, this task,
  and `docs/execution-plan.md` describe the same canonical evidence contract,
  version graph, fail-closed behavior, and live verification status.
