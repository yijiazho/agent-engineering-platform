# AEP-042: Implement Live Model Provider Adapter

**Status:** Not Started

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
