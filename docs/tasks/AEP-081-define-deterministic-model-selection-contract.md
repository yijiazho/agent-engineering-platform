# AEP-081: Define The Deterministic Model Selection Contract

**Status:** Not Started

## Context

All four Agent resources under `.ai/agents/` currently bind
`Model/default-reasoning@1.1.0`, whose Model declares `provider: openai` and
`model: gpt-5`. AEP-042 made the live adapter provider-specific and AEP-046
coordinates one provider's rate limits. AEP-062 will add hard execution
budgets, but it deliberately treats a Model as an already-resolved, stable
identity.

Operators want different Agents to be able to use different models and
eventually route through OpenRouter. If selection happens inside a prompt, from
live price or availability, or through an adapter's hidden fallback, AEP cannot
answer four required questions:

* which model was allowed to run,
* why it was chosen over another,
* which budget or scope was charged,
* whether replay reproduces the decision.

Selection is a control-plane decision. It must be declarative, deterministic,
recorded before dispatch, and independent of Agent output. The contract must
also preserve today's static single-Model behavior for Agents that do not opt
in.

## Deliverable

Add a versioned `ModelSelectionPolicy` Resource, optional Model capability and
pricing metadata, an optional Agent binding to the policy, and a
`ModelSelectionDecision` runtime object with deterministic resolution and
load-time validation. This task defines the contract and resolution only; it
does not add a provider adapter, budget enforcement, or CLI rendering.

## Dependencies

* AEP-002
* AEP-012
* AEP-014
* AEP-042
* AEP-046
* AEP-052
* AEP-056
* AEP-057
* AEP-062

## Acceptance Criteria

* `ModelSelectionPolicy` is a versioned Resource with an exact-version, ordered
  `candidates` list of Model references. At least one candidate is required,
  duplicate references are rejected, and `latest` is rejected.
* A policy declares the constraints used for selection: required capabilities,
  minimum context window, maximum input and output tokens, optional maximum
  monetary cost per invocation, allowed and denied providers, and explicit
  budget-fallback behavior (`DENY` or `LOWER_PRIORITY_ONLY`). The default is
  `DENY`.
* A policy that declares a monetary constraint must reference a versioned,
  pinned price catalog or carry complete declarative pricing. It must not
  require a live provider price lookup at selection time.
* The Model Resource schema supports optional capability metadata
  (`contextWindow`, `maxOutputTokens`, `structuredOutput`, `toolCalling`,
  `reasoning`) and optional pricing metadata (input and output price per
  million tokens, currency, effective time, and source/version identity).
  Existing Models without these fields remain valid when no policy requires
  them.
* The Agent Resource supports optional `spec.modelSelectionRef`.
  `spec.modelRef` remains required as the declared default and must appear in
  the policy's candidate set. An Agent without `modelSelectionRef` resolves
  exactly one Model exactly as it does today.
* Resolution is deterministic. Candidates are filtered by immutable
  constraints in declared order; the first eligible candidate wins. No runtime
  score, price sort, random choice, provider availability, latency observation,
  or Agent output may reorder candidates.
* Every rejected candidate records a stable reason such as
  `CAPABILITY_MISSING`, `CONTEXT_WINDOW_TOO_SMALL`, `PROVIDER_DISALLOWED`,
  `PRICE_UNAVAILABLE`, `BUDGET_INSUFFICIENT`, `MODEL_CONFIGURATION_INVALID`, or
  `DUPLICATE_CANDIDATE`.
* Higher scopes may only narrow a selection. Workflow, Task, Agent, and
  execution constraints are intersected; no lower scope can widen a hard limit
  or a provider restriction declared above it.
* Selection is computed before any ModelInvocation or provider request. When no
  candidate qualifies, resolution returns
  `MODEL_SELECTION_NO_ELIGIBLE_CANDIDATE` with per-candidate reasons and
  dispatches no invocation.
* A `ModelSelectionDecision` runtime object records the policy reference, the
  ordered candidates, evaluated constraints, rejected candidates and reasons,
  selected Model reference, selection reason, price-catalog identity, referenced
  budget snapshot when supplied, and creation time. It is immutable once
  terminal.
* `ResolvedAgent` records the effective selected `modelRef`, the declared
  `defaultModelRef`, `modelSelectionRef`, decision ID, and selected model
  configuration. Existing consumers that read `modelRef` continue to work.
* Monetary constraints fail closed when required price metadata is unavailable.
  Implicit zero cost, a live provider price lookup, or a provider default is
  never treated as AEP policy.
* Replay uses the recorded selection decision by default. An explicit selection
  override creates a new decision and cannot rewrite historical evidence.
* Load-time validation rejects missing or unversioned references, candidates
  whose provider has no registered adapter, capability or pricing metadata that
  contradicts declared constraints, duplicate candidates, and a policy that
  requires cost without a pinned price source.
* Tests cover static single-Model fallback, ordered selection, capability
  rejection, context-window rejection, provider allow and deny, missing price,
  no eligible candidate, duplicate candidate, default candidate absent from the
  policy, decision immutability, and replay reuse.
* Documentation defines the selection algorithm, scope precedence, decision
  evidence fields, and conceptual per-Agent policy examples without changing
  production Resources.

## Out Of Scope

* The OpenRouter provider adapter (AEP-082).
* Budget reservation and enforcement wiring (AEP-083).
* Inspection CLI rendering (AEP-084).
* Automatic promotion of a selected model based on evaluation scores.
* Provider-side fallback that hides attempts from AEP accounting.

## Implementation Notes

Use a single explicit strategy, `DECLARED_ORDER`. Do not introduce a numeric
scoring model or a live optimization loop. Price and capability metadata are
declarative inputs so that a decision can be reproduced from Resources rather
than from provider behavior. `ModelSelectionDecision` is evidence, not a second
configuration source: the selected Model must still be the exact versioned
Resource referenced by the decision. A policy may be shared by several Agents,
but the decision remains bound to one TaskExecution attempt.

## Design References

* [Workflow Runtime](../architecture/workflow-runtime.md)
* [Context Builder](../architecture/context-builder.md)
* [Observability](../architecture/observability.md)
