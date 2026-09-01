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

The first AEP-052 implementation corrected and versioned the Planner contract,
added recursive validation, and added safe invalid-schema classification. A
second controlled MTP-09/MTP-10 run for GitHub issue #76 proves that work is
incomplete. At repository revision
`7b51ff36ac55986d63cba69bbe88d8fc506775f2`, AnalyzeIssue and the corrected
`build-implementation-plan:1.7.0` both succeeded, but
`generate-patch:1.11.0` failed before output was returned:

```text
WorkflowExecution = workflowexecution-4453cee8-45d2-52f4-903c-a8b3eadea0cc
TaskExecution = taskexecution-fe3bca8b-8b76-5d7b-aa4f-7bf4b1cb1718
AgentInvocation = agentinvocation-ff4fdeaaf468b3882d149eaf
ModelInvocation = modelinvocation-d04522a8b8b509a81360e024
provider = openai
requestedModel = gpt-5
httpStatus = 400
errorCode = invalid_request
providerErrorReason = invalid_response_format
providerErrorCode = invalid_json_schema
providerErrorType = invalid_request_error
schemaParameter = text.format.schema
attemptCount = 1
retryDecision = suppressed
schemaValidation = NOT_RUN
```

The improved diagnostic and retry behavior worked: the response is now
classified as a permanent invalid schema rather than a generic provider error,
and the unchanged request was attempted only once. The compatibility boundary
did not work. `code-generator:1.11.0` represents its write/delete discriminator
with `{"const": "write"}` and `{"const": "delete"}` inside nested `anyOf`
branches. `provider_schema._SUPPORTED` explicitly admits `const`, and
`_provider_schema` transmits it unchanged, so Resource loading, Agent
resolution, and invocation preflight all accept the schema before the live
endpoint rejects `text.format.schema`.

For the deployed Responses API and `gpt-5` contract, these discriminators must
use the supported string-enum representation, such as
`{"type": "string", "enum": ["write"]}`, unless a provider-contract test
proves another representation against the exact endpoint/model generation.
The local allowlist must be derived from the effective provider subset rather
than from general Draft 2020-12 validity or assumptions about similarly named
provider limits. Passing the project's validator is not sufficient evidence
when the exact rendered self-hosting schemas have never been exercised against
a provider-faithful compatibility oracle.

Resolve the contract gap before another credentialed pilot. Do not treat a
different model, a larger token budget, or retries of the unchanged HTTP 400
request as a fix.

## Reproduction

Provide credential-free regressions for both live failures using the production
Resource loader, Agent resolver, provider schema projection, and a scripted
OpenAI transport. Do not require a live API key or persist a raw provider
response.

### Issue #74: Nested Required-Field Mismatch

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

### Issue #76: Accepted Locally, Rejected By Provider

1. Load `code-generator:1.11.0` and resolve its output schema for
   `generate-patch:1.11.0`.
2. Render the exact schema sent in `text.format.schema` and demonstrate that
   both `changes.items.anyOf` object branches preserve their `const`
   discriminators.
3. Demonstrate that Resource loading, the recursive strict-schema validator,
   self-hosting bundle validation, and invocation preflight all accept this
   rendered schema.
4. Feed a scripted HTTP 400 carrying only allowlisted
   `invalid_request_error`, `invalid_json_schema`, and
   `text.format.schema` fields through the production adapter.
5. Assert the issue #76 evidence shape: one failed ModelInvocation,
   `invalid_request`, `invalid_response_format`, `schemaValidation: NOT_RUN`,
   one attempt, and suppressed retry.
6. Replace each discriminator with an explicit string type and singleton enum,
   then demonstrate that the provider-faithful compatibility contract accepts
   the complete rendered Code Generator schema.

The regression fixture may contain an allowlisted provider error type, code,
parameter name, and sanitized schema location. It must not retain raw response
bodies or headers, prompts, ContextPackage contents, generated output, API
keys, project identifiers, or unredacted provider request IDs.

## Deliverable

Implement recursive provider-schema compatibility validation that:

* defines the OpenAI strict Structured Outputs subset separately from AEP's
  provider-neutral JSON Schema contracts;
* removes `const` from the accepted and transmitted subset for the deployed
  OpenAI Responses API contract, or gates it behind explicit endpoint/model
  capability evidence rather than admitting it globally;
* expresses Code Generator write/delete discriminators as supported typed
  singleton enums without weakening the AEP-side operation contract;
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
* fixes and versions the Code Generator, GeneratePatch Task, Workflow, and
  every downstream exact Resource reference affected by the issue #76 schema;
* audits every self-hosting Agent output schema recursively so another nested
  incompatibility cannot remain hidden behind a top-level-only assertion;
* validates the complete post-projection schemas actually transmitted for all
  self-hosting Agents, not only their unprojected Resource schemas or isolated
  hand-built fixtures;
* maintains one explicit, reviewable compatibility matrix for accepted AEP
  keywords, projected provider keywords, endpoint/model support, and AEP-only
  post-response validation keywords;
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
* A regression using the uncorrected `code-generator:1.11.0` rendered schema
  fails the provider-compatibility contract specifically at both nested
  operation discriminators; it cannot pass merely because `const` is valid
  general JSON Schema.
* The corrected Code Generator schema uses typed singleton enums for `write`
  and `delete`, retains mutually exclusive operation payloads, and continues
  to require `content` only for writes while preserving preimage binding for
  both operations.
* The Code Generator, GeneratePatch Task, issue-to-PR Workflow, expected
  self-hosting inventory, and all exact references receive synchronized new
  immutable versions.
* Every self-hosting Agent output schema passes a recursive strict-provider
  compatibility audit. Tests cover nested objects, array items, nullable
  objects, supported composition, missing `required`, extra required names,
  open `additionalProperties`, `const`, typed singleton enums, and unsupported
  keywords at multiple depths.
* The audit runs against each exact post-projection schema sent to OpenAI and
  asserts that its keyword set is a subset of the reviewed endpoint/model
  compatibility matrix. Adding a keyword to `_SUPPORTED` alone cannot make the
  audit pass.
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
* A new controlled MTP-09/MTP-10 execution progresses beyond both
  BuildImplementationPlan and GeneratePatch without an HTTP 400
  response-format rejection. It either completes the six-Task workflow and
  creates exactly one pull request, or fails later with independently
  actionable evidence.
* GitHub issues #74 and #76 and their failed WorkflowExecutions remain
  historical evidence; they are not replayed or mutated as part of
  credential-free validation.
* `README.md`, model-provider and workflow-runtime architecture,
  observability guidance, Resource authoring guidance, the self-hosting
  runbook, schemas, fixtures, this task, and `docs/execution-plan.md` describe
  the same recursive strict-schema contract and failure behavior.
