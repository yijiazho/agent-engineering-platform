# AEP-062: Add First-Class Execution Budgets

**Status:** Completed

## Context

AEP already constrains context size and coordinates model-provider rate limits, but autonomous workflows also need deterministic limits on total resource consumption. Without first-class budgets, retries or multi-step model workflows can exceed acceptable cost, token, invocation, or duration limits even when each individual Task is valid.

Budget enforcement belongs in the control plane rather than prompts. An Agent must not be asked to voluntarily stop spending, and provider-specific usage accounting must not determine whether the platform enforces a declared hard limit.

## Deliverable

Add declarative workflow- and task-level execution budgets for model invocations, input/output tokens, elapsed time, and monetary cost where reliable provider pricing/usage data is available. Persist budget consumption and deterministic enforcement decisions as runtime evidence.

Define inheritance and precedence rules between Workflow, Task, Agent/Model, and execution-level limits without conflating Context Builder input budgets with Model output-token limits.

## Dependencies

* AEP-010
* AEP-011
* AEP-014
* AEP-042
* AEP-045
* AEP-046
* AEP-051
* AEP-056
* AEP-058

## Acceptance Criteria

* Resource schemas support explicit positive bounded limits for maximum model invocations, input tokens, output tokens, elapsed duration, and optional monetary cost at documented scopes.
* Budget inheritance and override precedence are deterministic, validated at load time, and documented with no floating or implicit provider defaults used as AEP policy.
* The scheduler/model invocation path checks remaining hard budget before starting an invocation whose known minimum requirements would exceed the limit.
* Actual provider usage updates durable budget-consumption evidence after each invocation, including the provider/model identity and whether cost is exact, configured, estimated, or unavailable.
* Exhausted hard budgets prevent additional governed work with a stable terminal or blocked reason; the system does not ask an Agent to decide whether to ignore the budget.
* Retry and resume operations consume and enforce the appropriate workflow-attempt budget according to an explicit documented rule.
* Input-context budgets remain independent from Model output-token limits and from aggregate workflow token/cost budgets.
* AEP-056 shows configured limits, consumed amounts, remaining amounts, and the budget rule responsible for a blocked task.
* Tests cover exact-limit success, pre-invocation rejection, post-invocation exhaustion, nested scope precedence, missing cost metadata, retry/resume accounting, and provider usage normalization.
* Documentation includes examples for bounded issue-to-PR and evaluation-suite executions.

## Implementation State

Workflow, Task, and Agent schemas now accept the shared `executionBudget`
definition, and Model Resources accept optional pinned USD pricing. The
`aep.execution_budgets` module resolves Workflow, Task, Agent, Model, and
execution-level limits by intersection, rejects widening declarations at
Resource load time, and persists reservations, consumption, and stable
decisions on WorkflowExecution, TaskExecution, AgentInvocation, and
ModelInvocation evidence. `aep executions show` renders configured, consumed,
reserved, and remaining amounts, while `aep explain` reports
`BUDGET_BLOCKED` with the responsible scope, dimension, Resource, and rule.

Retries reuse Task-identity consumption, resume continues the same
WorkflowExecution ledger, and replay starts a new ledger while preserving only
explicit execution-level overrides. Provider retry policies are clamped to the
remaining invocation allowance and the full granted attempt allowance is
reserved before dispatch, bounded by the configured retry policy and the
remaining invocation, input-token, output-token, and monetary allowances, so
concurrent Tasks cannot over-admit provider attempts or hold unrelated units.
Each granted provider attempt receives a bounded share of the remaining output
budget, and monetary budgets reserve the priced output allowance even without
an aggregate output-token limit. When the Model omits `tokenLimit`, a finite
per-attempt output cap is derived from the remaining monetary allowance, or
admission is rejected if no safely priced bound exists. Cost comparisons use
normalized fixed-point arithmetic. Admission,
immutable execution-budget initialization, and reconciliation run as one atomic
workflow/task budget transaction, serialized by the durable store's
cross-process writer fence. Deferred requests and pre-transport adapter validation failures with zero
dispatched attempts release their reservation without charging an invocation.
Dispatched failures with unavailable usage retain only the reservation for
their unreconciled attempts; unused granted attempts are released, and a
successful final attempt replaces its reservation with actual usage. Elapsed
reconciliation captures a post-dispatch completion timestamp, including failed
invocations, so model latency counts against the elapsed budget. Task ceilings do not consume the shared Agent
ledger, execution overrides are validated against declared limits before
narrowing admission, and temporary reservation exhaustion is reevaluated after
reconciliation. Pre-dispatch pricing rejections are local rather than sticky.
Invocation-only budgets work without a Model output-token limit. Unreconciled
monetary spend, including earlier failed attempts hidden by a later retry
success, blocks further cost-budget work. Context Builder input budgets remain
a separate contract. Credential-free tests cover exact-limit success,
pre-invocation rejection, post-invocation exhaustion, nested precedence,
missing cost metadata, retry accounting, provider usage normalization, and
inspection. Bounded issue-to-PR and evaluation-suite examples are documented in
[Workflow Runtime](../architecture/workflow-runtime.md#81-execution-budgets).
