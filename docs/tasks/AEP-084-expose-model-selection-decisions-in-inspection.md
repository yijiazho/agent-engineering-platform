# AEP-084: Expose Model Selection Decisions In Execution Inspection

**Status:** Not Started

## Context

AEP-056 provides execution inspection and explanation over persisted runtime
objects. AEP-062 requires that inspection show configured limits, consumed and
remaining amounts, and the budget rule responsible for a blocked Task. With
AEP-081 and AEP-083, operators also need to see why a particular model ran, why
another was rejected, what it cost, and whether the provider actually used the
selected model.

Without a first-class view, the selection decision exists only as a runtime
object. Operators would have to correlate ModelSelectionDecision, ResolvedAgent,
AgentInvocation, ModelInvocation, and budget records manually, which defeats the
explanation contract.

## Deliverable

Extend the AEP-056 inspection CLI and its machine-readable output to render
model selection decisions, candidate rejection reasons, budget admission
evidence, and requested versus selected versus actual provider identity. Keep
the default output redacted and deterministic.

## Dependencies

* AEP-056
* AEP-057
* AEP-062
* AEP-081
* AEP-082
* AEP-083

## Acceptance Criteria

* `show` for a TaskExecution, AgentInvocation, or WorkflowExecution includes the
  linked `ModelSelectionDecision` when one exists: policy reference, ordered
  candidates, selected Model reference, selection reason, and price-catalog
  identity.
* Every rejected candidate is displayed with its stable reason and the
  constraint that rejected it. The output distinguishes capability, context
  window, provider restriction, missing price, and budget insufficiency.
* `explain` renders the causal chain from selection policy through budget
  admission, reservation, ModelInvocation, actual provider usage, and
  post-invocation reconciliation.
* The view shows requested Model reference, selected Model reference, upstream
  provider model identity, and any divergence between them. A hidden or
  unreported provider substitution is visible as evidence rather than inferred.
* Budget display includes each configured limit, consumed amount, remaining
  amount, and the stable budget rule that blocked or allowed the attempt. It
  covers invocation, input token, output token, duration, and cost dimensions
  where configured.
* Monetary cost display includes its fidelity label (`EXACT`, `CONFIGURED`,
  `ESTIMATED`, or `UNAVAILABLE`) and never renders unavailable cost as zero.
* Selecting a lower-priority candidate because of budget is explained as
  `BUDGET_CONSTRAINED`; blocking because no candidate fits is explained as
  `BUDGET_INSUFFICIENT_FOR_ALL_CANDIDATES`.
* Historical executions without a selection decision render an explicit
  `NOT_RECORDED` state rather than implying static selection or fabricating a
  decision.
* Default human-readable and JSON output omit prompts, prompt bodies, source
  bodies, credentials, raw provider request or response bodies, and raw
  provider error bodies. Existing unsafe-debug behavior remains explicit and
  separately gated.
* Field names, candidate ordering, and reason codes are stable and documented.
  The same persisted evidence produces byte-identical machine output apart from
  explicit volatile fields such as generation timestamps.
* The view works for successful selection, no eligible candidate, budget
  blocked, provider fallback, retry, resume, replay, and missing cost metadata.
* Tests cover an ordered multi-candidate selection, a rejected candidate for
  each constraint class, a budget-constrained lower-priority choice, a
  no-eligible-candidate block, requested versus actual model divergence,
  unavailable cost, a historical record without selection evidence, replay
  reuse, and redaction of sensitive fields.
* Documentation shows example `show` and `explain` output for an issue-to-PR
  execution with two Agents using different candidate policies.

## Out Of Scope

* New selection or budget semantics.
* Live provider queries or price lookups during inspection.
* Mutating or re-selecting a model from the CLI.
* Cross-registration access control changes.

## Implementation Notes

Read only from immutable runtime evidence. Resolve linked objects through their
existing IDs rather than re-deriving selection from current Resources, because
Resources may have advanced since the execution. Keep the selection section
additive so AEP-056 output consumers continue to work. Reason codes shown to
operators must match the persisted codes from AEP-081 and AEP-083, not a
separate presentation vocabulary.

## Design References

* [AEP-056: Build Execution Inspection and Explanation CLI](AEP-056-build-execution-inspection-and-explaination-cli.md)
* [AEP-062: Add First-Class Execution Budgets](AEP-062-add-first-class-execution-budgets.md)
* [Workflow Runtime](../architecture/workflow-runtime.md)
* [Observability](../architecture/observability.md)
