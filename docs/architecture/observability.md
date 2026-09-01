# Structured Observability

**Status:** Implemented MVP contract

**Version:** 1.0

## Purpose

AEP uses one trace for a complete WorkflowExecution. The WorkflowExecution is
the trace root; every downstream runtime object and service request carries the
same `traceId`. Telemetry is provider-neutral JSON so local composition can use
an in-memory or standard-library sink and a later deployment can translate the
same fields into a distributed tracing backend.

## Correlation Across Service Boundaries

Every internal request propagates these exact application fields:

| Field | Requirement |
| --- | --- |
| `traceId` | Required. Copied unchanged from WorkflowExecution. |
| `workflowExecutionId` | Required. Identifies the trace root. |
| `taskExecutionId` | Required after a TaskExecution exists; omitted only for workflow-level requests. |

`CorrelationContext`, `propagation_fields`, and
`CorrelationContext.from_boundary_fields` in `aep.observability` are the shared
contract. Transport adapters may map these values to HTTP headers or message
metadata, but application code must not generate a new trace at a service
boundary. Receiving services validate the fields before doing work.

Boundary validation is fail-closed. `CorrelationContext.from_runtime_object`
compares direct ownership fields with provenance ownership fields and rejects
any disagreement. `bind_correlation` performs the same check between an input
context, explicit producer arguments, and provenance before a runtime object is
created.

WorkflowExecution creation, TaskExecution transitions, and ContextPackage
construction accept an injected `StructuredLifecycleLogger`. ContextPackage
construction also derives its service-boundary fields through
`propagation_fields`, rather than independently copying correlation values.
Agent resolution, ModelRequest and ModelInvocation construction, ToolRequest
and Filesystem ToolInvocation construction, schema/build-test evaluation,
pre-execution policy evaluation, and GeneratedArtifact publication consume or
validate the shared correlation contract. These implemented producer
boundaries no longer accept a free trace scalar. AgentInvocation creation now
consumes this same correlation context and carries it into nested
ModelInvocations rather than introducing another propagation shape. Its
lifecycle records retain the Resource versions and repository revision from
the immutable invocation inputs.

The same `traceId` is required on WorkflowExecution, TaskExecution,
ContextPackage, ResolvedAgent, AgentInvocation, ModelInvocation,
ToolInvocation, EvaluationResult, PolicyDecision, Approval,
GeneratedArtifact, and ExecutionEvent through the common runtime-object
schema. `assert_trace_continuity` provides a deterministic validation helper
for composed flows and tests.

## Lifecycle Log Contract

Lifecycle records conform to
`schemas/observability/v1/lifecycle-log.schema.json`. Every record includes:

| Field | Meaning |
| --- | --- |
| `schemaVersion` | Stable lifecycle-log contract version. |
| `eventName`, `emittedAt`, `service`, `level` | Event identity, time, producer, and severity. |
| `traceId`, `executionId`, `taskId` | Cross-service correlation. `taskId` is null only at workflow scope. |
| `runtimeKind`, `runtimeObjectId` | Subject runtime object. |
| `resourceVersions` | Explicit `{kind,name,version}` references; `latest` is forbidden. |
| `repositoryRevision` | Immutable repository revision used by the execution. |
| `status`, `failureClass` | Lifecycle state and ADR-002 failure classification. |
| `durationMs` | Optional non-negative elapsed duration. |
| `attributes` | Optional redacted, bounded structured metadata. |

The required MVP event names are:

* `WorkflowExecutionStarted`, `WorkflowExecutionCompleted`, `WorkflowExecutionFailed`, `WorkflowExecutionCancelled`
* `TaskExecutionQueued`, `TaskExecutionStarted`, `TaskExecutionSucceeded`, `TaskExecutionFailed`, `TaskExecutionCancelled`, `TaskExecutionAwaitingApproval`
* `ContextPackageCreated`, `AgentResolved`
* `AgentInvocationStarted`, `AgentInvocationCompleted`, `AgentInvocationFailed`
* `ModelInvocationStarted`, `ModelRequestAdmitted`, `ModelRequestThrottled`,
  `ModelRetryScheduled`, `ModelRetrySuppressed`, `ModelInvocationCompleted`,
  `ModelInvocationFailed`
