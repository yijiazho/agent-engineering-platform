# AEP-042: Implement Live Model Provider Adapter

**Status:** Completed

## Context

The ModelInvocation contract is provider-neutral and current tests use fake
providers. Self-hosting requires at least one live model provider capable of
executing the Issue Analyzer, Planner, Code Generator, and PR Writer while
preserving the exact Model Resource, bounded request, structured output,
provider evidence, retry, timeout, and redaction contracts.

Model access is not a Tool capability. Credentials are deployment secrets and
must not enter Model Resources, ContextPackages, runtime logs, or generated
repository content. The adapter must not add a repository-retrieval path or
allow provider behavior to control Workflow scheduling.

## Deliverable

Implement one live MVP Model provider adapter, selected from the explicit
`Model.spec.provider`, that:

* translates the bounded provider-neutral model request into the provider API
  and returns structured output through the existing ModelInvocation boundary;
* injects credentials and endpoint configuration from runtime secret
  configuration rather than Resources;
* enforces Model Resource timeout, token limit, parameters, retry policy, and
  structured-output expectations; and
* records provider request identity, model identity, usage, latency, finish
  state, and normalized failure evidence without logging prompts, context,
  secrets, or artifact bodies.

## Dependencies

* AEP-013
* AEP-014
* AEP-036

## Acceptance Criteria

* Every self-hosting Agent can invoke the configured provider through the
  existing Model adapter interface and receives output validated against its
  declared schema before publication as an artifact.
* The invoked provider/model and effective bounded parameters exactly match the
  immutable Model Resource recorded by ResolvedAgent and ModelInvocation.
* Missing credentials and unsupported provider configuration fail fast;
  timeouts, rate limits, malformed responses, safety refusals, and transient or
  permanent provider errors receive explicit classifications and bounded retry
  behavior.
* API secrets, prompts, ContextPackage bodies, and model output bodies are not
  emitted in lifecycle logs or exception messages.
* The adapter exposes no filesystem, Git, GitHub, Tool, or repository-knowledge
  access and cannot choose Tasks, dependencies, retries, or publication state.
* Tests use a fake transport to cover structured success, usage evidence,
  timeout, retry, rate limit, refusal, malformed output, unsupported provider,
  missing credentials, and redaction without network access.
* README and deployment documentation describe provider selection, secret
  injection, supported configuration, and a credential-free verification mode.

## Implementation Notes

`aep.openai_model_provider` implements a dependency-free OpenAI Responses API
adapter behind the existing `ModelAdapter` interface. It translates only the
assembled model input, requests strict JSON Schema output, enforces the Model
Resource model, parameters, output-token limit, one overall timeout, and retry
policy, and normalizes request identity, model identity, usage, latency, finish
state, attempts, and safe failures. Runtime-only configuration reads the API
key from `AEP_OPENAI_API_KEY_FILE` and an optional clean HTTPS endpoint from
`AEP_OPENAI_API_URL`; unsupported providers and missing or invalid credentials
fail before invocation.

ResolvedAgent and ModelInvocation evidence now retain the exact effective
Model configuration in addition to the immutable Model reference. Provider
and transport bodies never enter fixed failure messages or exception chains.
Provider-controlled request IDs become deterministic redacted hashes before
persistence or logging. Invocation-time configuration failures are normalized
as permanent Model failures so the coordinator terminalizes both invocation
records.
The urllib transport enforces the remaining Model deadline across connection
and incremental response reads through a cancellable worker, preventing slow
streams from extending the invocation indefinitely. Redirect handling is
disabled, so credentials are never forwarded to a different or downgraded
origin.
Offline scripted-transport tests cover structured success, bounds and usage,
timeouts, transient retry, rate limiting, refusal, malformed output, model
identity mismatch, configuration failure, and redaction. The operator guide
documents secret injection and a credential-free, network-free verification
path.
