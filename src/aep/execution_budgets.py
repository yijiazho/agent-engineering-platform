"""Deterministic execution budgets for model-backed governed work.

Budgets are declarative Resource limits. The control plane resolves them by
intersecting every applicable scope, charges durable consumption, and blocks
new work before dispatch. Agents never decide whether to honour a budget.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import Any


BUDGET_FIELDS = (
    "maxModelInvocations",
    "maxInputTokens",
    "maxOutputTokens",
    "maxElapsedMs",
    "maxCostUsd",
)
COST_STATUSES = ("EXACT", "CONFIGURED", "ESTIMATED", "UNAVAILABLE", "MIXED")
BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
BUDGET_COST_UNAVAILABLE = "BUDGET_COST_UNAVAILABLE"
BUDGET_ALLOWED = "BUDGET_ALLOWED"

_SCOPE_SPECIFICITY = {"Execution": 0, "Task": 1, "Agent": 2, "Workflow": 3}


class ExecutionBudgetError(ValueError):
    """Raised when a budget contract is malformed or inconsistent."""


class ExecutionBudgetContractError(ExecutionBudgetError):
    """Raised when persisted budget evidence conflicts with immutable inputs."""


class ExecutionBudgetExceeded(ExecutionBudgetError):
    """Raised before dispatch when a hard budget cannot admit the invocation."""

    def __init__(self, decision: Mapping[str, Any]) -> None:
        super().__init__(str(decision.get("message", BUDGET_EXHAUSTED)))
        self.decision = deepcopy(dict(decision))


@dataclass(frozen=True)
class BudgetLimit:
    """One effective limit and the rule that established it."""

    field: str
    value: int | float
    scope: str
    resource_ref: Mapping[str, str] | None = None

    def as_record(self) -> dict[str, Any]:
        record: dict[str, Any] = {"value": self.value, "scope": self.scope}
        if self.resource_ref is not None:
            record["resourceRef"] = dict(self.resource_ref)
        return record


@dataclass(frozen=True)
class ResolvedExecutionBudget:
    """Effective limits for one Task execution, separated by enforcement scope."""

    workflow: Mapping[str, BudgetLimit | None]
    task: Mapping[str, BudgetLimit | None]
    agent: Mapping[str, BudgetLimit | None]
    model_output_token_limit: int | None = None
    model_pricing: Mapping[str, Any] | None = None
    resource_refs: tuple[Mapping[str, str], ...] = ()

    def has_limits(self) -> bool:
        return any(
            limit is not None
            for scope in (self.workflow, self.task, self.agent)
            for limit in scope.values()
        )

    def scopes(self) -> dict[str, dict[str, Any]]:
        return {
            "workflow": _scope_record(self.workflow),
            "task": _scope_record(self.task),
            "agent": _scope_record(self.agent),
        }

    def workflow_record(self, resolved_at: str) -> dict[str, Any]:
        return {
            "limits": _scope_record(self.workflow),
            "resolvedAt": resolved_at,
            "resourceRefs": [
                dict(ref) for ref in self.resource_refs if ref.get("kind") == "Workflow"
            ],
        }

    def task_record(self, resolved_at: str) -> dict[str, Any]:
        return {
            "scopes": self.scopes(),
            "modelOutputTokenLimit": self.model_output_token_limit,
            "pricing": deepcopy(dict(self.model_pricing))
            if self.model_pricing is not None
            else None,
            "resolvedAt": resolved_at,
            "resourceRefs": [dict(ref) for ref in self.resource_refs],
        }


@dataclass(frozen=True)
class BudgetReservation:
    """One durable pre-dispatch reservation reconciled after the invocation."""

    effective_output_token_limit: int | None
    max_provider_attempts: int | None
    estimated_input_tokens: int
    reserved_output_tokens: int
    reserved_cost_usd: float
    decision: Mapping[str, Any]


def resolve_execution_budget(
    *,
    workflow_budget: Mapping[str, Any] | None = None,
    task_budget: Mapping[str, Any] | None = None,
    agent_budget: Mapping[str, Any] | None = None,
    execution_budget: Mapping[str, Any] | None = None,
    workflow_ref: Mapping[str, Any] | None = None,
    task_ref: Mapping[str, Any] | None = None,
    agent_ref: Mapping[str, Any] | None = None,
    model_token_limit: int | None = None,
    model_pricing: Mapping[str, Any] | None = None,
) -> ResolvedExecutionBudget:
    """Resolve Workflow, Task, Agent, Model, and execution limits deterministically.

    Every limit is a hard ceiling. Child scopes may narrow a parent scope but
    may not widen it. The most restrictive declared value wins; equal values are
    attributed to the most specific scope.
    """

    declared_workflow = _declared_limits(workflow_budget, "Workflow", workflow_ref)
    execution_limits = _declared_limits(execution_budget, "Execution", None)
    task_limits = _declared_limits(task_budget, "Task", task_ref)
    agent_limits = _declared_limits(agent_budget, "Agent", agent_ref)

    # Validate the declared Resource hierarchy before applying an execution
    # override. An override may narrow admission, but it must not make an
    # otherwise valid Task or Agent declaration appear to widen its parent.
    if execution_limits:
        _reject_widening(
            declared_workflow,
            execution_limits,
            parent_scope="Workflow",
            child_scope="Execution",
        )
    if task_limits:
        _reject_widening(
            declared_workflow,
            task_limits,
            parent_scope="Workflow",
            child_scope="Task",
        )
    if agent_limits:
        _reject_widening(
            declared_workflow,
            agent_limits,
            parent_scope="Workflow",
            child_scope="Agent",
        )
        if task_limits:
            _reject_widening(
                task_limits,
                agent_limits,
                parent_scope="Task",
                child_scope="Agent",
            )

    effective_workflow = (
        _most_restrictive(declared_workflow, execution_limits)
        if execution_limits
        else declared_workflow
    )
    effective_task = _most_restrictive(effective_workflow, task_limits)
    # Task limits belong to the Task ledger. An Agent declaration alone bounds
    # the shared Agent ledger; a Task ceiling must not leak into another Task
    # that happens to use the same Agent.
    effective_agent = _most_restrictive(effective_workflow, agent_limits)

    refs: dict[tuple[str, str, str], Mapping[str, str]] = {}
    for ref in (workflow_ref, task_ref, agent_ref):
        if ref is None:
            continue
        key = tuple(str(ref.get(field, "")) for field in ("kind", "name", "version"))
        if all(key):
            refs[key] = {"kind": key[0], "name": key[1], "version": key[2]}

    return ResolvedExecutionBudget(
        workflow=effective_workflow,
        task=effective_task,
        agent=effective_agent,
        model_output_token_limit=_positive_int(
            model_token_limit, "Model.spec.tokenLimit"
        ),
        model_pricing=_model_pricing(model_pricing),
        resource_refs=tuple(refs[key] for key in sorted(refs)),
    )


def validate_budget_narrowing(
    *,
    workflow_budget: Mapping[str, Any] | None,
    task_budget: Mapping[str, Any] | None,
    agent_budget: Mapping[str, Any] | None,
    workflow_ref: Mapping[str, Any] | None = None,
    task_ref: Mapping[str, Any] | None = None,
    agent_ref: Mapping[str, Any] | None = None,
) -> None:
    """Reject a lower scope that declares a wider limit than its parent."""

    workflow_limits = _declared_limits(workflow_budget, "Workflow", workflow_ref)
    task_limits = _declared_limits(task_budget, "Task", task_ref)
    agent_limits = _declared_limits(agent_budget, "Agent", agent_ref)
    if task_limits:
        _reject_widening(
            workflow_limits,
            task_limits,
            parent_scope="Workflow",
            child_scope="Task",
        )
    if agent_limits:
        _reject_widening(
            workflow_limits,
            agent_limits,
            parent_scope="Workflow",
            child_scope="Agent",
        )
        if task_limits:
            _reject_widening(
                task_limits,
                agent_limits,
                parent_scope="Task",
                child_scope="Agent",
            )


class TaskBudgetManager:
    """Resolve and persist one Task's effective budget before invocation."""

    def __init__(self, store: Any, resources: Any, clock: Callable[[], str]) -> None:
        self._store = store
        self._resources = resources
        self._clock = clock

    def prepare(
        self,
        *,
        workflow_execution: Mapping[str, Any],
        task_execution: Mapping[str, Any],
        task: Any,
        agent: Any,
        model: Any,
    ) -> "TaskBudget | None":
        workflow_resource = self._resource(workflow_execution.get("workflowRef"))
        task_spec = task.data.get("spec") if isinstance(task.data.get("spec"), Mapping) else {}
        agent_spec = agent.data.get("spec") if isinstance(agent.data.get("spec"), Mapping) else {}
        model_spec = model.data.get("spec") if isinstance(model.data.get("spec"), Mapping) else {}
        workflow_spec = (
            workflow_resource.data.get("spec")
            if workflow_resource is not None
            and isinstance(workflow_resource.data.get("spec"), Mapping)
            else {}
        )
        workflow_ref = _reference_record(workflow_execution.get("workflowRef"))
        task_ref = _resource_reference(task)
        agent_ref = _resource_reference(agent)
        resolved = resolve_execution_budget(
            workflow_budget=workflow_spec.get("budget"),
            task_budget=task_spec.get("budget"),
            agent_budget=agent_spec.get("budget"),
            execution_budget=workflow_execution.get("budgetOverrides"),
            workflow_ref=workflow_ref,
            task_ref=task_ref,
            agent_ref=agent_ref,
            model_token_limit=model_spec.get("tokenLimit"),
            model_pricing=model_spec.get("pricing"),
        )
        if not resolved.has_limits():
            return None
        timestamp = self._clock()
        self._initialize_evidence(
            workflow_execution,
            task_execution,
            resolved,
            timestamp,
        )
        return TaskBudget(
            store=self._store,
            workflow_execution_id=str(workflow_execution["id"]),
            task_execution_id=str(task_execution["id"]),
            task_key=_canonical_ref(task_ref),
            agent_key=_canonical_ref(agent_ref),
            resolved=resolved,
            provider=str(model_spec.get("provider", "")),
            model=str(model_spec.get("model", "")),
        )

    def _resource(self, ref: Mapping[str, Any] | None) -> Any:
        if not isinstance(ref, Mapping):
            return None
        from aep.resource_loader import ResourceRef

        try:
            return self._resources.get(ResourceRef.from_mapping(dict(ref)))
        except (KeyError, TypeError, ValueError):
            return None

    def _initialize_evidence(
        self,
        workflow_execution: Mapping[str, Any],
        task_execution: Mapping[str, Any],
        resolved: ResolvedExecutionBudget,
        timestamp: str,
    ) -> None:
        expected_workflow = resolved.workflow_record(timestamp)
        expected_task = resolved.task_record(timestamp)

        def mutate(
            workflow: Mapping[str, Any], task: Mapping[str, Any]
        ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
            workflow_changes: dict[str, Any] = {}
            task_changes: dict[str, Any] = {}
            existing_workflow = workflow.get("executionBudget")
            if existing_workflow is None:
                workflow_changes["executionBudget"] = deepcopy(expected_workflow)
            elif _without_resolution_time(existing_workflow) != _without_resolution_time(
                expected_workflow
            ):
                raise ExecutionBudgetContractError(
                    "WorkflowExecution.executionBudget does not match the resolved "
                    "execution budget"
                )
            existing_task = task.get("executionBudget")
            if existing_task is None:
                task_changes["executionBudget"] = deepcopy(expected_task)
            elif _without_resolution_time(existing_task) != _without_resolution_time(
                expected_task
            ):
                raise ExecutionBudgetContractError(
                    "TaskExecution.executionBudget does not match the resolved "
                    "execution budget"
                )
            return workflow_changes, task_changes

        self._store.mutate_execution_budget(
            str(workflow_execution["id"]),
            str(task_execution["id"]),
            mutate=mutate,
            updated_at=timestamp,
        )


class TaskBudget:
    """Durable budget ledger bound to one WorkflowExecution and Task identity."""

    def __init__(
        self,
        *,
        store: Any,
        workflow_execution_id: str,
        task_execution_id: str,
        task_key: str,
        agent_key: str,
        resolved: ResolvedExecutionBudget,
        provider: str,
        model: str,
    ) -> None:
        self._store = store
        self.workflow_execution_id = workflow_execution_id
        self.task_execution_id = task_execution_id
        self.task_key = task_key
        self.agent_key = agent_key
        self.resolved = resolved
        self.provider = provider
        self.model = model
        self._reservation: dict[str, Any] | None = None

    @property
    def scopes(self) -> Mapping[str, Mapping[str, BudgetLimit | None]]:
        return {
            "workflow": self.resolved.workflow,
            "task": self.resolved.task,
            "agent": self.resolved.agent,
        }

    def preflight(
        self,
        *,
        estimated_input_tokens: int,
        configured_output_token_limit: int | None,
        configured_provider_attempts: int | None = None,
        timestamp: str,
    ) -> BudgetReservation:
        """Reserve budget or raise ``ExecutionBudgetExceeded`` before dispatch."""

        estimated = max(1, int(estimated_input_tokens))
        outcome: dict[str, Any] = {}

        def mutate(
            workflow: Mapping[str, Any], task: Mapping[str, Any]
        ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
            state = self._load_state(workflow, task, timestamp)
            decision, _ = self._evaluate(
                state,
                estimated_input_tokens=estimated,
                configured_output_token_limit=configured_output_token_limit,
                timestamp=timestamp,
            )
            if decision is not None:
                self._apply_block_decision(state, decision)
                outcome["decision"] = decision
                return self._changes_for_state(workflow, state, decision=decision)

            (
                max_provider_attempts,
                effective_output,
                reserved_input,
                reserved_output,
                reserved_cost,
            ) = self._reservation_plan(
                state,
                estimated_input_tokens=estimated,
                configured_output_token_limit=configured_output_token_limit,
                configured_provider_attempts=configured_provider_attempts,
            )
            reserved_invocations = max_provider_attempts
            for record in state["records"]:
                record["reservedModelInvocations"] += reserved_invocations
                record["reservedInputTokens"] += reserved_input
                record["reservedOutputTokens"] += reserved_output
                record["reservedCostUsd"] += reserved_cost
                record["updatedAt"] = timestamp
                record["lastDecision"] = {
                    "allowed": True,
                    "reason": BUDGET_ALLOWED,
                    "effectiveOutputTokenLimit": effective_output,
                    "estimatedInputTokens": estimated,
                    "decidedAt": timestamp,
                }
            outcome["reservation"] = BudgetReservation(
                effective_output_token_limit=effective_output,
                max_provider_attempts=max_provider_attempts,
                estimated_input_tokens=estimated,
                reserved_output_tokens=reserved_output,
                reserved_cost_usd=reserved_cost,
                decision={
                    "allowed": True,
                    "reason": BUDGET_ALLOWED,
                    "effectiveOutputTokenLimit": effective_output,
                    "estimatedInputTokens": estimated,
                    "decidedAt": timestamp,
                },
            )
            outcome["reservedModelInvocations"] = reserved_invocations
            outcome["reservedInputTokens"] = reserved_input
            outcome["reservedOutputTokens"] = reserved_output
            outcome["reservedCostUsd"] = reserved_cost
            return self._changes_for_state(workflow, state)

        self._store.mutate_execution_budget(
            self.workflow_execution_id,
            self.task_execution_id,
            mutate=mutate,
            updated_at=timestamp,
        )
        if "decision" in outcome:
            raise ExecutionBudgetExceeded(outcome["decision"])
        self._reservation = {
            "modelInvocations": outcome["reservedModelInvocations"],
            "inputTokens": outcome["reservedInputTokens"],
            "outputTokens": outcome["reservedOutputTokens"],
            "costUsd": outcome["reservedCostUsd"],
        }
        return outcome["reservation"]

    def record(
        self,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        latency_ms: int = 0,
        cost: float | None = None,
        cost_status: str | None = None,
        invocation_count: int = 1,
        unreconciled_dispatched_attempts: int = 0,
        timestamp: str,
    ) -> dict[str, Any]:
        """Reconcile reservation with actual provider usage and persist evidence."""

        if input_tokens < 0 or output_tokens < 0 or latency_ms < 0:
            raise ExecutionBudgetError("model usage must not be negative")
        if invocation_count < 0:
            raise ExecutionBudgetError("invocation_count must not be negative")
        if unreconciled_dispatched_attempts < 0:
            raise ExecutionBudgetError(
                "unreconciled_dispatched_attempts must not be negative"
            )
        reservation = self._reservation or {
            "modelInvocations": 1,
            "inputTokens": 0,
            "outputTokens": 0,
            "costUsd": 0.0,
        }
        dispatched = invocation_count > 0
        status, actual_cost = (
            _cost_evidence(
                cost,
                cost_status,
                self.resolved.model_pricing,
                input_tokens,
                output_tokens,
            )
            if dispatched
            else ("UNAVAILABLE", None)
        )
        outcome: dict[str, Any] = {}

        def mutate(
            workflow: Mapping[str, Any], task: Mapping[str, Any]
        ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
            state = self._load_state(workflow, task, timestamp)
            for scope_name in state["scopeNames"]:
                record = state[scope_name]
                record["reservedModelInvocations"] = max(
                    0,
                    record["reservedModelInvocations"]
                    - int(reservation.get("modelInvocations", 1)),
                )
                if unreconciled_dispatched_attempts == 0:
                    record["reservedInputTokens"] = max(
                        0,
                        record["reservedInputTokens"] - int(reservation["inputTokens"]),
                    )
                    record["reservedOutputTokens"] = max(
                        0,
                        record["reservedOutputTokens"] - int(reservation["outputTokens"]),
                    )
                    record["reservedCostUsd"] = max(
                        0.0,
                        record["reservedCostUsd"] - float(reservation["costUsd"]),
                    )
                record["modelInvocations"] += invocation_count
                record["inputTokens"] += input_tokens
                record["outputTokens"] += output_tokens
                record["elapsedMs"] = max(
                    record["elapsedMs"], _elapsed_ms(record["startedAt"], timestamp)
                )
                record["updatedAt"] = timestamp
                record["lastProvider"] = self.provider
                record["lastModel"] = self.model
                if dispatched:
                    if actual_cost is not None:
                        record["costUsd"] = round(record["costUsd"] + actual_cost, 12)
                    counts = dict(record.get("costStatusCounts", {}))
                    counts[status] = counts.get(status, 0) + 1
                    record["costStatusCounts"] = counts
                    record["costStatus"] = (
                        next(iter(counts)) if len(counts) == 1 else "MIXED"
                    )
                    cost_limit = getattr(self.resolved, scope_name).get("maxCostUsd")
                    unreconciled = (
                        status == "UNAVAILABLE"
                        or unreconciled_dispatched_attempts > 0
                    )
                    if unreconciled and cost_limit is not None:
                        decision = _blocked_decision(
                            scope_name,
                            cost_limit,
                            consumed=record["costUsd"] + record["reservedCostUsd"],
                            reserved=record["reservedCostUsd"],
                            required=0,
                            timestamp=timestamp,
                            reason=BUDGET_COST_UNAVAILABLE,
                            message=(
                                "monetary budget cannot be reconciled because a "
                                "dispatched provider attempt returned no reliable usage"
                            ),
                            sticky=True,
                        )
                        record["blockedReason"] = BUDGET_COST_UNAVAILABLE
                        record["blockedDecision"] = decision
                record["lastDecision"] = {
                    "allowed": True,
                    "reason": BUDGET_ALLOWED,
                    "decidedAt": timestamp,
                }
            self._apply_overruns(state)
            outcome["effectiveOutputTokenLimit"] = _effective_output_limit(
                self.resolved.model_output_token_limit,
                self._minimum_remaining_output(state),
            )
            outcome["remaining"] = self._remaining_by_scope(state)
            return self._changes_for_state(workflow, state)

        self._store.mutate_execution_budget(
            self.workflow_execution_id,
            self.task_execution_id,
            mutate=mutate,
            updated_at=timestamp,
        )
        self._reservation = None
        return {
            "invocationCount": invocation_count,
            "unreconciledDispatchedAttempts": unreconciled_dispatched_attempts,
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "latencyMs": latency_ms,
            "costUsd": actual_cost,
            "costStatus": status,
            "provider": self.provider,
            "model": self.model,
            "chargedAt": timestamp,
            "effectiveOutputTokenLimit": outcome["effectiveOutputTokenLimit"],
            "remaining": outcome["remaining"],
        }

    def release(self) -> None:
        """Release an unused reservation without charging usage."""

        reservation = self._reservation
        if reservation is None:
            return
        timestamp = self._now()

        def mutate(
            workflow: Mapping[str, Any], task: Mapping[str, Any]
        ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
            state = self._load_state(workflow, task, timestamp)
            for record in state["records"]:
                record["reservedModelInvocations"] = max(
                    0,
                    record["reservedModelInvocations"]
                    - int(reservation.get("modelInvocations", 1)),
                )
                record["reservedInputTokens"] = max(
                    0, record["reservedInputTokens"] - int(reservation["inputTokens"])
                )
                record["reservedOutputTokens"] = max(
                    0, record["reservedOutputTokens"] - int(reservation["outputTokens"])
                )
                record["reservedCostUsd"] = max(
                    0.0, record["reservedCostUsd"] - float(reservation["costUsd"])
                )
            return self._changes_for_state(workflow, state)

        self._store.mutate_execution_budget(
            self.workflow_execution_id,
            self.task_execution_id,
            mutate=mutate,
            updated_at=timestamp,
        )
        self._reservation = None

    def _now(self) -> str:
        return datetime.now().astimezone().isoformat()

    def _changes_for_state(
        self,
        workflow: Mapping[str, Any],
        state: Mapping[str, Any],
        *,
        decision: Mapping[str, Any] | None = None,
    ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        existing = (
            deepcopy(dict(workflow.get("budgetConsumption", {})))
            if isinstance(workflow.get("budgetConsumption"), Mapping)
            else {}
        )
        tasks = (
            deepcopy(dict(existing.get("tasks", {})))
            if isinstance(existing.get("tasks"), Mapping)
            else {}
        )
        agents = (
            deepcopy(dict(existing.get("agents", {})))
            if isinstance(existing.get("agents"), Mapping)
            else {}
        )
        tasks[self.task_key] = deepcopy(state["task"])
        agents[self.agent_key] = deepcopy(state["agent"])
        consumption = {
            **existing,
            "workflow": deepcopy(state["workflow"]),
            "tasks": tasks,
            "agents": agents,
        }
        task_changes: dict[str, Any] = {
            "budgetConsumption": {
                "workflow": deepcopy(state["workflow"]),
                "task": deepcopy(state["task"]),
                "agent": deepcopy(state["agent"]),
            }
        }
        if decision is not None:
            consumption["lastDecision"] = deepcopy(dict(decision))
            task_changes["budgetDecision"] = deepcopy(dict(decision))
        return {"budgetConsumption": consumption}, task_changes

    def _apply_block_decision(
        self, state: Mapping[str, Any], decision: Mapping[str, Any]
    ) -> None:
        scope = str(
            decision.get("internalScope") or decision.get("scope", "")
        ).casefold()
        record = state.get(scope)
        if not isinstance(record, dict):
            return
        record["lastDecision"] = deepcopy(dict(decision))
        record["updatedAt"] = decision["decidedAt"]
        if decision.get("sticky") is True:
            record["blockedReason"] = decision["reason"]
            record["blockedDecision"] = deepcopy(dict(decision))

    def _load_state(
        self,
        workflow: Mapping[str, Any],
        task: Mapping[str, Any],
        timestamp: str,
    ) -> dict[str, Any]:
        consumption = (
            deepcopy(dict(workflow.get("budgetConsumption", {})))
            if isinstance(workflow.get("budgetConsumption"), Mapping)
            else {}
        )
        workflow_record = _consumption_record(
            consumption.get("workflow"),
            str(workflow.get("startedAt") or workflow.get("createdAt") or timestamp),
            self.provider,
            self.model,
        )
        tasks = consumption.get("tasks") if isinstance(consumption.get("tasks"), Mapping) else {}
        agents = consumption.get("agents") if isinstance(consumption.get("agents"), Mapping) else {}
        task_started_at = str(task.get("startedAt") or task.get("createdAt") or timestamp)
        task_record = _consumption_record(
            tasks.get(self.task_key), task_started_at, self.provider, self.model
        )
        agent_record = _consumption_record(
            agents.get(self.agent_key), task_started_at, self.provider, self.model
        )
        return {
            "workflow": workflow_record,
            "task": task_record,
            "agent": agent_record,
            "records": (workflow_record, task_record, agent_record),
            "scopeNames": ("workflow", "task", "agent"),
        }

    def _evaluate(
        self,
        state: Mapping[str, Any],
        *,
        estimated_input_tokens: int,
        configured_output_token_limit: int | None,
        timestamp: str,
    ) -> tuple[dict[str, Any] | None, int | None]:
        remaining_output: int | None = None
        for scope_name in state["scopeNames"]:
            limits = getattr(self.resolved, scope_name)
            record = state[scope_name]
            decision = _check_scope(
                scope_name,
                limits,
                record,
                estimated_input_tokens=estimated_input_tokens,
                configured_output_token_limit=configured_output_token_limit,
                timestamp=timestamp,
                pricing=self.resolved.model_pricing,
            )
            if decision is not None:
                return decision, remaining_output
            output_limit = limits.get("maxOutputTokens")
            if output_limit is not None:
                remaining = int(output_limit.value) - (
                    record["outputTokens"] + record["reservedOutputTokens"]
                )
                remaining_output = remaining if remaining_output is None else min(
                    remaining_output, remaining
                )
        return None, remaining_output

    def _apply_overruns(self, state: Mapping[str, Any]) -> None:
        for scope_name in ("workflow", "task", "agent"):
            limits = getattr(self.resolved, scope_name)
            record = state[scope_name]
            overrun = False
            for field, consumed_field, reserved_field in (
                ("maxModelInvocations", "modelInvocations", "reservedModelInvocations"),
                ("maxInputTokens", "inputTokens", "reservedInputTokens"),
                ("maxOutputTokens", "outputTokens", "reservedOutputTokens"),
                ("maxCostUsd", "costUsd", "reservedCostUsd"),
            ):
                limit = limits.get(field)
                if limit is not None and (
                    record[consumed_field] + record[reserved_field] > limit.value
                ):
                    overrun = True
            if overrun:
                record["overrun"] = True

    def _reservation_plan(
        self,
        state: Mapping[str, Any],
        *,
        estimated_input_tokens: int,
        configured_output_token_limit: int | None,
        configured_provider_attempts: int | None,
    ) -> tuple[int, int | None, int, int, float]:
        invocation_allowance = self._invocation_allowance(state)
        if configured_provider_attempts is not None:
            attempt_limit = max(1, int(configured_provider_attempts))
        elif invocation_allowance is not None:
            attempt_limit = invocation_allowance
        else:
            attempt_limit = 1
        if invocation_allowance is not None:
            attempt_limit = min(attempt_limit, invocation_allowance)

        remaining_input = self._minimum_remaining(
            state, "maxInputTokens", "inputTokens", "reservedInputTokens"
        )
        if remaining_input is not None:
            attempt_limit = min(
                attempt_limit, max(1, int(remaining_input) // estimated_input_tokens)
            )
        remaining_output = self._minimum_remaining(
            state, "maxOutputTokens", "outputTokens", "reservedOutputTokens"
        )
        if remaining_output is not None:
            attempt_limit = min(attempt_limit, max(1, int(remaining_output)))

        per_attempt_output = configured_output_token_limit
        if remaining_output is not None:
            output_cap = max(1, int(remaining_output) // attempt_limit)
            per_attempt_output = max(
                1,
                min(configured_output_token_limit or output_cap, output_cap),
            )

        remaining_cost = self._minimum_remaining(
            state, "maxCostUsd", "costUsd", "reservedCostUsd"
        )
        if remaining_cost is not None and self.resolved.model_pricing is not None:
            for _ in range(3):
                minimum_cost = _estimate_cost(
                    self.resolved.model_pricing,
                    estimated_input_tokens,
                    per_attempt_output or 1,
                )
                if minimum_cost <= 0:
                    break
                max_by_cost = int(float(remaining_cost) // minimum_cost)
                if max_by_cost >= attempt_limit:
                    break
                attempt_limit = max(1, max_by_cost)
                if remaining_output is not None:
                    output_cap = max(1, int(remaining_output) // attempt_limit)
                    per_attempt_output = max(
                        1,
                        min(configured_output_token_limit or output_cap, output_cap),
                    )

        reserved_input = estimated_input_tokens * attempt_limit
        reserved_output = (
            int(per_attempt_output or 0) * attempt_limit
            if remaining_output is not None
            else 0
        )
        reserved_cost = (
            _estimate_cost(
                self.resolved.model_pricing,
                reserved_input,
                reserved_output if remaining_output is not None else attempt_limit,
            )
            if self.resolved.model_pricing is not None
            else 0.0
        )
        return (
            attempt_limit,
            per_attempt_output,
            reserved_input,
            reserved_output,
            reserved_cost,
        )

    def _minimum_remaining(
        self,
        state: Mapping[str, Any],
        limit_field: str,
        consumed_field: str,
        reserved_field: str,
    ) -> int | float | None:
        remaining: int | float | None = None
        for scope_name in ("workflow", "task", "agent"):
            limit = getattr(self.resolved, scope_name).get(limit_field)
            if limit is None:
                continue
            record = state[scope_name]
            value = limit.value - (
                record[consumed_field] + record[reserved_field]
            )
            remaining = value if remaining is None else min(remaining, value)
        return remaining

    def _invocation_allowance(self, state: Mapping[str, Any]) -> int | None:
        allowance: int | None = None
        for scope_name in ("workflow", "task", "agent"):
            limit = getattr(self.resolved, scope_name).get("maxModelInvocations")
            if limit is None:
                continue
            record = state[scope_name]
            available = int(limit.value) - (
                record["modelInvocations"] + record["reservedModelInvocations"]
            )
            allowance = available if allowance is None else min(allowance, available)
        return allowance

    def _minimum_remaining_output(self, state: Mapping[str, Any]) -> int | None:
        remaining: int | None = None
        for scope_name in ("workflow", "task", "agent"):
            limit = getattr(self.resolved, scope_name).get("maxOutputTokens")
            if limit is None:
                continue
            record = state[scope_name]
            value = int(limit.value) - (
                record["outputTokens"] + record["reservedOutputTokens"]
            )
            remaining = value if remaining is None else min(remaining, value)
        return remaining

    def _remaining_by_scope(self, state: Mapping[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for scope_name in ("workflow", "task", "agent"):
            result[scope_name] = summarize_limits(
                getattr(self.resolved, scope_name), state[scope_name]
            )
        return result


def summarize_limits(
    limits: Mapping[str, BudgetLimit | None], consumption: Mapping[str, Any]
) -> dict[str, Any]:
    """Return configured, consumed, reserved, and remaining evidence."""

    configured: dict[str, Any] = {}
    consumed: dict[str, Any] = {}
    reserved: dict[str, Any] = {}
    remaining: dict[str, Any] = {}
    for field in BUDGET_FIELDS:
        limit = limits.get(field)
        if limit is None:
            continue
        configured[field] = limit.as_record()
        consumed_field, reserved_field = _consumption_fields(field)
        used = consumption.get(consumed_field, 0)
        held = consumption.get(reserved_field, 0)
        consumed[field] = used
        reserved[field] = held
        remaining[field] = round(limit.value - used - held, 12)
    return {
        "configured": configured,
        "consumed": consumed,
        "reserved": reserved,
        "remaining": remaining,
    }


def budget_evidence(
    limits: Mapping[str, Any] | None, consumption: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Summarize persisted limit/consumption evidence for AEP-056 inspection."""

    limits = limits if isinstance(limits, Mapping) else {}
    consumption = consumption if isinstance(consumption, Mapping) else {}
    configured: dict[str, Any] = {}
    used: dict[str, Any] = {}
    reserved: dict[str, Any] = {}
    remaining: dict[str, Any] = {}
    for field in BUDGET_FIELDS:
        rule = limits.get(field)
        if not isinstance(rule, Mapping):
            continue
        configured[field] = deepcopy(dict(rule))
        consumed_field, reserved_field = _consumption_fields(field)
        consumed_value = consumption.get(consumed_field, 0)
        reserved_value = consumption.get(reserved_field, 0)
        used[field] = consumed_value
        reserved[field] = reserved_value
        remaining[field] = round(
            float(rule.get("value", 0)) - consumed_value - reserved_value, 12
        )
    return {
        "configuredLimits": configured,
        "consumed": used,
        "reserved": reserved,
        "remaining": remaining,
        "costStatus": consumption.get("costStatus", "UNAVAILABLE"),
        "overrun": bool(consumption.get("overrun", False)),
        "blockedReason": consumption.get("blockedReason"),
        "blockedDecision": deepcopy(consumption.get("blockedDecision"))
        if isinstance(consumption.get("blockedDecision"), Mapping)
        else None,
        "lastProvider": consumption.get("lastProvider"),
        "lastModel": consumption.get("lastModel"),
        "startedAt": consumption.get("startedAt"),
        "updatedAt": consumption.get("updatedAt"),
    }


def _declared_limits(
    declarations: Mapping[str, Any] | None,
    scope: str,
    ref: Mapping[str, Any] | None,
) -> dict[str, BudgetLimit | None]:
    result: dict[str, BudgetLimit | None] = {field: None for field in BUDGET_FIELDS}
    if declarations is None:
        return result
    if not isinstance(declarations, Mapping):
        raise ExecutionBudgetError("execution budget must be an object")
    unknown = set(declarations) - set(BUDGET_FIELDS)
    if unknown:
        raise ExecutionBudgetError("execution budget contains unsupported fields")
    resource_ref = _reference_record(ref)
    for field in BUDGET_FIELDS:
        value = declarations.get(field)
        if value is None:
            continue
        if field == "maxCostUsd":
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not isfinite(value)
                or value <= 0
            ):
                raise ExecutionBudgetError(f"{field} must be a positive finite number")
        else:
            value = _positive_int(value, field)
        result[field] = BudgetLimit(
            field=field,
            value=value,
            scope=scope,
            resource_ref=resource_ref,
        )
    return result


def _positive_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ExecutionBudgetError(f"{field} must be a positive integer")
    return value


def _model_pricing(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ExecutionBudgetError("Model.spec.pricing must be an object")
    pricing = deepcopy(dict(value))
    for field in ("inputPerMillionTokens", "outputPerMillionTokens"):
        candidate = pricing.get(field)
        if (
            not isinstance(candidate, (int, float))
            or isinstance(candidate, bool)
            or not isfinite(candidate)
            or candidate < 0
        ):
            raise ExecutionBudgetError(f"pricing.{field} must be a non-negative number")
    if pricing.get("currency") != "USD":
        raise ExecutionBudgetError("pricing.currency must be USD")
    return pricing


def _most_restrictive(
    *scopes: Mapping[str, BudgetLimit | None],
) -> dict[str, BudgetLimit | None]:
    result: dict[str, BudgetLimit | None] = {}
    for field in BUDGET_FIELDS:
        candidates = [scope[field] for scope in scopes if scope.get(field) is not None]
        if not candidates:
            result[field] = None
            continue
        result[field] = min(
            candidates,
            key=lambda limit: (
                limit.value,
                _SCOPE_SPECIFICITY.get(limit.scope, 99),
                str(limit.resource_ref),
            ),
        )
    return result


def _reject_widening(
    parent: Mapping[str, BudgetLimit | None],
    child: Mapping[str, BudgetLimit | None],
    *,
    parent_scope: str,
    child_scope: str,
) -> None:
    for field in BUDGET_FIELDS:
        parent_limit = parent.get(field)
        child_limit = child.get(field)
        if parent_limit is None or child_limit is None:
            continue
        if child_limit.value > parent_limit.value:
            raise ExecutionBudgetError(
                f"{child_scope} {field} ({child_limit.value}) widens "
                f"{parent_scope} limit ({parent_limit.value})"
            )


def _check_scope(
    scope_name: str,
    limits: Mapping[str, BudgetLimit | None],
    record: Mapping[str, Any],
    *,
    estimated_input_tokens: int,
    configured_output_token_limit: int | None,
    timestamp: str,
    pricing: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    blocked = record.get("blockedDecision")
    if isinstance(blocked, Mapping) and blocked.get("sticky") is True:
        # Only unreconciled monetary spend is sticky. Ordinary exhaustion and
        # pre-dispatch pricing failures are reevaluated after reservations
        # release or a correctly priced Model is chosen.
        return deepcopy(dict(blocked))
    checks = (
        ("maxModelInvocations", "modelInvocations", "reservedModelInvocations", 1),
        ("maxInputTokens", "inputTokens", "reservedInputTokens", estimated_input_tokens),
        ("maxOutputTokens", "outputTokens", "reservedOutputTokens", 1),
    )
    elapsed = max(record.get("elapsedMs", 0), _elapsed_ms(record.get("startedAt"), timestamp))
    for field, consumed_field, reserved_field, required in checks:
        limit = limits.get(field)
        if limit is None:
            continue
        consumed = record.get(consumed_field, 0) + record.get(reserved_field, 0)
        if consumed + required > limit.value:
            return _blocked_decision(
                scope_name,
                limit,
                consumed=consumed,
                reserved=record.get(reserved_field, 0),
                required=required,
                timestamp=timestamp,
            )
    elapsed_limit = limits.get("maxElapsedMs")
    if elapsed_limit is not None and elapsed + 1 > elapsed_limit.value:
        return _blocked_decision(
            scope_name,
            elapsed_limit,
            consumed=elapsed,
            reserved=0,
            required=1,
            timestamp=timestamp,
        )
    cost_limit = limits.get("maxCostUsd")
    if cost_limit is not None:
        if pricing is None:
            return _blocked_decision(
                scope_name,
                cost_limit,
                consumed=record.get("costUsd", 0.0) + record.get("reservedCostUsd", 0.0),
                reserved=record.get("reservedCostUsd", 0.0),
                required=0,
                timestamp=timestamp,
                reason=BUDGET_COST_UNAVAILABLE,
                message="monetary budget requires reliable Model pricing metadata",
            )
        minimum_cost = _estimate_cost(pricing, estimated_input_tokens, 1)
        consumed_cost = record.get("costUsd", 0.0) + record.get("reservedCostUsd", 0.0)
        if consumed_cost + minimum_cost > cost_limit.value:
            return _blocked_decision(
                scope_name,
                cost_limit,
                consumed=consumed_cost,
                reserved=record.get("reservedCostUsd", 0.0),
                required=minimum_cost,
                timestamp=timestamp,
            )
    return None


def _blocked_decision(
    scope_name: str,
    limit: BudgetLimit,
    *,
    consumed: int | float,
    reserved: int | float,
    required: int | float,
    timestamp: str,
    reason: str = BUDGET_EXHAUSTED,
    message: str | None = None,
    sticky: bool = False,
) -> dict[str, Any]:
    remaining = round(float(limit.value) - float(consumed), 12)
    scope_label = limit.scope
    ref_label = (
        "/".join(str(limit.resource_ref.get(field, "")) for field in ("name", "version"))
        if isinstance(limit.resource_ref, Mapping)
        else "execution"
    )
    return {
        "allowed": False,
        "reason": reason,
        "dimension": limit.field,
        "scope": scope_label,
        "internalScope": scope_name,
        "resourceRef": dict(limit.resource_ref)
        if isinstance(limit.resource_ref, Mapping)
        else None,
        "limit": limit.value,
        "consumed": consumed,
        "reserved": reserved,
        "remaining": remaining,
        "required": required,
        "sticky": sticky,
        "message": message
        or (
            f"execution budget exhausted: {scope_label} {ref_label} "
            f"{limit.field}={limit.value} has {remaining} remaining but "
            f"requires {required} (rule {scope_name})"
        ),
        "decidedAt": timestamp,
    }


def _consumption_record(
    value: Any,
    started_at: str,
    provider: str,
    model: str,
) -> dict[str, Any]:
    record = deepcopy(dict(value)) if isinstance(value, Mapping) else {}
    defaults = {
        "modelInvocations": 0,
        "inputTokens": 0,
        "outputTokens": 0,
        "reservedModelInvocations": 0,
        "reservedInputTokens": 0,
        "reservedOutputTokens": 0,
        "elapsedMs": 0,
        "costUsd": 0.0,
        "reservedCostUsd": 0.0,
        "costStatus": "UNAVAILABLE",
        "costStatusCounts": {},
        "startedAt": started_at,
        "updatedAt": started_at,
        "lastProvider": provider,
        "lastModel": model,
        "overrun": False,
        "blockedReason": None,
    }
    for key, default in defaults.items():
        record.setdefault(key, default)
    return record


def _consumption_fields(field: str) -> tuple[str, str]:
    return {
        "maxModelInvocations": ("modelInvocations", "reservedModelInvocations"),
        "maxInputTokens": ("inputTokens", "reservedInputTokens"),
        "maxOutputTokens": ("outputTokens", "reservedOutputTokens"),
        "maxElapsedMs": ("elapsedMs", "reservedElapsedMs"),
        "maxCostUsd": ("costUsd", "reservedCostUsd"),
    }[field]


def _scope_record(limits: Mapping[str, BudgetLimit | None]) -> dict[str, Any]:
    return {
        field: limit.as_record() if limit is not None else None
        for field, limit in limits.items()
    }


def _effective_output_limit(
    configured: int | None, remaining: int | None
) -> int | None:
    if remaining is None:
        return configured
    if remaining < 1:
        return None
    return remaining if configured is None else min(configured, remaining)


def _estimate_cost(
    pricing: Mapping[str, Any] | None,
    input_tokens: int,
    output_tokens: int,
) -> float:
    if pricing is None:
        return 0.0
    return round(
        (
            input_tokens * float(pricing["inputPerMillionTokens"])
            + output_tokens * float(pricing["outputPerMillionTokens"])
        )
        / 1_000_000,
        12,
    )


def _cost_evidence(
    cost: float | None,
    cost_status: str | None,
    pricing: Mapping[str, Any] | None,
    input_tokens: int,
    output_tokens: int,
) -> tuple[str, float | None]:
    if cost_status is not None and cost_status not in COST_STATUSES:
        raise ExecutionBudgetError("cost_status is not a supported cost classification")
    if cost is not None:
        if cost < 0 or not isfinite(cost):
            raise ExecutionBudgetError("cost must be a non-negative finite number")
        return (cost_status or "EXACT"), float(cost)
    if cost_status in {"ESTIMATED", "CONFIGURED"}:
        if pricing is not None and (input_tokens or output_tokens):
            return cost_status, _estimate_cost(pricing, input_tokens, output_tokens)
        return "UNAVAILABLE", None
    if pricing is not None and (input_tokens or output_tokens):
        return "ESTIMATED", _estimate_cost(pricing, input_tokens, output_tokens)
    return "UNAVAILABLE", None


def _elapsed_ms(started_at: Any, timestamp: str) -> int:
    if not isinstance(started_at, str):
        return 0
    try:
        start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        return max(0, int((end - start).total_seconds() * 1000))
    except ValueError:
        return 0


def _canonical_ref(ref: Mapping[str, Any]) -> str:
    return "|".join(
        str(ref.get(field, "")) for field in ("kind", "name", "version")
    )


def _without_resolution_time(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): deepcopy(item)
        for key, item in value.items()
        if key != "resolvedAt"
    }


def _resource_reference(resource: Any) -> dict[str, str] | None:
    ref = getattr(resource, "ref", None)
    if ref is None:
        return None
    return {
        "kind": str(getattr(ref, "kind", "")),
        "name": str(getattr(ref, "name", "")),
        "version": str(getattr(ref, "version", "")),
    }


def _reference_record(value: Mapping[str, Any] | None) -> dict[str, str] | None:
    if not isinstance(value, Mapping):
        return None
    values = {field: value.get(field) for field in ("kind", "name", "version")}
    if not all(isinstance(item, str) and item for item in values.values()):
        return None
    return {
        "kind": str(values["kind"]),
        "name": str(values["name"]),
        "version": str(values["version"]),
    }
