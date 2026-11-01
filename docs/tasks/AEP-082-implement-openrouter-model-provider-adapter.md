# AEP-082: Implement The OpenRouter Model Provider Adapter

**Status:** Not Started

## Context

The current runtime constructs one OpenAI adapter from environment configuration
at process startup. `OpenAIModelAdapter` requires `provider == "openai"` and
speaks the OpenAI Responses API. The Model schema permits `anthropic`, `google`,
and `local`, but no adapter exists for those values, and no adapter registry
selects a provider per resolved Model.

OpenRouter exposes many upstream models through one HTTPS API. It can provide
useful per-request usage and cost data, but it also introduces routing behavior,
upstream capability differences, and provider-specific failure codes. Adding it
as a single global adapter would make the Model Resource's provider identity
ambiguous and could hide spend from AEP-062 budgets.

This task adds OpenRouter as one explicit provider behind the existing
provider-neutral ModelInvocation boundary. Model selection remains an AEP-081
control-plane decision; the adapter executes exactly the Model it is given.

## Deliverable

Implement an `OpenRouterModelAdapter`, register it behind a provider-keyed
adapter registry, and normalize OpenRouter request, response, usage, cost,
capability, and failure evidence. Add environment configuration, sample
Resources, and credential-free adapter tests.

## Dependencies

* AEP-014
* AEP-042
* AEP-046
* AEP-052
* AEP-062
* AEP-081

## Acceptance Criteria

* The Model Resource provider enum includes `openrouter`, and a provider-keyed
  adapter registry resolves the adapter from the immutable Model configuration.
  Existing OpenAI behavior and environment variables remain unchanged.
* Runtime composition no longer assumes one global model adapter. Two Agents or
  Tasks in one execution may resolve different providers and models without
  sharing provider-specific clients or state.
* OpenRouter credentials are read from `AEP_OPENROUTER_API_KEY_FILE`; the API
  base URL comes from `AEP_OPENROUTER_API_URL` and must be a clean HTTPS URL.
  Secrets never appear in Resources, runtime evidence, logs, or test output.
* The adapter sends exactly the requested Model's `spec.model` slug for one
  ModelInvocation. OpenRouter-side automatic model fallback lists are rejected
  unless a later task defines how each hidden attempt is separately evidenced
  and budgeted. An AEP candidate fallback is always a new ModelInvocation.
* OpenRouter's response is normalized into the existing ModelResponse contract:
  output, input and output token usage, optional cached or reasoning token
  details, latency, provider request or generation identity, finish reason, and
  safe provider metadata. Missing or malformed required usage fails closed.
* Cost evidence records a fidelity label: `EXACT` when OpenRouter reports the
  request cost, `CONFIGURED` when pinned Model pricing computes it from usage,
  `ESTIMATED` only for an explicitly declared estimation rule, and
  `UNAVAILABLE` when no reliable value exists. Unavailable cost is never
  persisted or displayed as zero.
* Structured output is validated against the requested schema before dispatch.
  A model that does not support the required structured-output contract fails
  with a stable terminal configuration error and is never silently downgraded.
* Provider failures map to stable classifications: authentication and
  authorization are permanent; insufficient credit is a permanent
  `provider_credit_exhausted` condition, not a retry; rate limiting is
  recoverable and honors `Retry-After`; transient server failures are bounded
  and recoverable; invalid request, context-length, schema, and unknown-model
  failures are permanent.
* Timeout, retry, and output-token bounds come from the immutable Model
  Resource. The adapter does not add a second retry loop or exceed
  `retryPolicy`. Existing ModelInvocation pair claims prevent ambiguous replay
  after restart.
* Rate-limit admission is keyed by provider, API endpoint, and upstream model
  rather than one process-global OpenAI bucket. Safe OpenRouter rate-limit
  headers and reset hints are persisted without raw bodies.
* Tests cover a successful structured response, normalized usage, cached or
  reasoning token fields when present, exact and configured and unavailable
  cost, insufficient credit, rate limiting with and without `Retry-After`,
  transient server failure, schema rejection, unsupported model, timeout,
  retry bounds, adapter registry selection, and credential redaction.
* Documentation describes OpenRouter configuration, credential handling,
  supported request parameters, cost fidelity, failure mapping, and a sample
  Model Resource. The sample uses a placeholder credential reference only.

## Out Of Scope

* The model selection policy and candidate ordering (AEP-081).
* Budget reservation, per-candidate preflight, and blocked reasons (AEP-083).
* Inspection CLI rendering (AEP-084).
* Automatic discovery of OpenRouter models or live price synchronization.
* Provider-side fallback that consumes budget without a recorded AEP attempt.

## Implementation Notes

Preserve the provider-neutral `ModelAdapter` boundary. OpenRouter request
translation belongs inside the adapter, and runtime evidence must retain both
the requested AEP Model reference and the upstream provider model identity.
Use a scripted transport for all deterministic tests; live credentials are not
required for the suite. Do not import OpenRouter SDK behavior into the
scheduler or budget layer.

## Design References

* [Workflow Runtime](../architecture/workflow-runtime.md)
* [Observability](../architecture/observability.md)
* [Tool Platform](../architecture/tool-platform.md)
