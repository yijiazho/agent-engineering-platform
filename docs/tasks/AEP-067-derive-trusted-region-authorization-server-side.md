# AEP-067: Derive Trusted Region Authorization Server-Side

**Status:** In Progress

## Context

AEP-066 replaced the requirement that a model emit one localized operation for
each literal required insertion with aggregate postimage reconciliation. That
change correctly accepts one safe multiline operation when its applied
postimage satisfies every authorized literal outcome. The next controlled
MTP-09/MTP-10 run exposed a separate, older model-shape constraint before
postimage reconciliation could run.

GitHub-triggered WorkflowExecution
`workflowexecution-abb41a25-92ae-5630-a4b6-55923ba8962d`, at repository
revision `69e225b313ab922c47c743566f6df2e83859e11a`, recorded:

```text
analyze-issue:1.4.0               SUCCEEDED
build-implementation-plan:1.10.0 SUCCEEDED
generate-patch:1.14.0            FAILED
run-validation:1.1.0             BLOCKED
```

GeneratePatch failed before creating a patch, Patch Evaluation, or
RunValidation TaskExecution. Its terminal evidence is
`taskexecution-643b5716-a6be-599a-9312-e24dedb3e5a3`, with failure:

```text
CONFIGURATION: REGION_MISMATCH: operation is not bound to the planned region
```

The immutable plan authorized `README.md` and the `Repository Layout` Markdown
section. The model supplied a revision-bound insertion with the correct
preimage digest, a unique anchor inside that section, and the requested
`deploy/` subtree. It declared
`regionId: readme-repository-layout:add-deploy`. The runtime derived the
trusted region name `Repository Layout` from planning evidence and rejected the
operation because those strings were not byte-for-byte equal.

The equality check was introduced by commit `2dc29b4` (`Add localized patch
application foundation`). Its apparent purpose was to bind a localized
operation to the plan-authorized region. At that point, however, it compared
only a model-controlled label with the trusted region name; it did not prove
the anchor actually lay within the region. Commit `2392e94` (`Address localized
patch safety review`) added the substantive authorization control: resolve the
trusted region against the immutable preimage and reject an anchor span outside
its bounds with `OUT_OF_REGION`.

The label equality check is therefore neither a reliable authorization proof
nor an outcome requirement from the triggering issue. It creates a false
failure whenever a model chooses a semantically descriptive identifier instead
of an exact internal selector name. Requiring a model to reproduce internal
identifier spelling also defeats the intent of localized, evidence-bound
generation: the control plane must authorize mutable scope from immutable plan
evidence, while the model proposes bounded edits within that scope.

The durable historical executions must remain unchanged:

* `workflowexecution-5f315522-c2d6-59ca-90d6-25d9d57ee7de` documents the
  superseded one-operation-per-insertion rejection; and
* `workflowexecution-abb41a25-92ae-5630-a4b6-55923ba8962d` documents the
  model-controlled region-label rejection.

Neither execution may be replayed or rewritten. A later controlled issue must
verify the corrected immutable Resource generation.

## Reproduction

Create credential-free unit and handler regressions with a revision-bound
`README.md` editable target containing one `Repository Layout` Markdown
section and at least one distinct section outside it.

1. Build trusted planning evidence that authorizes only `README.md` and the
   `Repository Layout` section.
2. Submit a localized insert with the matching path, revision, preimage digest,
   a unique anchor inside that section, and a noncanonical descriptive
   `regionId` such as `readme-repository-layout:add-deploy`.
3. Demonstrate the current equality guard rejects the operation before
   deterministic application even though the anchor span is in bounds.
4. Submit the same operation after the correction and require deterministic
   application, preservation evidence, and postimage reconciliation to pass.
5. Submit an otherwise identical operation whose anchor is in another Markdown
   section. Require `OUT_OF_REGION` before filesystem mutation, regardless of
   whether its declared region label matches, differs from, or is absent from
   the plan's display name.
6. Cover stale revisions and preimages, unsafe paths, missing or ambiguous
   anchors, overlapping and out-of-order operations, non-UTF-8 data, and
   unauthorized rewrite/delete behavior to prove that removing the label
   equality check does not relax other safety boundaries.
7. Exercise the complete deterministic GeneratePatch handler and verify that a
   genuine candidate scope or postcondition failure is represented as rejected
   candidate/evaluation evidence, rather than as `CONFIGURATION` unless the
   immutable Resource graph is actually inconsistent.

