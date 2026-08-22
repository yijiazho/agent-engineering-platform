# AEP-046: Coordinate Model Rate Limits

**Status:** In Progress

## Context

The controlled self-hosting pilot now passes MTP-09 and builds the bounded
AnalyzeIssue ContextPackage introduced by AEP-045. MTP-10 remains blocked at
the live OpenAI boundary. The observed WorkflowExecution built a 15,563-token
ContextPackage within its 32,000-token input budget, but both AnalyzeIssue task
attempts failed before structured output was returned:

```text
2 TaskExecution attempts x 2 Model provider attempts = 4 rate-limited requests
failure.class = RECOVERABLE
failure.message = model provider rate limit exceeded
outputSchemaValidation = NOT_RUN
```

The self-hosting Model currently declares `tokenLimit: 32000`,
`timeoutMs: 120000`, and a retry policy of two attempts with a one-second
fallback backoff. The OpenAI adapter sends `tokenLimit` as
`max_output_tokens`. It reads a numeric `Retry-After` header in memory, but
caps it at 60 seconds and does not persist the applied delay or other safe
rate-limit evidence. If the header is absent or invalid, the adapter retries
after one second. The Workflow scheduler independently retries a recoverable
TaskExecution immediately, multiplying provider attempts without coordinating
their token demand.

Every HTTP 429 is currently normalized to the same recoverable `rate_limit`
failure. The raw provider body and headers are intentionally not persisted,
but the implementation also discards safe diagnostic fields needed to tell a
temporary request/token limit from quota, billing, or another condition that
requires operator action. Consequently, the operator cannot prove whether
`Retry-After` was present, how long the runtime waited, which limit scope was
exhausted, or why a retry was attempted.

OpenAI documents that unsuccessful requests contribute to per-minute limits,
`Retry-After` is a minimum wait, nested retry loops must be accounted for, and
the configured maximum output should reflect expected completion size because
it participates in rate-limit accounting. This task must first make the
existing 32,000-token output allowance operationally safe through coordinated
admission, pacing, and retry behavior. Reducing that allowance is not the
primary remediation and must not be required to satisfy this task.

## Reproduction

Run MTP-09 and MTP-10 from
`docs/operations/self-hosting-dogfood.md` with one newly opened issue carrying
the `dogfood` label at creation time. Inspect the correlated TaskExecution,
AgentInvocation, and ModelInvocation records. The failure is reproduced when
AnalyzeIssue creates two failed task attempts, each ModelInvocation records two
`rate_limit` attempts without token usage or schema validation, and no
downstream task or pull request is created.

Provide a credential-free regression using a scripted OpenAI transport, an
injected clock/sleeper, and concurrent or closely spaced model requests. The
fixture must cover responses with and without `Retry-After`, a retry delay
longer than 60 seconds, temporary token exhaustion, and an actionable quota or
billing-style 429. Live provider calls are reserved for the controlled MTP-10
operator verification.

## Deliverable

Implement provider-aware rate-limit coordination and safe diagnostics that:

* introduces a shared Model request admission coordinator, scoped at least by
  provider and configured model/credential project boundary, so concurrent
  Tasks and retry attempts reserve estimated demand and are paced instead of
  producing an uncoordinated burst;
* uses a deterministic, bounded token/request scheduling policy informed by
  request estimates, configured output allowance, successful usage, and safe
  provider rate-limit/reset hints, with injected time and jitter sources for
  offline tests;
* honors a valid `Retry-After` as a minimum without silently truncating it to
  60 seconds, and defers retry work when the required delay exceeds the active
  invocation deadline instead of sending an early request;
* coordinates Model adapter retries with Workflow Task retries through one
  explicit attempt budget or persisted `not-before` contract, preventing the
  current two-by-two retry amplification and preserving restart-safe,
  idempotent TaskExecution ownership;
* distinguishes temporary throttling from normalized quota, billing, access,
  and other operator-action failures using only allowlisted provider fields,
  so unchanged requests are not repeatedly retried when waiting cannot help;
* persists and logs safe rate-limit evidence including HTTP status, normalized
  provider error reason, attempt count, coordinator delay, delay source,
  applied `Retry-After`, retry eligibility time, and allowlisted
  limit/remaining/reset values when present;
* records admission, throttling, retry-scheduled, retry-suppressed, and
  terminal provider lifecycle events with trace, WorkflowExecution,
  TaskExecution, AgentInvocation, and ModelInvocation correlation;
* continues to omit raw provider bodies, raw headers, prompts,
  ContextPackage/model output bodies, API keys, credential/project identity,
  and unredacted provider request IDs from runtime evidence and logs;
