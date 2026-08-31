# AEP-052: Validate Strict Provider Output Schemas

**Status:** In Progress

## Context

The controlled MTP-09/MTP-10 self-hosting run for GitHub issue #74 completed
`analyze-issue:1.2.0`, then failed permanently during
`build-implementation-plan:1.6.0`. The correlated runtime evidence is:

```text
TaskExecution = taskexecution-af3908db-109e-589e-affc-6924cc504cc5
AgentInvocation = agentinvocation-1895ed924aa17576d70bd28f
ModelInvocation = modelinvocation-644089504c6e99c7b9000871
provider = openai
requestedModel = gpt-5
httpStatus = 400
errorCode = provider_error
attemptCount = 1
retryDecision = suppressed
failure.class = PERMANENT
failure.message = model provider rejected the request
schemaValidation = NOT_RUN
```

This is not the rate-limit failure addressed by AEP-046. The provider admitted
one request, returned HTTP 400, supplied no `Retry-After`, and the runtime
correctly avoided retrying the unchanged request.

The Planner Resource sends its output schema to OpenAI Structured Outputs with
`strict: true`. Within `acceptanceCriteriaClassifications.items`, the object
declares the properties `criterion`, `classification`, and
`requiredInsertion`, but its `required` array contains only `criterion` and
`classification`. OpenAI strict schemas require every object property to be
listed in that object's `required` array; optional values must be represented
through a supported nullable schema rather than by omitting the property from
`required`. The provider therefore rejects the response format before model
output exists.

Existing coverage checks only the top-level property/required equality of each
self-hosting Agent schema. It does not recursively validate nested objects in
properties, array items, or composition branches, so the invalid Planner
schema passed local validation and immutable Resource publication. AEP's
Resource schema also validates general JSON Schema shape without enforcing the
stricter provider-specific subset selected at invocation time.

The OpenAI adapter compounds diagnosis by mapping most non-success HTTP
responses outside its specially classified status codes to the generic
permanent `provider_error`. It persists the HTTP status and a redacted request
identity but intentionally discards the response body, leaving no allowlisted
reason that distinguishes an invalid strict response schema from other HTTP
400 requests. Redaction must remain fail-closed, but operators need a stable,
safe classification for this failure.

Resolve the contract gap before another credentialed pilot. Do not treat a
different model, a larger token budget, or retries of the unchanged HTTP 400
request as a fix.

## Reproduction

Provide a credential-free regression using the production Resource loader,
Agent resolver, provider schema projection, and a scripted OpenAI transport.
Do not require a live API key or persist a raw provider response.

1. Load the current `planner:1.6.0` Agent and resolve its output schema for
   `build-implementation-plan:1.6.0`.
2. Walk the schema recursively and inspect
   `acceptanceCriteriaClassifications.items`.
3. Demonstrate that the object declares `requiredInsertion` in `properties`
   but omits it from `required`.
4. Render the exact strict response-format schema produced by
   `_provider_schema` and demonstrate that the mismatch is preserved.
5. Use a scripted HTTP 400 response equivalent to the provider's invalid
   response-format rejection and demonstrate the current persisted outcome:
   permanent `provider_error`, one attempt, `schemaValidation: NOT_RUN`, and no
   safe invalid-schema reason.
6. Demonstrate that the existing self-hosting schema projection test passes
   because it compares `required` and `properties` only at the root object.

The regression fixture may contain an allowlisted provider error type, code,
parameter name, and sanitized schema location. It must not retain raw response
bodies or headers, prompts, ContextPackage contents, generated output, API
keys, project identifiers, or unredacted provider request IDs.

## Deliverable

Implement recursive provider-schema compatibility validation that:

* defines the OpenAI strict Structured Outputs subset separately from AEP's
  provider-neutral JSON Schema contracts;
* recursively validates every object reachable through properties, array
  items, and supported composition keywords, including the invariant that
  `additionalProperties` is false and every declared property is required;
* represents genuinely optional output values through an explicitly supported
  nullable form while retaining deterministic AEP-side validation and Task
  semantics;
* rejects an incompatible resolved Agent schema before network admission and
  before creating a provider attempt, with a stable non-sensitive schema path
  and failure code;
