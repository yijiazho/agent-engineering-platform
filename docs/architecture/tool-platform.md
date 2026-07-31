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
the authorized mount and configured CPU and memory limits. Its process and log
storage boundaries remain injectable for daemon-independent tests. The executor
exposes bounded wait, terminate, kill, and cleanup operations so the Tool
Runtime retains lifecycle control, plus startup cleanup for partially
provisioned resources. Each command result records its arguments, stdout,
stderr, exit code, duration, and logs reference. Startup, timeout, and
nonzero-exit failures are classified separately. These records are execution
evidence; build and test acceptance is evaluated separately.

---

# 22. Design Principles

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
