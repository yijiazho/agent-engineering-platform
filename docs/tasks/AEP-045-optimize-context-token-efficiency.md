# AEP-045: Optimize Context Token Efficiency

**Status:** In Progress

The deterministic implementation, regression fixture, Resource versions, and
local validation are complete. The credentialed MTP-09/MTP-10 rerun remains an
operator action and is required before this task can be marked Completed.

## Context

The controlled self-hosting pilot reached the `AnalyzeIssue` Task but failed
before Agent or Model invocation with this terminal TaskExecution failure:

```text
mandatory context requires approximately 121421 tokens, exceeding budget 32000
```

The failure is reproducible against repository revision
`57129d977d5109575f61b255c8e4fce72d6fd6be` with the narrowly scoped issue
"Normalize invalid durable runtime checkpoint object failures." The issue names
only `src/aep/runtime_store.py` and `tests/test_runtime_store.py` as allowed
paths, so its minimum sufficient context should not require a repository-scale
prompt.

The estimate is consistent with the context currently selected. A deterministic
reconstruction at that revision produced this approximate breakdown before the
complete GitHub issue metadata was included:

| Mandatory source | Elements | Estimated tokens |
| --- | ---: | ---: |
| Repository inventory | 350 | 73,545 |
| Knowledge base | 170 | 35,011 |
| Candidate files | 20 | 4,323 |
| Documentation | 20 | 4,032 |
| Task and abbreviated Event | 2 | 557 |
| Total | 562 | 117,468 |

`Task/analyze-issue:1.0.1` declares `event`, `issue`,
`repository-inventory`, `documentation`, `candidate-files`, and `knowledge` as
required context. `ContextBuilder._query_repository()` resolves
`repository-inventory` with `CandidateFileQuery(limit=None)`. The declared
`KnowledgeBase/aep-repository:1.0.0` independently resolves unbounded
repository sources under `schemas/`, `src/`, and `tests/`, plus broad
documentation sources. Results are deduplicated within one KnowledgeBase query
but not across repository inventory, candidate files, documentation, and
knowledge. At the observed revision, 113 file identities occurred in both the
repository inventory and knowledge results, 12 documentation identities
occurred in both documentation and knowledge, and candidate results could be
repeated again.

Each repeated result carries useful but verbose revision, snapshot, source, and
traversal provenance. The resulting package is therefore dominated by repeated
file inventory and provenance metadata rather than issue-specific repository
evidence. The current estimator, canonical UTF-8 JSON bytes divided by four,
is approximate but accurately exposes the order of magnitude of the assembled
input. Replacing it with a larger budget would mask the selection problem.

There is also a contract divergence between input and output limits. The
`AnalyzeIssueTaskHandler` supplies a hard-coded 32,000-token Context Builder
budget, while `Model.spec.tokenLimit` is passed by the OpenAI adapter as
`max_output_tokens`. A Task's input-context budget must be explicit and must not
be inferred from or changed by a Model's output-token allowance.

Resolve the divergence without allowing Agents to retrieve repository knowledge
directly, dropping required provenance, weakening immutable revision binding,
or making the self-hosting workflow depend on a provider-specific tokenizer.

## Reproduction

Run MTP-09 and the MTP-10 runtime audit from
`docs/operations/self-hosting-dogfood.md` using one newly opened issue that has
the `dogfood` label at creation time. The failure is represented in the runtime
object store, not the webhook reconciliation-failure table: the reconciliation
outbox becomes `COMPLETED`, `reconciliation_failures` remains empty, and the
first TaskExecution records the non-retryable `CONFIGURATION` failure above.