* fixes and versions the Planner, BuildImplementationPlan Task, Workflow, and
  every other changed Resource, updating exact references and self-hosting
  inventory fixtures atomically;
* audits every self-hosting Agent output schema recursively so another nested
  incompatibility cannot remain hidden behind a top-level-only assertion;
* keeps provider projection deterministic and prevents projection from
  silently changing which fields are required or weakening the immutable AEP
  output contract;
* classifies allowlisted invalid-request details from OpenAI HTTP 400 responses
  when safe to do so, while preserving the existing redaction boundary and
  suppressing retries of unchanged permanent requests;
* persists enough safe evidence to distinguish local provider-schema
  incompatibility, provider-reported invalid response format, and an unrelated
  provider rejection; and
* updates model-provider, Agent resolution, observability, Resource authoring,
  and self-hosting troubleshooting documentation to describe validation timing,
  supported optional-field representation, and safe failure evidence.

The preferred boundary is validation during Resource loading or Agent
resolution so an invalid immutable Resource generation cannot report provider
readiness and then fail only after a paid live workflow begins. Invocation-time
validation remains required as defense in depth when an adapter receives a
request from another caller.

## Dependencies

* AEP-001
* AEP-003
* AEP-012
* AEP-013
* AEP-014
* AEP-030
* AEP-036
* AEP-040
* AEP-042

## Acceptance Criteria

* A recursive regression fails against the uncorrected `planner:1.6.0` schema
  at the stable path
  `$.properties.acceptanceCriteriaClassifications.items.required`, identifying
  `requiredInsertion` as a declared but non-required property.
* The corrected Planner schema expresses `requiredInsertion` in a
  provider-supported nullable form, requires the field on every classification
  item, and retains the semantic rule that it contains `path` and `value` only
  for `REQUIRED_INSERTION` classifications.
* The Planner, BuildImplementationPlan Task, and issue-to-PR Workflow receive
  new immutable versions, and all Agent/Task/Workflow references, expected
  inventory, fixtures, and tests agree on those exact versions.
* Every self-hosting Agent output schema passes a recursive strict-provider
  compatibility audit. Tests cover nested objects, array items, nullable
  objects, supported composition, missing `required`, extra required names,
  open `additionalProperties`, and unsupported keywords at multiple depths.
* Invalid schemas fail deterministically before transport invocation. Tests
  prove the scripted transport received zero requests, no provider quota was
  reserved or consumed, and no retry was scheduled.
* Direct adapter callers receive the same fail-closed preflight even if they
  bypass normal Resource loading or Agent resolution.
* Provider schema projection preserves property names, required-field
  semantics, enums, and supported nullability while removing only explicitly
  documented unsupported annotation or validation keywords. The complete AEP
  schema remains the authority for post-response validation.
* A scripted OpenAI HTTP 400 with allowlisted invalid-response-format evidence
  produces a stable permanent classification such as `invalid_request`, a
  sanitized schema parameter/path when available, `attemptCount: 1`, and
  `retryDecision: suppressed`.
* Unknown or malformed HTTP 400 bodies remain generic and redacted. Tests prove
  raw bodies, raw headers, prompts, context/output bodies, credentials, project
  identifiers, and unredacted request IDs cannot enter runtime evidence, logs,
  exceptions, causes, contexts, or tracebacks.
* Existing structured-output success, malformed response, refusal, timeout,
  authentication, authorization, rate-limit coordination, and retry tests
  continue to pass.
* The full `python -m pytest` suite and self-hosting Resource bundle validation
  pass without network access or live credentials.
* A new controlled MTP-09/MTP-10 execution progresses beyond
  BuildImplementationPlan without an HTTP 400 response-format rejection. It
  either completes the six-Task workflow and creates exactly one pull request,
  or fails later with independently actionable evidence.
* GitHub issue #74 and its failed WorkflowExecution remain historical evidence;
  they are not replayed or mutated as part of credential-free validation.
* `README.md`, model-provider and workflow-runtime architecture,
  observability guidance, Resource authoring guidance, the self-hosting
  runbook, schemas, fixtures, this task, and `docs/execution-plan.md` describe
  the same recursive strict-schema contract and failure behavior.
