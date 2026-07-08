# ADR-002: Runtime Object Model

**Status:** Proposed

**Authors:** Project Team

**Date:** 2026-07-08

---

# Context

ADR-001 defines declarative Resources as immutable, versioned specifications stored in Git.

The platform also needs runtime-only objects created while reconciling events into executions.

These objects must capture execution state, provenance, tool activity, evaluation evidence, approvals, generated artifacts, and failure information without becoming declarative Resources.

The objective is to make every execution observable, resumable, auditable, and reproducible while preserving a clean boundary between desired configuration and runtime state.

---

# Decision

AEP defines runtime objects separately from Resources.

Runtime objects are created by controllers and runtime services during execution.

They are not stored in the repository and are not edited by users.

The initial runtime object model includes:

* WorkflowExecution
* TaskExecution
* ContextPackage
* ResolvedAgent
* AgentInvocation
* ModelInvocation
* ToolInvocation
* EvaluationResult
* PolicyDecision
* Approval
* GeneratedArtifact
* ExecutionEvent

Runtime objects may reference Resources by immutable versioned identifiers.

Runtime objects may reference generated artifacts by content address.

---

# Runtime Object Principles

## Runtime Objects Are Not Resources

Resources describe desired behavior.

Runtime objects describe observed execution.

Runtime objects are never reconciled from Git and never become part of the `.ai/` resource tree.

---

## Immutable Inputs

Every TaskExecution runs against immutable inputs:

* Workflow version
* Task version
* Agent version
* Prompt version
* Model version
* Tool versions
* Policy versions
* Repository revision
* Repository Knowledge Graph version
* ContextPackage identifier

If any input changes, the platform creates a new execution rather than mutating the existing one.

---

## Mutable Status, Immutable Evidence

Runtime objects may have mutable status fields while executing.

Examples:

* Pending
* Running
* Succeeded
* Failed
* Cancelled
* AwaitingApproval

Evidence attached to a completed step is immutable.

Examples:

* logs
* model outputs
* tool outputs
* generated patches
* evaluation reports
* policy decisions
* approvals

---

## Provenance Is Required

Every runtime object records:

* execution identifier
* parent object identifier
* resource versions
* repository revision
* timestamps
* actor or caller
* trace identifier

No runtime object should be published without enough provenance to explain why it exists.

---

# Runtime Object Hierarchy

```text
WorkflowExecution
  TaskExecution*
    ContextPackage
    ResolvedAgent
    AgentInvocation*
      ModelInvocation*
      ToolInvocation*
    GeneratedArtifact*
    EvaluationResult*
    PolicyDecision*
    Approval*
    ExecutionEvent*
```

The hierarchy describes ownership and traceability.

It does not require physical storage in a single database table.

---

# WorkflowExecution

WorkflowExecution represents one execution of a Workflow in response to an Event.

Responsibilities:

* bind a Workflow version to an Event
* record repository revision
* record execution graph
* track TaskExecution state
* coordinate cancellation and retries
* expose overall execution status
* provide trace root

Typical fields:

```yaml
id:
workflowRef:
eventRef:
repositoryRevision:
knowledgeGraphVersion:
status:
startedAt:
completedAt:
taskExecutions:
traceId:
```

WorkflowExecution is stored as operational state.

---

# TaskExecution

TaskExecution represents one execution attempt of a Task.

Responsibilities:

* bind Task version to a WorkflowExecution
* track dependencies
* request ContextPackage
* request ResolvedAgent
* coordinate AgentInvocation and ToolInvocation lifecycle
* record generated artifacts
* attach evaluation and policy results
* expose retry state

Typical fields:

```yaml
id:
workflowExecutionId:
taskRef:
attempt:
status:
dependencies:
contextPackageId:
resolvedAgentId:
agentInvocations:
toolInvocations:
generatedArtifacts:
evaluationResults:
policyDecisions:
startedAt:
completedAt:
```

TaskExecution is the primary unit for retry, failure classification, and observability.

---

# ContextPackage

ContextPackage is the immutable contract between the Context Builder and an AgentInvocation.

Responsibilities:

* contain minimum sufficient task context
* record selected repository, knowledge, artifact, event, and policy context
* preserve provenance for every included element
* record token budget and truncation metadata
* satisfy Task required-context constraints

Agents never retrieve repository knowledge directly.

If additional repository knowledge is needed, the workflow creates a follow-up Task that receives a new ContextPackage.

---

# ResolvedAgent

ResolvedAgent is produced by the Agent Resolver.

The Agent Resolver loads:

* Agent
* Prompt
* Model
* Tool references
* Policies

ResolvedAgent is an immutable runtime object used for a single TaskExecution or AgentInvocation scope.

Responsibilities:

* bind Agent to explicit Prompt and Model versions
* capture model configuration
* capture prompt reference
* capture allowed non-model Tools
* capture applicable policy constraints
* prevent floating resource resolution during invocation

Typical fields:

```yaml
id:
agentRef:
promptRef:
modelRef:
toolRefs:
policyRefs:
modelParameters:
outputSchema:
resolvedAt:
```

The Agent Resolver owns no durable state.

