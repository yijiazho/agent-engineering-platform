# ADR-003: MVP Vertical Slice

**Status:** Proposed

**Authors:** Project Team

**Date:** 2026-07-08

---

# Context

AEP's long-term architecture includes declarative Resources, repository intelligence, deterministic context construction, reusable Agents, non-model Tools, evaluation, policy, approvals, generated artifacts, and Kubernetes-native execution.

That scope is intentionally broad.

The first implementation should prove the complete engineering loop with the smallest coherent vertical slice.

The objective is not to implement the full control plane.

The objective is to demonstrate that a GitHub Issue can trigger a deterministic AI workflow that builds context, plans work, proposes code changes, validates them, and opens a pull request with auditable execution history.

---

# Decision

The MVP will implement one end-to-end workflow:

```text
GitHub Issue Created
  -> Normalize Event
  -> Resolve Workflow
  -> Execute Task DAG
  -> Build ContextPackages
  -> Resolve Agents
  -> Generate Plan
  -> Generate Patch
  -> Run Validation
  -> Evaluate Results
  -> Apply Publication Policy
  -> Open Pull Request
  -> Persist Runtime History and GeneratedArtifacts
```

The MVP will support one repository, one workspace, one workflow, and one GitHub event type.

The MVP will not merge pull requests or deploy changes.

---

# MVP Scope

## Repository Scope

Supported:

* one GitHub repository
* one default branch
* one working branch per WorkflowExecution
* repository-local `.ai/` configuration

Not supported:

* multi-repository workflows
* organization-wide policy
* cross-repository knowledge
* multi-tenant isolation

---

## Event Scope

Supported event:

* GitHub Issue Created

The Event Controller normalizes the webhook into a platform Event object.

Required event fields:

```yaml
id:
source: github
type: github.issue.created
repository:
issue:
sender:
receivedAt:
deduplicationKey:
```

The deduplication key prevents duplicate WorkflowExecutions for the same GitHub delivery.

---

## Resource Scope

The MVP supports these Resources:

* Workspace
* Workflow
* Task
* Agent
* Prompt
* Model
* Tool
* KnowledgeBase
* Policy
* Evaluation
* Event

All resource references must use explicit versions.

Floating references such as `latest` are not allowed.

---

## Runtime Object Scope

The MVP persists these runtime objects:

* WorkflowExecution
* TaskExecution
* ContextPackage
* ResolvedAgent
* AgentInvocation
* ModelInvocation
* ToolInvocation
* EvaluationResult
* PolicyDecision
* GeneratedArtifact
* ExecutionEvent

Approval may be represented but is not required for pull request creation unless policy requires it.

---

# MVP Workflow

The initial Workflow is `issue-to-pr`.

```text
AnalyzeIssue
  -> BuildImplementationPlan
  -> GeneratePatch
  -> RunValidation
  -> EvaluateAcceptance
  -> CreatePullRequest
```

Each step is a Task.

The Workflow Runtime schedules the DAG.

Agents perform cognitive work only inside TaskExecution boundaries.

---

# MVP Tasks

## AnalyzeIssue

Purpose:

* classify the GitHub issue
* extract requested change
* identify acceptance criteria
* identify likely repository areas

Agent:

* Issue Analyzer

Outputs:

* issue analysis GeneratedArtifact
* acceptance criteria candidate list

Evaluation:

* JSON schema validation

---

## BuildImplementationPlan

Purpose:

* produce a concrete implementation plan
* list files likely to change
* list tests likely to run
* identify risks and assumptions

Agent:

* Planner

Outputs:

* implementation plan GeneratedArtifact

Evaluation:

* plan schema validation
* required sections present

---

## GeneratePatch

Purpose:

* produce code and test changes
* keep changes scoped to the plan
* generate a patch against a working branch

Agent:

* Code Generator

Tools:

* filesystem write
* git branch
* git diff

Outputs:

* patch GeneratedArtifact
* changed file list

Evaluation:

* patch applies cleanly
* changed files are within allowed workspace

---

## RunValidation

Purpose:

* run deterministic validation in Docker
* capture logs
* classify failures

Agent:

* none required

Tools:

* Docker
* build system command
* test command

Outputs:

* validation report GeneratedArtifact

Evaluation:

* build status
* test status

---

## EvaluateAcceptance

Purpose:

* compare generated outputs against issue-derived acceptance criteria
* combine build, test, and artifact validation results

Agent:

* none required for MVP

Outputs:

* EvaluationResult

Policy:

* Publication Policy determines whether PR creation is allowed.

---

## CreatePullRequest

Purpose:

* create a pull request containing the generated patch
* include implementation plan and validation summary
* link back to the triggering issue

Agent:

* PR Writer

Tools:

* GitHub create pull request
* git push

Policy:

* Pre-Execution Capability Policy for `git.push`
* Pre-Execution Capability Policy for `github.create_pr`
* Publication Policy after evaluation passes

Outputs:

* pull request URL
* pull request description GeneratedArtifact

---

# MVP Agents

The MVP defines four Agents:

