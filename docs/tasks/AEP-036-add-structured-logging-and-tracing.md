# AEP-036: Add Structured Logging And Tracing

**Status:** Completed

## Context

ADR-002 requires provenance for every runtime object, and the deployment architecture requires one trace across WorkflowExecution, TaskExecution, ContextPackage, invocations, evaluations, policies, and artifacts. Consistent structured telemetry is necessary to explain scheduling and governance decisions across service boundaries.

Logging must avoid secrets and large artifact bodies while retaining immutable identifiers, versioned references, revision, status, timing, and failure classification. Helpers should work with both local composition and future distributed tracing.

## Deliverable

Implement shared observability support that:

* creates a trace identifier at WorkflowExecution creation and propagates it through every runtime contract;
* emits structured lifecycle logs with execution/task IDs, Resource versions, repository revision, status, timing, and failure class;
* defines correlation and redaction helpers plus service-boundary propagation fields;
* documents required event names and logging fields; and
* tests propagation, correlation, and secret redaction across a fake workflow.

## Dependencies

* AEP-004
* AEP-008
* AEP-011

## Acceptance Criteria

* WorkflowExecution creates traceId.
* TaskExecution, ContextPackage, AgentInvocation, ToolInvocation, EvaluationResult, and PolicyDecision include traceId.
* Logs include execution id, task id, resource versions, repository revision, and status.
* Tests verify trace propagation across fake workflow execution.

## Implementation Notes

The shared `aep.observability` module defines validated correlation fields,
trace-continuity checks, recursive redaction, and an injected-sink lifecycle
logger. The provider-neutral log schema and deterministic fixture live under
`schemas/observability/v1/` and `fixtures/observability/`. Required event names,
fields, propagation rules, and payload exclusions are documented in
`docs/architecture/observability.md`.

Implemented runtime producers consume `CorrelationContext` or validated
boundary fields, reject direct/provenance identity conflicts, and carry the
context through Agent resolution, model and Tool requests, deterministic
evaluations, policy decisions, and artifact publication. Redaction covers
compound credential keys, credential-bearing URLs, and artifact body aliases
without mutating caller data.
