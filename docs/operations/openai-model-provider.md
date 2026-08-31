# OpenAI Model Provider Operations

The live MVP Model adapter is selected only when the immutable Model Resource
sets `spec.provider: openai`. It calls the OpenAI Responses API through the
existing `ModelAdapter` boundary. It is not a Tool and has no filesystem, Git,
GitHub, scheduler, or repository-knowledge capability.

## Configure The Model Resource

The Resource owns the model identity and every invocation bound:

```yaml
spec:
  provider: openai
  model: gpt-5
  tokenLimit: 32000
  timeoutMs: 120000
  retryPolicy:
    maxAttempts: 1
    backoffMs: 1000
  rateLimitPolicy:
    requestsPerMinute: 2
    tokensPerMinute: 80000
```

The live adapter accepts only the stateless generation parameters
`temperature` and `top_p`; their values must be finite. All other parameter
names, including state-bearing Responses options such as
`previous_response_id` and `conversation`, fail permanently before a provider
request. This prevents provider-side conversation state from changing an
invocation outside its assembled, content-addressed ModelRequest. `tokenLimit`
becomes `max_output_tokens`; `timeoutMs` is one deadline across all attempts;
and `retryPolicy` is the maximum attempt and backoff bound. Although the base
Model schema permits omission, `timeoutMs` is required by the live OpenAI
adapter so every elapsed deadline is explicit in ResolvedAgent and
ModelInvocation evidence. The adapter requests strict JSON Schema output using
the provider-supported structural projection of the ResolvedAgent output
schema. Unsupported generation hints such as `minLength` and `uniqueItems` are
omitted only from the provider request. A provider success is parsed as JSON
and then the AgentInvocation coordinator
performs authoritative validation against the complete immutable schema before
artifact publication. This follows the
[official Structured Outputs schema contract](https://developers.openai.com/api/docs/guides/structured-outputs);

Before readiness or invocation, AEP recursively validates object schemas in
properties, array items, composition branches, and `$defs`. Every object must
set `additionalProperties` to `false`, and its `required` names must exactly
match its declared properties. Express an optional value as a required property
whose schema includes an explicit `{ "type": "null" }` branch in `anyOf`.
`anyOf` is the only supported composition keyword; `allOf` and `oneOf` fail
preflight rather than being forwarded to the provider. `anyOf` is nested-only;
root-level composition and array-valued `type` unions also fail preflight.
Provider projection preserves names, requiredness, enums, and nullability; it
only removes the documented AEP-side `minLength`, `maxLength`, and `uniqueItems`
checks. The immutable AEP schema remains authoritative after generation.

An incompatible local schema fails as `invalid_response_schema` with a safe
`schemaPath`, zero provider attempts, no quota reservation, and suppressed
retry. An allowlisted OpenAI HTTP 400 invalid-schema response fails permanently
as `invalid_request` with sanitized error type/code and schema parameter.
Malformed or unknown bodies remain generic `provider_error`; raw bodies,
headers, prompts, context, credentials, project identifiers, and raw request IDs
are never persisted.
the configured GPT-5 model supports both the Responses endpoint and Structured
Outputs according to the
[official model page](https://developers.openai.com/api/docs/models/gpt-5).

`rateLimitPolicy` drives a thread-safe, process-local admission coordinator.
Each reservation includes the estimated serialized request tokens plus the
complete configured output allowance, so the self-hosting 32,000-token
allowance remains available without being hidden from rate-limit accounting.
Request and token clocks pace concurrently-ready work. Successful usage credits
a conservative reservation only while it remains the latest token reservation;
later reservations keep the shared tail fixed so credit cannot create a burst.
Provider reset and `Retry-After` hints delay the whole credential scope. The self-hosting policy uses one provider request
per TaskExecution attempt; the Workflow scheduler owns the second logical
attempt and honors persisted `failure.retryNotBefore` evidence.
Delayed reservations recheck the shared throttle clock immediately before
dispatch, so a newer provider minimum supersedes an earlier reservation.

The self-hosting `gpt-5` Resource omits `parameters` because that model does
not accept `temperature` or `top_p`. Use these optional generation parameters
only with a configured model that supports them.

The adapter sends the versioned Prompt Resource's `system` and `formatting`
content through the Responses API `instructions` channel. Prompt examples, if
present, are included there as trusted instructions as well. The top-level
`input` contains only the assembled ContextPackage, keeping user-controlled
issue and repository text at user priority instead of placing it alongside
self-hosting guardrails. Structured-output enforcement remains provider-owned
through `text.format`, and the complete provider-neutral ModelRequest remains
content-addressed in runtime evidence.

The HTTP transport applies each attempt's remaining budget to the complete
operation, including connection setup, response headers, and incremental body
reads. A cancellable deadline worker closes an active response and returns a
timeout even when a peer continuously trickles bytes below the socket's
per-operation timeout. HTTP redirects are never followed: a 3xx response is
normalized as a provider failure, so the Authorization header cannot cross to
another origin or an HTTP downgrade.

## Inject The Credential And Endpoint

Mount the API key as a read-only runtime secret file in every process that
executes AgentInvocations:

```text
AEP_OPENAI_API_KEY_FILE=/run/secrets/openai-api-key
AEP_OPENAI_API_URL=https://api.openai.com/v1
AEP_MODEL_WORKER_REPLICAS=1
AEP_STATE_ROOT=/var/lib/aep
```

`AEP_OPENAI_API_URL` is optional and defaults to the value shown. It must be a
clean HTTPS URL without user information, query parameters, or a fragment.
Construct the selected adapter with `openai_model_adapter_from_environment`.
The coordinator is process-local for the single-replica MVP. Startup rejects
`AEP_MODEL_WORKER_REPLICAS` values other than `1`; multi-process deployment
requires a durable distributed coordinator.
Safe request, token, and throttle deadlines are atomically checkpointed below
`AEP_STATE_ROOT/model-rate-limits`. The internal directory scope hashes the
endpoint and credential, and its files hash the configured model and capacity;
raw identities never enter the state document, runtime evidence, or logs.
Startup restores unexpired deadlines against wall time before admitting work,
so restarting the worker cannot erase an active reservation or `Retry-After`.
Missing state initializes an empty coordinator; malformed, unreadable, or
unwritable state fails recoverably without dispatching a provider request, and
continues to fail closed until the checkpoint is repaired.
Startup fails before a provider request when the selected provider is not
supported, the secret-file setting is missing, the file is unavailable or
empty, or the endpoint is invalid. Never put the key, secret path, or endpoint
credentials in a Model Resource, ContextPackage, prompt, or generated content.

## Verify Without Credentials Or Network

Resource discovery in the local Compose smoke test remains credential-free.
The provider selection and endpoint can also be checked without reading a key
or making a network request:

```powershell
python -c "from aep.openai_model_provider import verify_openai_model_provider_environment as verify; print(verify('openai'))"
python -m pytest tests/test_openai_model_provider.py
```

The first command reports only provider, endpoint, and
`CONFIGURATION_VALID`. Live readiness reports the same safe identity and never
returns the key or secret-file path. Credential validity is established only
by a bounded live invocation.

## Failure And Evidence Contract

Success evidence records provider request ID, the requested Model Resource
identity, the provider-resolved model identity, token usage, latency, finish
state, and attempt count. OpenAI aliases such as `gpt-5` may resolve to a
dated snapshot; this is recorded as `requestedModel` and `providerModel`
instead of rejecting a valid alias response. Provider model IDs must match a
tightly bounded identifier format before entering evidence. The
ModelInvocation separately records the exact provider, model, parameters,
token limit, timeout, and retry policy resolved from the versioned Model
Resource. Input and output bodies are represented by content addresses in
lifecycle evidence.

Provider request IDs from response headers and bodies are always represented
by a deterministic `redacted:sha256:` identity before they enter attempt
metadata, runtime persistence, or lifecycle logs. This retains correlation
without trusting any provider-controlled string as safe evidence.
Unsupported parameters and other invocation-time configuration failures are
permanent classified model failures, so both ModelInvocation and
AgentInvocation evidence becomes terminal. Non-finite Resource parameter
values are rejected during Model configuration, before runtime records are
created; unsafe serialization discovered later is normalized to the same
terminal failure path.

Timeouts, rate limits, incomplete responses, and retryable provider or
transport failures are recoverable only within the Resource retry/deadline
bounds. An incomplete response caused by `content_filter` is a permanent safety
failure, and `max_output_tokens` exhaustion is a permanent configured-bound
failure because retrying the unchanged request cannot succeed. Unknown
incomplete reasons remain recoverable within the Resource bounds.
Authentication failures, unsupported configuration, safety refusals, rejected
requests, decoder nesting-limit failures, and malformed structured output are
permanent. Non-finite or overflowing `Retry-After` values are ignored instead
of escaping normalized failure handling. Provider error bodies and transport
exception text are reduced to fixed diagnostics so credentials, prompts,
ContextPackage bodies, and model output bodies do not enter exceptions or
lifecycle logs.

Provider schema projection removes unsupported `minLength`, `maxLength`, and
`uniqueItems` keywords only from schema objects. Identically named fields under
`properties` and other schema maps are preserved, including their `required`
entries.

A valid numeric `Retry-After` is a minimum delay and is never capped at 60
seconds. When admission or retry cannot occur before the invocation deadline,
the adapter sends no early request and records `retryEligibleAt` for the
Workflow scheduler. Missing or invalid values use exponential backoff bounded
at 60 seconds with injected jitter. Temporary token/request throttles remain
recoverable. Allowlisted quota, billing, authentication, authorization,
invalid-request, and unsupported-model reasons are permanent for the unchanged
request even when delivered as HTTP 429.
Eligibility is calculated before checking whether the current provider attempt
is the last one. Thus the self-hosting one-attempt Model still persists the
provider minimum for the scheduler instead of suppressing it.

Safe failure evidence includes HTTP status, normalized reason and limit scope,
attempt count, estimated/reserved tokens, coordinator and applied delays,
delay source, hashed request identity, retry decision and eligibility, plus
numeric allowlisted limit/remaining/reset fields. Raw bodies, raw headers,
prompts, ContextPackage/output bodies, API keys, project or credential
identity, and raw request IDs are omitted.

```powershell
$objects = Get-Content "$env:AEP_STATE_ROOT/runtime/objects.json" -Raw | ConvertFrom-Json
$objects | Where-Object kind -eq ModelInvocation | Select-Object id,status,failure,providerMetadata
docker compose --env-file deploy/local/.env -f deploy/local/compose.yaml logs --since 15m agent-invocation | Select-String 'ModelRequestAdmitted|ModelRequestThrottled|ModelRetry'
```
