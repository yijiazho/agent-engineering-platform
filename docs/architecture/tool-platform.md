# Tool Platform

**Project:** AI Agent Engineering Platform (AEP)

**Document:** Tool Platform

**Status:** Draft

**Version:** 0.1

---

# 1. Overview

The Tool Platform provides the execution layer for non-model interactions with external systems.

Rather than allowing Agents to directly invoke operating system commands, APIs, or services, every non-model external capability is represented by a declarative Tool resource and executed through a controlled Tool Runtime.

This architecture ensures that every external action is observable, auditable, permission-controlled, and reproducible.

Model providers are not Tools.

Model provider configuration is represented by Model resources and consumed through ResolvedAgent during AgentInvocation.

---

# 2. Design Goals

The Tool Platform should provide:

* Secure execution
* Deterministic behavior
* Strong isolation
* Fine-grained permissions
* Auditability
* Extensibility
* Reusable tool definitions

---

# 3. Non-Goals

The Tool Platform does not:

* perform AI reasoning
* schedule workflows
* build context
* decide which Tool should be invoked
* invoke model providers

Tool selection is constrained by Tasks, ResolvedAgents, and policies.

Model provider invocation belongs to AgentInvocation and is configured through Model resources.

---

# 4. High-Level Architecture

```text id="1mu75u"
               Agent
                 │
                 ▼
           Tool Contract
                 │
                 ▼
           Tool Runtime
                 │
        Permission Engine
                 │
                 ▼
         Sandbox Manager
                 │
                 ▼
        Container Executor
                 │
                 ▼
          External System
```

The Tool Runtime owns execution.

Tools only describe capabilities.

---

# 5. Design Philosophy

Tools are declarative resources.

They describe:

* capability
* interface
* permissions
* execution requirements

They never execute code themselves.

Execution is delegated to the Tool Runtime.

This mirrors the separation between:

* Workflow and Workflow Runtime
* Task and TaskExecution
* Agent and Agent Invocation

---

# 6. Tool Resource

A Tool represents a reusable platform capability.

Examples include:

* GitHub
* Filesystem
* Python
* Docker
* Search
* Kubernetes
* Git

Tool resources are immutable and versioned.

---

# 7. Tool Contract

Every Tool defines a contract.

The contract includes:

## Metadata

* identifier
* version
* description
* owner

---

## Input Schema

Defines accepted parameters.

Example:

```yaml id="gw5pif"
repository:
branch:
commit:
```

---

## Output Schema

Defines structured results.

Example:

```yaml id="vw1cgb"
files:
status:
logs:
```

---

## Execution Requirements

Defines:

* runtime image
* timeout
* retry policy
* required permissions

---

## Capability Metadata

Examples:

* read-only
* write
* network
* privileged

The Tool Runtime uses these capabilities for scheduling and Pre-Execution Capability Policy evaluation.

---

# 8. Tool Runtime

The Tool Runtime executes Tool invocations.

Responsibilities include:

* schema validation
* permission enforcement
* sandbox provisioning
* execution
* retries
* timeout handling
* logging
* metrics
* cleanup

The runtime never performs business logic.

---

# 9. Tool Invocation

Tool invocations are runtime objects.

Lifecycle:

```text id="k4b3t5"
Requested

↓

Validated

↓

Authorized

↓

Scheduled

↓

Running

↓

Completed
```

Failure path:

```text id="ay5mku"
Running

↓

Failed

↓

Retry

↓

Completed
```

ToolInvocation records complete execution history.

---

# 10. Tool Categories

The platform recognizes three categories.

## Data Source

Read-only non-model capabilities.

Examples:

* GitHub
* Filesystem
* Search

Agents must not use Data Source Tools to retrieve repository knowledge directly.

Repository and knowledge retrieval is performed by the Context Builder through deterministic ContextPackage construction.

---

## Execution

Runs code.

Examples:

* Python
* Docker
* Shell
* Build Systems

---

## External Services

Communicates with external systems.

Examples:

* Slack
* Email
* Jira

---

Categories enable different scheduling and permission policies.

---

# 11. Sandboxing

Every Tool executes inside an isolated execution environment.

The default sandbox is a Docker container managed by Kubernetes.

Isolation boundaries include:

* filesystem
* network
* process
* memory
* CPU
* environment variables

No Tool executes directly on the host machine.

---

# 12. Permission Model

Pre-Execution Capability Policy is evaluated before every Tool invocation.

Permissions may originate from:

* Tool
* Agent
* Task
* Workflow
* Workspace
* Platform Policy

Permission evaluation follows the principle of least privilege.