The focused implementation loop must also provide a credential-free regression
fixture that represents the same narrow issue and repository layout. Run at
least:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_context_builder.py
.\.venv\Scripts\python.exe -m pytest tests/test_analyze_issue.py
.\.venv\Scripts\python.exe -m pytest tests/test_self_hosting_resource_bundle.py
.\.venv\Scripts\python.exe -m pytest tests/test_dogfood_runtime.py
```

The implementation must not require a second live GitHub issue merely to
measure the corrected package. Use deterministic local tests before resuming
the existing controlled pilot procedure.

## Deliverable

Implement a deterministic, token-efficient context-selection contract and
update the self-hosting Resource bundle by:

* adding an explicit Task-level input-context budget and optional-context
  declaration, or an equivalently clear declarative contract, so Context
  Builder input limits are versioned independently from Model output-token
  limits;
* removing the handler-only hard-coded budget as the source of truth and
  validating every configured context budget as a positive bounded integer;
* revising the AnalyzeIssue context requirements so the normalized Event,
  issue, and a bounded relevant candidate set remain mandatory while broad
  inventory, documentation, and knowledge cannot force every repository record
  into the package;
* applying deterministic relevance filters and explicit limits to repository,
  documentation, and KnowledgeBase retrieval, including KnowledgeBase sources
  that currently use `limit=None` and ignore issue search terms;
* deduplicating the same repository knowledge identity across requirement
  categories before token accounting while retaining explainable provenance and
  every selection reason that caused the surviving element to be included;
* preserving stable ordering, immutable revision and knowledge-snapshot
  binding, required-context validation, optional-candidate pruning evidence,
  and deterministic ContextPackage identity;
* recording a safe per-category token and element-count breakdown that can be
  asserted in tests and inspected operationally without logging issue bodies,
  prompts, source bodies, credentials, or other sensitive content;
* versioning every changed Task, Model, KnowledgeBase, Agent, Workflow, or other
  `.ai/` Resource and updating all exact references and deterministic inventory
  fixtures in the same change;
* adding regression tests for the observed 121,421-token failure, cross-category
  duplicate selection, deterministic pruning and ordering, and the separation
  of input-context budget from `max_output_tokens`; and
* updating Context Builder architecture, Resource documentation, self-hosting
  operations, schemas, fixtures, and contributor guidance wherever the public
  configuration or observable runtime evidence changes.

Do not satisfy this task solely by increasing 32,000 to a value above the
observed estimate. A larger provider context window may be used as headroom,
but the selected ContextPackage must remain minimum, relevant, bounded, and
provider-neutral.

## Dependencies

* AEP-016
* AEP-017
* AEP-029
* AEP-040
* AEP-042

## Acceptance Criteria

* A deterministic regression representing the controlled issue builds an
  AnalyzeIssue ContextPackage at or below 32,000 estimated input tokens instead
  of failing near 121,421 tokens.
* The corrected package contains the normalized issue, the AnalyzeIssue Task
  contract, and relevant repository evidence identifying
  `src/aep/runtime_store.py` and `tests/test_runtime_store.py`; it does not
  include the complete repository inventory merely because broad repository or
  KnowledgeBase context is declared.
* No repository knowledge identity is emitted more than once across repository
  inventory, candidate-files, documentation, and knowledge categories unless
  the entries represent explicitly different source slices. A surviving
  deduplicated element retains all applicable selection reasons and valid
  provenance.
* Every unbounded Context Builder query used by the self-hosting AnalyzeIssue
  path is replaced by a documented deterministic bound or a compact aggregate
  representation whose size is independently bounded.
* Required context still fails closed when genuinely missing. Optional or
  lower-priority context is deterministically pruned when the configured input
  budget is exhausted, and discarded candidates retain safe reason and token
  estimate metadata.
* The Task-level input-context budget is explicit in the Resource contract and
  is consumed by the Context Builder. Changing a Model output-token limit does
  not silently change the ContextPackage budget, and changing the input budget
  does not change the OpenAI `max_output_tokens` request field.
* Token accounting remains deterministic and provider-neutral. Tests verify
  aggregate and per-category estimates, stable selection order, and identical
  ContextPackage identity for identical inputs.
* ContextPackage elements remain provenance-complete and bound to the exact
  WorkflowExecution repository revision and knowledge snapshot. Agents gain no
  repository or knowledge-provider access.
* The versioned self-hosting Resource graph loads without floating references,
  and `tests/test_self_hosting_resource_bundle.py` verifies the revised context
  contract, exact Resource versions, and expected inventory.
* Focused Context Builder, AnalyzeIssue, Resource bundle, and dogfood runtime
  tests pass, followed by the complete `python -m pytest` suite.
* Re-running the controlled MTP-09/MTP-10 path with an authorized issue creates
  one bounded ContextPackage, proceeds to Agent and Model invocation, and no
  longer fails AnalyzeIssue with `ContextBudgetExceededError`. Any later Task or
  provider failure remains separately diagnosable and does not create a second
  Event or WorkflowExecution.
* `README.md`, `docs/architecture/context-builder.md`,
  `docs/operations/self-hosting-dogfood.md`, schemas, fixtures, this task, and
  `docs/execution-plan.md` describe the same final budget, selection, evidence,
  and Resource-version behavior.
