# AEP-083: Wire Model Selection Into Execution Budgets

**Status:** Not Started

## Context

AEP-062 adds model-agnostic hard budgets for invocations, input and output
tokens, elapsed time, and optional monetary cost. AEP-081 defines how one
eligible Model is selected from an ordered candidate policy. AEP-082 adds the
OpenRouter adapter. Those pieces are not sufficient until a candidate attempt
and its budget charge are the same durable decision.

The four Agent Resources under `.ai/agents/` currently select the same
`default-reasoning` Model. The desired behavior is that the Issue Analyzer,
Planner, Code Generator, and PR Writer may prefer different models for their
different jobs while remaining bound by one workflow budget. A fallback must
not reset consumption, and an exhausted budget must not silently pick a cheaper
model unless the policy explicitly allows it.

## Deliverable

Integrate selection and budget admission into the Task and Agent invocation
path, persist the linked decision and consumption evidence, and update the
repository Resource bundle so each Agent declares its own selection policy and
the issue-to-PR workflow declares explicit budgets.

## Dependencies

* AEP-010
* AEP-011
* AEP-012
* AEP-013
* AEP-040
* AEP-046
* AEP-058
* AEP-062
* AEP-081
* AEP-082

## Acceptance Criteria

* A TaskExecution attempt resolves its Agent selection policy, evaluates
  candidates against immutable constraints, and performs hard-budget preflight
  for each candidate before any ModelInvocation or provider request exists.
* The admission order is explicit and deterministic: resolve policy, filter
  candidates, check remaining budget for the next candidate, record the
  selection decision, atomically reserve budget, then dispatch. A failure in any
  step dispatches no provider call and leaves no ambiguous reservation.
* Budgets are anchored to the WorkflowExecution, TaskExecution attempt, or
  other AEP-062 scope, never to a selected Model reference. Switching models does
  not reset, split, or create a new aggregate budget.
* Every candidate attempt, including fallback, retry, and resumed work, is a
  distinct ModelInvocation that consumes the same documented workflow and task
  invocation, token, duration, and cost budgets.
* Model-level limits intersect with workflow and task budgets; the most
  restrictive applicable limit wins. A selection policy cannot widen a hard
  limit or exempt a candidate from it.
* With `budgetFallback: DENY`, a highest-priority candidate that cannot fit the
  remaining budget blocks the attempt with `BUDGET_INSUFFICIENT`; the system does
  not silently downgrade to a lower-priority model.
* With `budgetFallback: LOWER_PRIORITY_ONLY`, a lower-priority candidate may be
  selected only when it is eligible on every other constraint and fits the
  remaining budget. The decision records `BUDGET_CONSTRAINED` and each rejected
  higher-priority candidate with its minimum requirement.
* When no candidate fits, the attempt ends with the stable
  `BUDGET_INSUFFICIENT_FOR_ALL_CANDIDATES` blocked reason. The evidence includes
  each candidate's minimum requirement and the budget rule responsible.
* A monetary budget fails closed before dispatch when the selected candidate's
  cost is `UNAVAILABLE` or its pinned price source is missing. A budget that
  does not enforce monetary cost may proceed and must record cost as
  unavailable.
* Actual provider usage reconciles the reservation after each invocation. An
  overrun is recorded as budget evidence, is charged to the attempt, and blocks
  further governed work; it is never hidden by resetting a counter.
* Selection and reservation are atomic for a budget scope. Concurrent Tasks
  cannot both spend the last available invocation, token, or cost unit.
* Exact-limit behavior is well defined: consuming exactly the configured limit
  succeeds and leaves zero remaining; any next governed invocation is blocked.
* Retry and resume consumption follows the AEP-062 rule. A resumed execution
  reuses durable selection decisions only when the policy, candidate versions,
  price catalog, budget scope, and remaining budget are still valid; otherwise
  it records a new decision and re-checks admission.
* Each of `.ai/agents/issue-analyzer.yaml`, `.ai/agents/planner.yaml`,
  `.ai/agents/code-generator.yaml`, and `.ai/agents/pr-writer.yaml` declares an
  explicit `modelSelectionRef` and is version-bumped. Their policies express
  different capability, context, quality, and cost priorities. At least two
  Agents select different Models for the same workflow inputs under normal
  budget conditions.
* The repository bundle adds the referenced `.ai/model-selection/*.yaml`
  policies and any OpenRouter-backed `.ai/models/*.yaml` candidates with
  explicit versions, capability metadata, and pinned pricing where a monetary
  budget or cost ceiling applies. `latest` is never used.
* `.ai/workflows/issue-to-pr.yaml` declares a workflow-level execution budget,
  and the cognitive Tasks declare applicable task-level limits where they are
  narrower. The existing `inputContextTokenBudget` remains independent of
  output-token and aggregate workflow budgets.
* Selection and budget evidence remain linked to the same TaskExecution and
  AgentInvocation, and replay reuses the recorded decision and consumption
  unless an explicit override creates new evidence.
* Tests cover exact-limit success, pre-invocation rejection, post-invocation
  exhaustion, per-Agent policy differences, fallback consuming budget,
  budget-denied fallback, no eligible candidate, missing cost metadata,
  concurrent reservation, overrun recording, retry accounting, resume reuse,
  and failure to widen a higher-scope limit.
* Documentation includes bounded issue-to-PR and evaluation-suite examples that
  show per-Agent selection, budget scope, fallback behavior, and the stable
  blocked reasons.

## Out Of Scope

* Implementing AEP-062's budget ledger or inspector itself.
* New provider adapters beyond OpenRouter.
* Automatic model promotion from ExperimentResult or EvaluationSuite scores.
* Live price lookup or provider-driven candidate ordering.
* Cross-registration budget pooling.

## Implementation Notes

Treat the selection decision and the budget reservation as two linked records
written in the same admission step. If the active runtime store cannot provide
an atomic reserve-and-record operation, serialize admission for the affected
budget scope rather than allowing a race. Do not compute remaining budget from
mutable provider responses alone. The `.ai` bundle should remain credential-free:
model slugs, capability metadata, and prices are declarative; API keys stay in
environment-referenced secret files.

## Design References

* [AEP-062: Add First-Class Execution Budgets](AEP-062-add-first-class-execution-budgets.md)
* [AEP-081: Define The Deterministic Model Selection Contract](AEP-081-define-deterministic-model-selection-contract.md)
* [Workflow Runtime](../architecture/workflow-runtime.md)
* [Small-Scale Deployment](../architecture/small-scale-deployment.md)
