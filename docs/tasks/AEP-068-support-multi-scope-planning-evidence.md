# AEP-068: Support Multi-Scope Planning Evidence

**Status:** Not Started

## Context

The controlled MTP-09/MTP-10 retry at repository revision `e76bf823882a673bc93163767b6809cd4b0362de` reached a new planning boundary. WorkflowExecution `workflowexecution-31c5a968-a84d-51de-98d6-bd37faee99ad` completed AnalyzeIssue but failed BuildImplementationPlan before GeneratePatch could run:

```text
analyze-issue:1.4.0               SUCCEEDED
build-implementation-plan:1.10.0 FAILED
generate-patch:1.15.0            BLOCKED
run-validation:1.1.0             BLOCKED
```

The failed TaskExecution is `taskexecution-22ed3c86-0f68-5523-8662-2985e4615c8d`; it recorded:

```text
CONFIGURATION: planning-evidence target 'README.md' has inconsistent region selectors
```

The schema-valid Issue Analyzer output expressed a normal request with multiple criterion scopes for `README.md`:

* Four `TEXT_ABSENT` to `TEXT_PRESENT` transitions for the requested `deploy/` subtree, each scoped to `MARKDOWN_SECTION: Repository Layout`.

* Two whole-file (`region: null`) `UNSUPPORTED_SEMANTIC` declarations for preserving surrounding guidance and satisfying `git diff --check`.

This is not conflicting authorization. The first group describes the only permitted mutation region. The second group describes preservation and formatting outcomes that a deterministic patch or validation evaluator owns; they must not grant authority to edit outside the Repository Layout section.

The current Context Builder groups all planning predicates by path and stores a single `inspection.region` on one trusted planning-evidence record. It then requires every declaration for that path to have identical region values. Consequently, the valid sequence `Repository Layout, Repository Layout, Repository Layout, Repository Layout, null, null` fails before trusted planning evidence, a plan artifact, or any mutation exists.

The one-selector-per-path check was introduced in commit `84303b0` (`Bind multi-insertion acceptance criteria`); nullable region declaration support was extended in `82e71c2` (`Address AEP-055 review feedback`). The AnalyzeIssue schema permits nullable selectors but the later aggregation step cannot represent them. The error is therefore a contract mismatch, not an unusable immutable Resource graph. AEP-067 separately ensures localized operations are authorized from server-derived spans rather than model labels; this task must preserve that property.

Preserve WorkflowExecution `workflowexecution-31c5a968-a84d-51de-98d6-bd37faee99ad` and its linked TaskExecutions as immutable diagnostic evidence. Do not replay or rewrite it. A new controlled issue must verify a corrected immutable generation.

## Reproduction

Create deterministic, credential-free declarations for a revision-bound `README.md` target with a `Repository Layout` section and a separate section outside it.

1. Declare literal insertion predicates and postconditions scoped to `MARKDOWN_SECTION: Repository Layout`.

2. For the same path, declare a whole-file preservation or formatting criterion with `region: null`.

3. Demonstrate that current path-level aggregation rejects the declarations although the schema accepts both selector forms.

4. After the correction, persist separate trusted evidence identities for the mutation region and whole-file criterion/evaluator evidence.

5. Propose an operation inside the Repository Layout span and prove it proceeds to deterministic application and aggregate postimage reconciliation.

6. Propose an otherwise identical operation outside that span and prove it fails before mutation; the whole-file criterion must not authorize it.

7. Cover two explicitly authorized editable regions, multiple whole-file criteria, missing or ambiguous selectors, stale revision/digest, missing or ambiguous anchors, and unsupported criteria with no owning evaluator.

Fixtures must be deterministic and credential-free. Do not store provider requests, source or artifact bodies, credentials, or unrestricted logs.

## Deliverable

Implement a planning-evidence and plan-authority model that:

* represents trusted predicate evidence at `(path, scope)` granularity rather than requiring one selector for every declaration attached to a path;

* derives each scope, selection identity, revision, complete-content digest, inspection result, and postcondition solely from immutable repository and planning evidence;

* retains distinct trusted selections for one or more editable regions in a path, and permits localized mutation only where the server derives a matching trusted region span;

* treats `region: null` as non-authorizing for localized mutation unless an independently reviewed Resource explicitly grants whole-file rewrite power;

* routes preservation, path-boundary, diff-format, build, and test criteria to their owning deterministic evaluator rather than requiring a localized planning record to represent them;

* fails closed for malformed, missing, duplicate, unsupported, or ambiguous selectors and for criteria with neither deterministic evidence nor an owning evaluator;

* retains AEP-055 insertion ownership and AEP-066 aggregate postimage proof, while supplying AEP-067 with every trusted editable-region identity it needs;

* makes planner and Context Builder contracts explicit about criteria that authorize edits, verify postimages, or are evaluator-owned requirements;

* persists safe criterion evidence: path, scope class, trusted selection ID, owning evaluator, result, and authorization role, without source bodies; and

* versions affected Tasks, Agents, Prompts, Evaluations, schemas, fixtures, Workflows, and exact Resource references atomically with synchronized docs.

Do not implement this task by treating `region: null` as implicit whole-file write authority, dropping semantic criteria silently, weakening trusted-region or anchor-span checks, making the model select authoritative scopes, or replaying the failed workflow.

## Dependencies

* AEP-002
* AEP-013
* AEP-017
* AEP-018
* AEP-025
* AEP-026
* AEP-029
* AEP-030
* AEP-031
* AEP-033
* AEP-039
* AEP-040
* AEP-051
* AEP-053
* AEP-054
* AEP-055
* AEP-066
* AEP-067

## Acceptance Criteria

* One path may have a region-scoped editable criterion and `region: null` preservation, formatting, build, or test criteria without an inconsistent-selector failure.

* Trusted planning evidence persists separate scope/selection identities for `README.md`'s Repository Layout mutation scope and whole-file evaluator-owned criteria; inspection exposes no source body.

* A whole-file criterion cannot expand localized mutation authority. An anchor outside every trusted editable region fails before postimage construction or filesystem mutation.

* Two separately authorized regions in one path each support in-bounds operations, while crossing, overlapping, or out-of-region operations fail closed with stable diagnostics.

* Missing, duplicate, malformed, unsupported, or ambiguously resolved regions remain distinct fail-closed outcomes. A model label or anchor cannot replace trusted evidence.

* AEP-066 aggregate proof remains exact: one safe multiline operation may satisfy multiple required values, while a missing value fails reconciliation.

* Preservation, changed-path boundaries, `git diff --check`, build, and test requirements receive deterministic evaluator ownership. Genuinely unsupported requirements have explicit blocking evidence and are not silently satisfied.

* AnalyzeIssue, Context Builder, BuildImplementationPlan, GeneratePatch, Patch Evaluation, and inspection tests cover mixed scopes, multi-region paths, whole-file non-authority, classification, and evidence provenance.

* A schema-valid mixed-scope candidate that cannot be evaluated records a decisive candidate/evaluation result, not `CONFIGURATION` unless immutable Resources are inconsistent or unavailable.

* A new immutable self-hosting generation and controlled issue complete BuildImplementationPlan and GeneratePatch, then reach RunValidation without whole-file mutation authority.

* WorkflowExecution `workflowexecution-31c5a968-a84d-51de-98d6-bd37faee99ad` remains immutable historical evidence and is not replayed or rewritten.

* AEP-055 through AEP-067, architecture/operator docs, schemas, Resources, fixtures, this task, and `docs/execution-plan.md` describe the same contract.