* `ToolInvocationStarted`, `ToolInvocationCompleted`, `ToolInvocationFailed`
* `EvaluationCompleted`, `EvaluationFailed`, `PolicyDecisionRecorded`
* `ApprovalRequested`, `ApprovalRecorded`, `GeneratedArtifactCreated`

Event names are a closed contract. Adding one requires updating the schema and
the semantic compatibility table in `aep.observability`. The logger rejects an
event whose subject kind, status, or failure class is impossible; for example,
`TaskExecutionFailed` cannot describe a successful TaskExecution, and every
failed event must carry one ADR-002 failure class.

## Redaction And Payload Limits

`redact` recursively removes known credential fields and credential-shaped
values. Authorization headers, cookies, passwords, API keys, access and refresh
tokens, private keys, and credentials become `[REDACTED]`. Header maps recognize
common custom names such as `X-Api-Key` and `X-Auth-Token`. Environment maps
also redact connection variables such as `DATABASE_URL`, DSNs, credentials,
and tokens. Credential-bearing connection URLs are redacted even outside an
environment map. Keys representing body or content payloads and strings over
4096 characters become `[OMITTED]`.

Key matching normalizes case and separators, then rejects compound secret
variants such as `secret_key`, `access_token_value`, and `credential_blob` even
when their values are short. Within `artifact`, `artifacts`,
`generatedArtifact`, or `generatedArtifacts` maps (including maps inside
lists), body aliases such as `payload`, `patch`, `diff`, `data`, `text`, and
`bytes` are omitted. Compound top-level aliases such as `artifact_patch` and
`generated_artifact_payload` are omitted as well. Safe metadata such as
`contentAddress` remains available.
Content addresses, artifact identifiers, sizes, media types, and hashes remain
safe metadata.

Do not pass model prompts or outputs, Tool input/output bodies, repository file
contents, GeneratedArtifact bodies, environment variables, or credentials as
top-level lifecycle fields. Persist large evidence in its governed store and
log only its immutable address. `StructuredLifecycleLogger` applies redaction
to a deep result before invoking its injected sink and never mutates caller
input.

Git askpass evidence is limited to operation, repository, branch, safe failure
class, remote mutation state, duration, command exit status, and the
content-addressed redacted log reference. Helper source, interpreter output,
credential values, scoped or ambient environment maps, provider response
bodies, and unrestricted stderr are never lifecycle fields. Local
provider-schema incompatibility is distinguished from a provider-reported
invalid response format: the former has zero attempts and no quota reservation;
the latter records one suppressed-retry attempt plus only allowlisted error
type, code, and redacted schema parameter. Unknown HTTP 400 bodies remain a
generic, redacted provider error.

## Failure And Timing Semantics

When a runtime object has a failure, the lifecycle record carries its ADR-002
failure class (`RECOVERABLE`, `CONFIGURATION`, `EVALUATION`, `POLICY`, or
`PERMANENT`) and uses `ERROR` level. Other lifecycle records use `INFO`.
Durations are measured by the producing component and expressed as
non-negative integer milliseconds. Timestamps are timezone-qualified RFC3339.

Model admission attributes are allowlisted normalized evidence: HTTP status,
provider reason/scope, attempt and retry decision, token estimate/reservation,
coordinator/applied delay and source, retry eligibility, hashed request ID, and
numeric limit/remaining/reset hints. Raw provider bodies and headers, prompts,
ContextPackage/model output bodies, API keys, credential/project identity, and
raw request IDs are omitted.

Strict output-schema failures distinguish three safe cases: local
`invalid_response_schema` evidence has a schema path and attempt count zero;
allowlisted provider-reported response-format rejection is `invalid_request`
with a sanitized schema parameter; unrelated or malformed HTTP 400 responses
remain `provider_error`. None may retain provider bodies or raw headers.