The shared Tool Runtime authorization hook evaluates every requested
capability and persists one `PolicyDecision` per capability before adapter
startup. Only `ALLOW` proceeds to execution. `DENY`, `REQUIRE_APPROVAL`, and
the absence of a matching grant all block the adapter.

---

# 13. Capability-Based Security

Permissions are expressed as capabilities rather than identities.

Examples include:

```text id="zr4s3p"
filesystem.read

filesystem.write

git.push

docker.run

github.create_pr

kubernetes.deploy
```

Agents receive only the capabilities explicitly granted.

---

# 14. Approval Gates

Certain capabilities require human approval.

Examples:

* merge pull request
* deploy application
* install tool
* modify production resources

Approval occurs before Tool execution as part of Pre-Execution Capability Policy.

The Tool Runtime never bypasses platform policies.

---

# 15. Secrets

Tools never access secrets directly.

Instead:

```text id="hn6ttb"
Tool

↓

Secret Provider

↓

Temporary Credentials

↓

Execution

↓

Credential Revocation
```

Secrets are injected only for the duration of execution.

Secret values are never exposed to Agents.

---

# 16. Execution Environment

Each Tool specifies execution requirements.

Examples include:

* Python version
* Operating system
* Container image
* Resource limits

The Tool Runtime provisions an appropriate execution environment.

---

# 17. Observability

Every Tool invocation records:

* Tool version
* execution ID
* caller
* input parameters
* output
* execution time
* retry count
* resource usage
* logs

These records provide complete auditability.

---

# 18. Failure Handling

Failures are classified into:

Recoverable

* network
* timeout
* rate limit

Configuration

* invalid schema
* missing permissions

Permanent

* unsupported operation
* policy violation

Failure classification determines retry behavior.

---

# 19. Extensibility

New Tools require only:

* Tool definition
* execution adapter
* schema

No changes to the Workflow Runtime are required.

This allows independent evolution of platform capabilities.

---

# 20. Runtime Interfaces

The Tool Runtime exposes a uniform interface regardless of implementation.

Adapters start Tools in an isolated process, container, Pod, or remote execution
and return a lifecycle handle to the Tool Runtime. The handle supports bounded
waiting, graceful termination, forced termination, and cleanup. A timeout is not
implemented by abandoning an in-process thread: the runtime terminates the
execution sandbox, waits for a bounded grace period, and kills it if necessary.
Cleanup runs for successful, failed, and timed-out executions.

Every Tool invocation follows the same lifecycle:

```text id="0djlwm"
Validate

↓

Authorize

↓

Provision

↓

Execute

↓

Collect Result

↓

Cleanup

↓

Publish Status
```

Individual Tool implementations remain interchangeable.

## Git Adapter

The repository-bound Git adapter exposes branch creation, status, diff,
read-only patch checking, and push operations. `check_patch` requires a clean
execution branch at the configured immutable revision. Patch bytes enter the
isolated sandbox over standard input and are inspected with `git apply
--numstat` and `git apply --check --cached`; the operation returns changed paths
and deterministic diagnostics without changing the index or worktree. Patch
Evaluation owns allowed-path and correctness decisions, while push remains a
separate capability-authorized operation.

The persisted Git Tool boundary atomically creates pending ToolInvocation
evidence before starting an adapter operation. Its deterministic request
fingerprint binds the task, caller, Tool version, input, capabilities, timeout,
trace, and policy decision. Matching retries and concurrent duplicates reuse
one terminal result; a reused identity with different inputs is rejected.

## GitHub Adapter

The MVP GitHub adapter exposes two structured operations:

* `readIssue` uses `github.issue.read` and returns normalized issue identity,
  content, labels, URL, provider request ID, attempt count, and trace ID.
* `createPullRequest` uses `github.create_pr` and accepts repository, head
  branch, base branch, title, and body. Branch creation and push remain Git Tool
  responsibilities.

Pull-request publication resolves immutable artifact, evaluation, and
Publication Policy records through a trusted verifier. The verifier binds the
CreatePullRequest task and PolicyDecision while allowing artifacts and
evaluations to preserve their distinct owning tasks. All evidence shares one
WorkflowExecution, repository revision, and trace. A successful Git push
ToolInvocation owned by the CreatePullRequest task must use an
immutable-version Git Tool and prove through matching input and output that the
exact approved head resolves to that revision. The verifier resolves the push's
persisted pre-execution PolicyDecision and requires `git.push`, `ALLOW`, and the
same task, workflow, revision, trace, and target. The publication decision
separately binds repository, head, base, `PUBLICATION` gate, and action before
`github.create_pr` authorization. Caller assertions or a changed target cannot
grant publication.

