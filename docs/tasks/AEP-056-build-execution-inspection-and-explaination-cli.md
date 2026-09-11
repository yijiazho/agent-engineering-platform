# AEP-056: Build Execution Inspection and Explanation CLI

**Status:** Completed

## Context

AEP persists WorkflowExecution, TaskExecution, ContextPackage, AgentInvocation, ModelInvocation, ToolInvocation, EvaluationResult, PolicyDecision, Approval, GeneratedArtifact, and ExecutionEvent objects, but operators currently need to inspect raw runtime state to reconstruct what happened. AEP's control-plane model requires execution history to be understandable without reading implementation code or correlating storage records manually.

The inspection surface must preserve the distinction between immutable Resource configuration and observed runtime objects, expose provenance and causal relationships, and avoid leaking prompt bodies, source bodies, credentials, or other sensitive content by default.

## Deliverable

Implement an `aep` inspection CLI that can render an execution summary and drill into related runtime objects, including an explanation view for terminal workflow outcomes.

The CLI must support at least execution, task, context, invocation, artifact, evaluation, and policy-decision inspection. `aep explain <workflow-execution-id>` must identify the decisive failed, blocked, denied, or approval-pending condition and trace it to the runtime evidence that caused that outcome.

## Dependencies

* AEP-002
* AEP-004
* AEP-011
* AEP-013
* AEP-018
* AEP-025
* AEP-028
* AEP-036

## Acceptance Criteria

* `aep executions show <id>` renders the WorkflowExecution state, repository revision, resolved Workflow version, task DAG status, and elapsed execution information without requiring direct runtime-store access.
* `aep executions list` discovers persisted WorkflowExecution identifiers deterministically, with an optional status filter, so operators do not need direct checkpoint access before inspection.
* Operators can inspect referenced TaskExecution, ContextPackage, AgentInvocation, ModelInvocation, ToolInvocation, EvaluationResult, PolicyDecision, Approval, and GeneratedArtifact objects by immutable identifier when present.
* Inspection output includes immutable Resource references and runtime provenance needed to determine which Workflow, Task, Agent, Prompt, Model, Tool, Evaluation, and Policy versions contributed to an execution.
* `aep explain <id>` produces a deterministic explanation for successful, failed, denied, blocked, and approval-pending executions and identifies the decisive runtime evidence rather than generating an LLM-authored explanation.
* Context inspection reports element categories, selection reasons, provenance, token estimates, and pruning evidence without printing repository source bodies or issue/prompt contents unless an explicit unsafe/debug mode is separately invoked.
* Artifact inspection reports media type, digest, producer TaskExecution, and lineage without silently materializing or executing artifact content.
* Missing or malformed identifiers fail with a non-zero exit code and a stable machine-readable error representation.
* The CLI supports a machine-readable JSON output mode in addition to a human-readable terminal view.
* Tests cover complete success, validation failure, policy denial, approval pending, missing runtime references, and redaction behavior.
* Operator documentation includes examples for tracing an issue-to-PR execution from Event through publication decision.
