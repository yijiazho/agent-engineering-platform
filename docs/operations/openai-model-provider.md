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
  parameters:
    temperature: 0.1
  tokenLimit: 32000
  timeoutMs: 120000
  retryPolicy:
    maxAttempts: 2
    backoffMs: 1000
```

The adapter passes `parameters` to the Responses API unchanged. The names
`model`, `input`, `max_output_tokens`, and `text` are adapter-owned and cannot
be overridden through `parameters`. `tokenLimit` becomes
`max_output_tokens`; `timeoutMs` is one deadline across all attempts; and
`retryPolicy` is the maximum attempt and backoff bound. The adapter requests
strict JSON Schema output using the provider-supported structural projection
of the ResolvedAgent output schema. Unsupported generation hints such as
`minLength` and `uniqueItems` are omitted only from the provider request. A
provider success is parsed as JSON and then the AgentInvocation coordinator
performs authoritative validation against the complete immutable schema before
artifact publication. This follows the
[official Structured Outputs schema contract](https://developers.openai.com/api/docs/guides/structured-outputs);
the configured GPT-5 model supports both the Responses endpoint and Structured
Outputs according to the
[official model page](https://developers.openai.com/api/docs/models/gpt-5).

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
```

`AEP_OPENAI_API_URL` is optional and defaults to the value shown. It must be a
clean HTTPS URL without user information, query parameters, or a fragment.
Construct the selected adapter with `openai_model_adapter_from_environment`.
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

Success evidence records provider request ID, requested model identity, token
usage, latency, finish state, and attempt count. The ModelInvocation separately
records the exact provider, model, parameters, token limit, timeout, and retry
policy resolved from the versioned Model Resource. Input and output bodies are
represented by content addresses in lifecycle evidence.

Provider request IDs from response headers and bodies are always represented
by a deterministic `redacted:sha256:` identity before they enter attempt
metadata, runtime persistence, or lifecycle logs. This retains correlation
without trusting any provider-controlled string as safe evidence.
Adapter-owned parameter collisions and other invocation-time configuration
failures are permanent classified model failures, so both ModelInvocation and
AgentInvocation evidence becomes terminal.

Timeouts, rate limits, incomplete responses, and retryable provider or
transport failures are recoverable only within the Resource retry/deadline
bounds. Authentication failures, unsupported configuration, safety refusals,
model-identity mismatches, rejected requests, and malformed structured output
are permanent. Provider error bodies and transport exception text are reduced
to fixed diagnostics so credentials, prompts, ContextPackage bodies, and
model output bodies do not enter exceptions or lifecycle logs.