Provider calls return cancellable execution handles before network work can
block the Tool Runtime. Safe issue reads honor provider retry-after hints within
the single Tool deadline and record immutable evidence for every attempt.
Pull-request creation is not automatically replayed after an ambiguous provider
failure. Timeout handling returns frozen GitHub-specific evidence with the
provider request ID, trace, TIMEOUT attempt, and ambiguity flag, then terminates,
kills when needed, and cleans up the same provider operation without starting a
second publication.

---

# 21. Docker Validation Adapter

The Docker validation adapter accepts a digest-pinned image, ordered command
arguments, workspace bind mount, invocation timeout, and CPU and memory limits.
It requires the `docker.run` capability to pass the shared Pre-Execution
Capability Policy hook before provisioning begins.

Before startup, the adapter canonicalizes the requested mount source and
requires it to remain within its configured authorized workspace root after
traversal and symlink resolution. The container destination is fixed at
`/workspace`.

The production Docker CLI executor creates one invocation-scoped container with
networking disabled by default, the authorized mount, and configured CPU and
memory limits. Every command executes with `/workspace` as its working
directory. Create, start, and all commands consume one absolute invocation
deadline rather than resetting the timeout at each phase.

Its process and log storage boundaries remain injectable for
daemon-independent tests. The executor exposes bounded wait, terminate, kill,
and cleanup operations so the Tool Runtime retains lifecycle control, plus
startup cleanup for partially provisioned resources. Each command result
records its arguments, stdout, stderr, exit code, duration, and logs reference.
If a later command times out, evidence and the immutable logs reference for
completed commands remain on the timed-out result while termination and cleanup
continue. If cleanup itself fails, the shared Tool Runtime changes the terminal
classification to an adapter failure and appends the cleanup error without
discarding captured output, logs, metrics, or timing. Startup, timeout, and
nonzero-exit failures are classified separately. These records are execution
evidence; build and test acceptance is evaluated separately.

# 22. MVP Filesystem Adapter

The Filesystem adapter exposes schema-declared UTF-8 `read` and `write`
operations against one explicitly configured WorkflowExecution workspace. It
normalizes output paths relative to that workspace and returns byte counts and
SHA-256 content digests.

Absolute paths, traversal components, dangling symlinks, and resolved symlink
targets outside the workspace are denied. POSIX implementations walk from a
pinned workspace directory handle using no-follow relative opens. Windows
implementations pin and verify the workspace and parent directory handles,
reject reparse points, and open or create the final child relative to the
pinned parent with the native API. The kernel-resolved final handle is verified
before reading, truncating, or writing, so replacing either a final or
intermediate path cannot redirect an effect. A write request must declare
`filesystem.write`, and the shared Pre-Execution Capability Policy hook must
authorize it before the adapter starts. Structured content-addressed logs omit
file contents, while terminal `ToolInvocation` records preserve inputs,
structured outputs, metrics, log addresses, and normalized failure evidence.

This adapter performs file access for authorized workflow operations. It is not
a repository-knowledge retrieval path for Agents: `AgentInvocation` read
requests are denied regardless of capability policy. Only explicit
`ContextBuilder`, `TaskExecution`, and `WorkflowRuntime` control-plane caller
contracts may read; repository knowledge remains supplied to Agents through
immutable ContextPackages.

In one atomic store operation, the Tool Runtime creates pending evidence that
binds each invocation id to an immutable-request fingerprint and an ownership
token before any file effect. A failed atomic create leaves no separate claim
that can strand the invocation. Identical retries, including concurrent
duplicates, reuse the terminal result. Reusing an id with different inputs is
an identity conflict.

---

# 23. Design Principles

## Declarative Tools

Tools define capabilities, not execution.

---

## Runtime Ownership

Only the Tool Runtime performs execution.

---

## Isolation by Default

Every Tool executes inside an isolated sandbox.

---

## Least Privilege

Agents receive only the capabilities required for a Task.

---

## Uniform Contracts

Every Tool exposes the same lifecycle regardless of implementation.

---

## Observable Execution

Every external action produces a complete audit trail.

---

# 23. Future Enhancements

The architecture supports future capabilities without changing the Tool contract.

Potential enhancements include:

* Remote execution clusters
* GPU scheduling
* Distributed Tool workers
* Resource quotas
* Tool marketplaces
* Cost-aware scheduling
* Execution caching
* Policy simulation

---

# 24. Summary

The Tool Platform provides a secure, declarative execution layer for non-model interactions with external systems.

Tool resources describe reusable capabilities, while the Tool Runtime validates requests, enforces permissions, provisions isolated execution environments, and manages the complete execution lifecycle.

This separation ensures that external actions remain deterministic, observable, governable, and independent of workflow or Agent implementation.