* retains the self-hosting Model `tokenLimit: 32000` as the default for the
  MTP-10 rerun, while keeping future per-Agent output limits possible through
  ordinary versioned Model Resources; and
* updates runtime schemas, Model adapter and Workflow Runtime architecture,
  observability guidance, self-hosting operations, Resource versions and
  fixtures, and operator troubleshooting commands for the resulting public
  behavior.

The coordinator may be process-local for the single-replica self-hosting MVP
only if its ownership boundary is explicit and the design fails safely when
multiple workers would otherwise share one provider quota. A distributed
deployment must not falsely claim cross-process coordination.

## Dependencies

* AEP-010
* AEP-036
* AEP-040
* AEP-042
* AEP-045

## Acceptance Criteria

* A deterministic concurrent test proves that multiple ready Model requests
  sharing one provider quota are admitted at paced times according to the
  coordinator rather than being dispatched as an immediate burst.
* The rate-limit estimate accounts for the configured 32,000-token output
  allowance and the estimated request size. The self-hosting Model remains at
  `tokenLimit: 32000` throughout the primary MTP-10 validation.
* A numeric `Retry-After` is honored as a minimum even when it exceeds 60
  seconds. If it cannot fit within the current deadline, no early provider
  request occurs and persisted evidence states when a later retry becomes
  eligible.
* Missing or invalid `Retry-After` uses bounded exponential backoff with
  jitter, not a fixed one-second loop. Tests assert the chosen delay and its
  source without sleeping in real time.
* Provider and Task retry layers share an explicit policy: one logical
  AnalyzeIssue attempt cannot expand into an undocumented two-by-two request
  burst, and unsuccessful requests are included in subsequent admission and
  backoff decisions.
* Temporary request/token throttles remain recoverable. Allowlisted quota,
  billing, authentication, authorization, invalid-request, and unsupported
  model conditions become distinct stable classifications and do not retry
  unchanged requests merely because their HTTP status is 429.
* Failed ModelInvocation evidence contains enough safe information to answer
  whether `Retry-After` was received, what delay was applied, which normalized
  rate-limit scope or reason was observed, how many attempts occurred, and why
  another attempt was sent, deferred, or suppressed.
* Structured logs expose correlated coordinator decisions and normalized
  provider outcomes. Redaction tests prove that raw response bodies and
  headers, prompts, context/output bodies, API keys, project identifiers, and
  raw request IDs cannot enter logs, exceptions, or persisted runtime objects.
* Coordination state is safe across concurrent threads and runtime restarts.
  The documented single-process scope either prevents unsupported
  multi-worker use or is replaced by a durable coordination implementation.
* Existing structured-output, timeout, malformed-response, refusal,
  observability, scheduler, and dogfood tests continue to pass. New tests use
  scripted transports and deterministic time and require no network or live
  credential.
* The full `python -m pytest` suite passes, and all changed `.ai/` Resources
  have new immutable versions with exact references and inventory fixtures
  updated together.
* After local regression validation and provider readiness checks, one
  controlled MTP-09/MTP-10 run progresses beyond AnalyzeIssue without a
  rate-limit retry burst and creates exactly one pull request. If the provider
  remains genuinely unavailable, the run is deferred or fails with sufficient
  safe evidence to identify the actionable limit rather than issuing rapid
  duplicate attempts.
* `README.md`, `docs/architecture/workflow-runtime.md`,
  `docs/architecture/observability.md`,
  `docs/operations/self-hosting-dogfood.md`, schemas, fixtures, this task, and
  `docs/execution-plan.md` describe the same final admission, retry, evidence,
  redaction, Resource-version, and MTP-10 behavior.

## Implementation State

The credential-free implementation and regressions are complete. The
single-worker runtime now has provider-scoped admission pacing, deterministic
request/token reservations, uncapped numeric `Retry-After`, bounded
exponential backoff with injected jitter/time, normalized actionable 429
classification, safe persisted evidence and lifecycle events, and scheduler
`retryNotBefore` coordination. The self-hosting Resource chain is versioned
through `default-reasoning:1.1.0` and `issue-to-pr:1.3.0` with the 32,000-token
output allowance retained.
Safe coordinator deadlines are durably checkpointed and restored across worker
restarts, and delayed admissions revalidate provider-wide throttle changes
immediately before dispatch.

The task remains In Progress until an authorized operator completes the live
MTP-09/MTP-10 run and records either exactly one pull request or actionable
provider evidence without a retry burst.