* Issue Analyzer
* Planner
* Code Generator
* PR Writer

Agents are stateless and immutable.

Each Agent references:

* Prompt
* Model
* allowed non-model Tools
* Policies

Agents never retrieve repository knowledge directly.

Repository context comes only from ContextPackages.

---

# MVP Context Strategy

The Context Builder creates one ContextPackage per TaskExecution.

For the MVP, context retrieval may use a simplified Repository Knowledge model:

* file inventory
* README and docs index
* basic language detection
* dependency manifest detection
* changed file candidates
* issue payload
* previous GeneratedArtifacts from earlier Tasks

Full AST and symbol graph support may be stubbed behind the Repository Knowledge API.

The public ContextPackage contract should not change when richer Repository Intelligence is added.

---

# MVP Tool Scope

The MVP supports these non-model Tools:

* GitHub read issue
* GitHub create pull request
* Git read diff
* Git create branch
* Git push branch
* Filesystem read
* Filesystem write
* Docker run validation

Tool execution must pass Pre-Execution Capability Policy.

Model providers are configured through Model resources and recorded through ModelInvocation.

Model providers are not Tools.

---

# MVP Policy Scope

The MVP implements two policy gates.

## Pre-Execution Capability Policy

Required before:

* filesystem.write
* git.push
* docker.run
* github.create_pr

The MVP may allow these capabilities by default for the configured repository and workspace.

Denied capabilities fail immediately.

## Publication Policy

Required before:

* pull request creation

Publication Policy allows PR creation only when:

* patch generation succeeded
* validation ran
* required EvaluationResults are present
* no policy violation occurred

Merging pull requests is out of scope.

Deployment is out of scope.

---

# MVP Evaluation Scope

The MVP supports deterministic evaluation only.

Evaluation types:

* output schema validation
* patch applies
* build command exits successfully
* test command exits successfully
* required GeneratedArtifacts exist

LLM-as-judge evaluation is out of scope for the MVP.

---

# MVP Storage Scope

PostgreSQL stores:

* WorkflowExecution
* TaskExecution
* PolicyDecision
* runtime metadata

Object Storage stores:

* ContextPackage payloads
* GeneratedArtifacts
* validation logs
* model input/output records

Graph Store stores:

* simplified Repository Knowledge Graph versions

Redis stores:

* queues
* leases
* locks

Git stores:

* `.ai/` Resources
* application source code

---

# MVP Deployment Scope

The MVP runs in Kubernetes with these services:

* event-controller
* resource-controller
* workflow-runtime
* agent-resolver
* context-builder
* tool-runtime
* evaluation-engine

The Knowledge Compiler may run as a simple repository scanner for the MVP.

The Agent Resolver owns no durable state.

---

# Success Criteria

The MVP succeeds when the platform can:

1. Receive a GitHub Issue Created event.
2. Normalize it into a platform Event.
3. Resolve the `issue-to-pr` Workflow.
4. Create a WorkflowExecution.
5. Execute all TaskExecutions in dependency order.
6. Build deterministic ContextPackages.
7. Resolve Agents into ResolvedAgent runtime objects.
8. Invoke model-backed Agents.
9. Generate an implementation plan.
10. Generate a patch.
11. Run validation in Docker.
12. Evaluate deterministic criteria.
13. Apply Publication Policy.
14. Open a pull request.
15. Persist runtime history and GeneratedArtifacts.

---

# Explicit Non-Goals

The MVP will not support:

* pull request merge
* deployment
* multi-tenant authentication
* multi-repository workflows
* full AST-based Repository Knowledge Graph
* tool marketplace
* prompt optimization
* workflow generation
* autonomous policy changes
* LLM-as-judge evaluation
* long-running conversational agents

---

# Failure Handling

The MVP uses simple failure semantics.

Recoverable failures may retry:

* transient GitHub API failures
* model timeout
* Docker startup failure

Configuration failures fail fast:

* invalid resource reference
* missing prompt
* missing model
* missing tool permission

Evaluation failures stop before PR creation:

* build failed
* tests failed
* patch failed to apply

Policy denial stops immediately.

---

# Consequences

## Benefits

* Proves the full control-loop architecture.
* Keeps the first implementation narrow and testable.
* Exercises Resources, runtime objects, context, agents, tools, evaluation, policy, and artifacts.
* Avoids premature multi-repository and multi-tenant complexity.
* Creates a foundation for richer Repository Intelligence later.

## Trade-offs

* Repository Intelligence starts simplified.
* Only one event type is supported.
* Policies are intentionally basic.
* Human approval is represented but not deeply implemented.
* The initial workflow is specialized rather than fully general.

These trade-offs are acceptable because the MVP must validate the end-to-end architecture before expanding breadth.

---

# Future Expansion Path

After the MVP works, the next increments should be:

1. Replace simplified repository scanning with AST and symbol graph compilation.
2. Add Pull Request event workflows.
3. Add review and documentation workflows.
4. Add richer policy language.
5. Add human approval UI.
6. Add multi-repository workflow support.
7. Add replay and execution comparison tooling.