There is no standalone Agent Runtime service.

---

# AgentInvocation

AgentInvocation represents one cognitive invocation of a ResolvedAgent.

Responsibilities:

* bind ResolvedAgent to ContextPackage
* assemble final model input
* invoke model provider through Model configuration
* validate structured output
* request allowed non-knowledge Tools when needed
* record model usage and cost

Typical fields:

```yaml
id:
taskExecutionId:
resolvedAgentId:
contextPackageId:
status:
modelInvocations:
toolInvocations:
output:
outputSchemaValidation:
tokenUsage:
cost:
startedAt:
completedAt:
```

AgentInvocation may create ModelInvocation objects.

It may create ToolInvocation objects only for allowed non-model, non-knowledge tools.

---

# ModelInvocation

ModelInvocation records a call to an LLM, embedding model, reranker, or other model provider.

Responsibilities:

* bind invocation to Model resource version
* record input and output metadata
* record token usage
* record provider latency
* record cost
* record schema validation outcome
* support replay diagnostics

Model providers are not Tools.

ModelInvocation is governed by Model resources and ResolvedAgent policy constraints.

---

# ToolInvocation

ToolInvocation records one non-model Tool execution.

Responsibilities:

* validate input schema
* evaluate Pre-Execution Capability Policy
* provision sandbox
* execute non-model capability
* record output schema result
* record logs, metrics, and failure class

ToolInvocation never bypasses policy.

Repository knowledge retrieval is not performed by Agents through ToolInvocation.

---

# EvaluationResult

EvaluationResult records the result of deterministic validation.

Responsibilities:

* bind to Evaluation resource version
* record evaluated artifact or TaskExecution
* record pass/fail outcome
* capture logs and evidence
* classify failure reason

EvaluationResult is immutable after completion.

---

# PolicyDecision

PolicyDecision records the result of a policy gate.

There are two named policy gates:

* Pre-Execution Capability Policy
* Publication Policy

Responsibilities:

* record evaluated capability or action
* record matching policy versions
* record decision
* record reason
* record whether approval is required

Possible decisions:

* ALLOW
* DENY
* REQUIRE_APPROVAL

---

# Approval

Approval records a human decision required by policy.

Responsibilities:

* bind approval to PolicyDecision
* record requester
* record approver
* record decision
* record reason
* record expiration
* record timestamp

Approvals are runtime objects and part of execution history.

---

# GeneratedArtifact

GeneratedArtifact represents a durable output produced by workflow execution.

GeneratedArtifact is not a Resource.

Examples:

* implementation plan
* design document
* generated patch
* pull request description
* review report
* evaluation report

Responsibilities:

* record artifact type
* record producer TaskExecution
* record content address
* record repository revision
* record provenance
* link evaluation and policy results
* support reuse by future ContextPackages

Generated artifacts are immutable after publication.

---

# ExecutionEvent

ExecutionEvent is an append-only event emitted by runtime components.

Examples:

* WorkflowExecutionStarted
* TaskExecutionQueued
* ContextPackageCreated
* AgentResolved
* AgentInvocationStarted
* ToolInvocationCompleted
* EvaluationFailed
* ApprovalRequested
* WorkflowExecutionCompleted

ExecutionEvents support audit, replay diagnostics, and observability.

They are not external trigger Events.

---

# Storage

Runtime objects are stored according to durability requirements.

PostgreSQL stores:

* WorkflowExecution
* TaskExecution
* PolicyDecision
* Approval
* runtime metadata

Object Storage stores:

* GeneratedArtifact content
* logs
* model input/output records
* evaluation reports

Redis stores:

* queues
* leases
* locks
* short-lived cache

Graph Store stores:

* Repository Knowledge Graph versions

Git remains the source of truth for declarative Resources.

---

# Failure Semantics

Runtime failures are classified into:

## Recoverable

Examples:

* transient network failure
* model timeout
* rate limit
* sandbox startup failure

Recoverable failures may retry according to Workflow and Task policy.

## Configuration

Examples:

* invalid Resource reference
* missing required context
* schema mismatch
* missing permission

Configuration failures fail fast.

## Evaluation

Examples:

* tests failed
* build failed
* acceptance criteria failed

Evaluation failures follow Workflow-defined behavior.

## Policy

Examples:

* denied capability
* approval rejected
* approval expired

Policy failures are not retried automatically.

---

# Consequences

## Benefits

* Separates declarative desired state from runtime observed state.
* Makes executions auditable and replayable.
* Gives each subsystem explicit contracts.
* Preserves deterministic context construction.
* Clarifies generated artifacts without making them Resources.
* Provides a foundation for persistence, retries, and observability.

## Trade-offs

* More runtime object types to implement.
* More explicit persistence and status management.
* Requires careful trace and provenance discipline.

These trade-offs are acceptable because runtime evidence is central to trust, reproducibility, and debugging.

---

# Future Extensions

Potential future runtime objects include:

* CostRecord
* ResourceSnapshot
* PromptAssembly
* SandboxExecution
* ReplayRecord
* HumanReview
* RiskAssessment

These should remain runtime objects unless they describe desired behavior that belongs in Git.