Fixtures must be deterministic and credential-free. Do not embed provider
requests, artifact bodies, secrets, or unrestricted logs.

## Deliverable

Implement a trusted-region authorization contract that:

* derives the authorized path, region selector, resolved region bounds, and
  plan-selection identity exclusively from immutable planning evidence in the
  control plane;
* treats model-provided `regionId` values as non-authoritative diagnostic data,
  or removes the field from the generated-operation schema when it has no
  consumer other than the redundant equality check;
* evaluates every non-rewrite localized operation's resolved anchor/edit span
  against the server-derived authorized region before materializing a
  postimage or issuing a filesystem write;
* persists the trusted plan-selection identity, region selector, resolved span,
  and model-declared label when present as structured operation evidence, so an
  operator can audit scope authorization without trusting the model label;
* retains immutable revision and preimage checks, normalized path authorization,
  unique-anchor requirements, span ordering and overlap checks, UTF-8 checks,
  preservation proof, postimage reconciliation, Patch Evaluation, and the
  existing rewrite/delete approval boundaries;
* rejects an anchor outside, or a region selector that cannot be resolved
  uniquely within, the trusted plan region with stable safe diagnostics before
  any mutation;
* distinguishes a model candidate rejected for scope, anchor, or postimage
  evidence from a malformed or inconsistent immutable Resource configuration;
* versions every changed Task, Agent, Prompt, Workflow, schema, fixture, and
  exact Resource reference atomically, preserving historical Resource graphs;
  and
* updates the localized-patch architecture, AEP-066 relationship, dogfood
  runbook, schemas, fixtures, and execution plan so documentation describes
  server-derived authorization rather than model-supplied region identity.

Do not implement this task by trusting a model label as authorization,
weakening the actual region-span check, silently normalizing labels into a
match, accepting an unresolved selector, or retrying the historical workflows.

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
* AEP-039
* AEP-040
* AEP-051
* AEP-053
* AEP-054
* AEP-055
* AEP-066

## Acceptance Criteria

* A localized operation with a noncanonical, absent, or mismatched
  model-provided region label is accepted only when the control plane proves
  its path, revision, preimage, unique anchor span, and resulting edit are
  within the immutable plan-authorized region.
* An operation with a canonical-looking or exact model region label but an
  anchor/edit span outside the trusted region fails with `OUT_OF_REGION` before
  postimage construction or filesystem mutation.
* A trusted region selector that is missing, ambiguous, unsupported, or cannot
  be resolved against the immutable preimage fails closed with a stable
  server-derived diagnostic; no model label can substitute for it.
* Runtime operation evidence records the immutable plan artifact/selection
  identity, trusted selector and resolved span, preimage and postimage digests,
  and any model-declared label separately. Ordinary inspection output remains
  free of source and artifact bodies.
* The aggregate-postimage behavior from AEP-066 remains intact: one safe
  multiline insertion may satisfy multiple required values, while missing
  required values fail deterministic reconciliation.
* Revision/preimage mismatch, unsafe path, missing or ambiguous anchor,
  out-of-order or overlapping spans, non-UTF-8 content, unauthorized
  replace/delete/rewrite, preservation failure, and unauthorized changed paths
  continue to fail closed with their existing stable diagnostics.
* Tests prove that the original `REGION_MISMATCH` equality condition cannot
  block an otherwise authorized candidate and that it cannot be bypassed to
  edit outside the trusted region.
* A model-originated candidate rejected by region bounds, anchor validation, or
  postimage reconciliation records a rejected-candidate evaluation/policy
  outcome. `CONFIGURATION` is reserved for an unusable or inconsistent
  immutable Resource graph.
* Unit, schema, fixture, deterministic harness, and complete `python -m pytest`
  coverage exercise the direct applicator, GeneratePatch handler, persisted
  operation evidence, failure classification, and resource-version graph.
* A new immutable self-hosting generation and one new controlled issue complete
  GeneratePatch for the README-layout scenario, then reach RunValidation. The
  two recorded failed workflows remain immutable diagnostic evidence and are
  not replayed.
* `README.md`, architecture and operator documentation, schemas, Resources,
  fixtures, AEP-066, this task, and `docs/execution-plan.md` describe the same
  server-derived trusted-region authorization contract.
