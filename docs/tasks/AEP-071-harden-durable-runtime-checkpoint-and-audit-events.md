# AEP-071: Harden Durable Runtime Checkpoint and Audit Events

**Status:** In Progress

## Context

The controlled GitHub issue #109 execution at repository revision
`aaed84323b8dab33372f723d3af2de0410b1c7fd` exposed a control-plane
durability failure before `BuildImplementationPlan` began. WorkflowExecution
`workflowexecution-64b93420-8653-53f4-be92-986d4c5db6c6` created attempt 1,
`taskexecution-0f676b74-4e97-55f9-96e1-f6437d16797d`, then recorded:

```text
RECOVERABLE: could not record TaskExecutionStarted: RuntimeStoreError
```

This wording is misleading. The scheduler first successfully applied the
`PENDING` to `RUNNING` lifecycle transition; the persisted TaskExecution has
`startedAt`. It then failed while appending the `TaskExecutionStarted`
ExecutionEvent. No planner AgentInvocation, ContextPackage, or evaluation
exists for attempt 1. The scheduler deliberately converted the audit write
failure into a retryable task failure rather than execute work whose start
evidence was not durably recorded.

The next attempt began ten seconds later, proving that the store was usable
again. It later failed for the independent AEP-068 planning-evidence condition
`required-insertion criterion cannot rely on unsupported path evidence`.
That planning failure is out of scope for this task: fixing it must not hide
or conflate the failed audit checkpoint.

The self-hosted MVP uses `DurableJsonRuntimeObjectStore` for
`runtime/objects.json`. Each mutation serializes the complete snapshot to the
fixed sibling temporary name `objects.json.tmp`, then calls `os.replace`.
The adapter has only a process-local lock and catches every `OSError`,
discarding its errno and operating-system message before raising the generic
`RuntimeStoreError`. As a result, the evidence cannot distinguish a temporary
write failure, replacement failure, shared-volume behavior, or contention
between writer processes. A fixed temporary name is additionally unsafe if
the claimed single-writer deployment invariant is violated.

The durable runtime store is execution evidence, not an optional log sink.
Lifecycle state, execution events, deterministic keys, claims, and indexes
must remain mutually consistent through process restart and transient storage
failure. A retry must not cause task work or external effects to run merely
because recording a start event was partially completed.

Preserve WorkflowExecution `workflowexecution-64b93420-8653-53f4-be92-986d4c5db6c6`
and both BuildImplementationPlan attempts as immutable historical diagnostic
evidence. Do not replay, rewrite, or reclassify them.

## Reproduction

Create credential-free deterministic tests against a durable runtime-store
directory and a scheduler with a no-op task executor.

1. Create a workflow and pending TaskExecution. Inject an `OSError` while
   persisting the `TaskExecutionStarted` ExecutionEvent after the lifecycle
   transition would otherwise become durable.
2. Restart the runtime from the same durable state. Verify that it reconciles
   the incomplete lifecycle/event boundary deterministically and does not run
   the task body or invoke an external Tool before durable start evidence is
   available.
3. Inject failures separately for temporary-file creation/write, flush/sync,
   replacement, and directory sync where supported. Assert bounded structured
   diagnostics identify the persistence operation and sanitized OS failure
   category or errno without exposing paths outside configured state roots,
   credentials, source bodies, or unrestricted exception text.
4. Exercise two independently constructed store instances against the same
   state root. Demonstrate the selected single-writer fence or transactional
   durable backend prevents lost updates, temporary-name collision, and
   divergent deterministic-key or claim state.
5. Retry the failed start boundary repeatedly and restart between retries.
   Verify one logical TaskExecution start event, one task body execution, and
   no duplicate external publication or Tool invocation.
6. Simulate a terminal-event persistence failure and a crash after the task
   body returns. Verify reconciliation preserves the immutable terminal result
   and idempotently repairs its event rather than re-running side effects.

Fixtures and injected failures must be deterministic and credential-free.
They must not retain live provider requests, secrets, unrestricted logs, or
artifact bodies.

## Deliverable

Implement a durable runtime-persistence boundary that:

* replaces the fixed-name, process-local JSON checkpoint assumption with an
  explicit single-writer lease/fence plus collision-safe checkpoint protocol,
  or a provider-neutral transactional durable RuntimeObjectStore backend;

* atomically commits, or deterministically reconciles, each TaskExecution
  lifecycle transition with its corresponding ExecutionEvent and workflow
  attachment;

* prevents task dispatch until durable start evidence exists, and makes retry
  or restart repair an incomplete start boundary without ambiguous duplicate
  task execution;

* persists idempotent event identity, deterministic-key, claim, lifecycle,
  and artifact-attachment state without lost updates across restart or
  competing writer attempts;

* reports bounded, safe persistence diagnostics including operation phase and
  sanitized failure category/errno, while preserving the original exception as
  an internal cause for operators; and

* updates RuntimeObjectStore contracts, scheduler recovery, dogfood deployment
  configuration, operator inspection, structured observability, schemas,
  fixtures, and self-hosting documentation together.

Do not solve this task by treating audit-event persistence as best-effort,
silently swallowing checkpoint errors, retrying a task body after ambiguous
start or terminal evidence, reusing a shared fixed temporary file, exposing
raw host paths or exception bodies, weakening immutable runtime evidence, or
changing the independent AEP-068 planning-evidence outcome.

## Dependencies

* AEP-002
* AEP-004
* AEP-008
* AEP-010
* AEP-011
* AEP-018
* AEP-036
* AEP-040
* AEP-056
* AEP-058

## Acceptance Criteria

* A failure while recording `TaskExecutionStarted` is reported as an
  ExecutionEvent persistence failure, not as an ambiguous task-start failure.
  Its durable diagnostic evidence identifies the checkpoint phase and a safe
  OS failure category or errno.

* The TaskExecution lifecycle transition, owning WorkflowExecution attachment,
  and required start/terminal ExecutionEvents are atomically durable or are
  repaired idempotently at restart before dispatch. No completed or running
  TaskExecution is left with an irreconcilable missing event.

* When start-event persistence fails, the task handler, AgentInvocation,
  ToolInvocation, and external side effect have not begun. A later retry runs
  the task body at most once after one durable logical start boundary.

* A terminal-event persistence interruption after task work completes is
  reconciled from immutable terminal evidence without re-running an external
  effect, creating a duplicate artifact, or creating a duplicate pull request.

* Two independently constructed durable store instances cannot corrupt the
  checkpoint, overwrite one another through a shared temporary path, lose a
  deterministic key or claim, or both dispatch the same task. The selected
  fence or transactional backend has deterministic contention behavior.

* Temporary-write, replacement, sync, contention, restart, start-event, and
  terminal-event fault-injection tests are deterministic and cover safe
  diagnostics, recovery, idempotency, and failure classification.

* Existing RuntimeObjectStore conformance, TaskExecution lifecycle, scheduler,
  event deduplication, execution inspection, resume, publication, and
  self-hosting tests continue to pass.

* A new controlled self-hosting generation records exactly one durable
  `TaskExecutionStarted` event for each dispatched task and retains enough
  bounded evidence to diagnose a forced checkpoint failure without exposing
  source or artifact bodies.

* WorkflowExecution `workflowexecution-64b93420-8653-53f4-be92-986d4c5db6c6`
  remains immutable historical evidence and is not replayed or rewritten.
