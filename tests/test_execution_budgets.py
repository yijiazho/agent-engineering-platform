import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

import pytest

from aep.execution_budgets import (
    BUDGET_COST_UNAVAILABLE,
    BUDGET_EXHAUSTED,
    ExecutionBudgetError,
    ExecutionBudgetExceeded,
    TaskBudgetManager,
    budget_evidence,
    resolve_execution_budget,
    validate_budget_narrowing,
)
from aep.inspection_cli import main
from aep.resource_loader import Resource, ResourceCollection, ResourceRef
from aep.runtime_store import (
    DurableJsonRuntimeObjectStore,
    InMemoryRuntimeObjectStore,
)


TIMESTAMP = "2026-10-31T00:00:00Z"
LATER = "2026-10-31T00:00:01Z"
WORKFLOW_ID = "workflowexecution-budget00001"
TASK_ID = "taskexecution-budget00001"
WORKFLOW_REF = {"kind": "Workflow", "name": "issue-to-pr", "version": "1.0.0"}
TASK_REF = {"kind": "Task", "name": "analyze-issue", "version": "1.0.0"}
AGENT_REF = {"kind": "Agent", "name": "issue-analyzer", "version": "1.0.0"}
MODEL_REF = {"kind": "Model", "name": "default-reasoning", "version": "1.0.0"}


def test_nested_scope_precedence_is_most_restrictive_and_attributed() -> None:
    resolved = resolve_execution_budget(
        workflow_budget={"maxModelInvocations": 10, "maxInputTokens": 1000},
        task_budget={"maxModelInvocations": 5},
        agent_budget={"maxModelInvocations": 2, "maxInputTokens": 500},
        execution_budget={"maxModelInvocations": 8},
        workflow_ref=WORKFLOW_REF,
        task_ref=TASK_REF,
        agent_ref=AGENT_REF,
        model_token_limit=4096,
    )

    assert resolved.workflow["maxModelInvocations"].value == 8
    assert resolved.workflow["maxModelInvocations"].scope == "Execution"
    assert resolved.task["maxModelInvocations"].value == 5
    assert resolved.task["maxModelInvocations"].scope == "Task"
    assert resolved.agent["maxModelInvocations"].value == 2
    assert resolved.agent["maxModelInvocations"].scope == "Agent"
    assert resolved.agent["maxInputTokens"].value == 500
    assert resolved.model_output_token_limit == 4096


def test_lower_scope_cannot_widen_a_parent_limit() -> None:
    with pytest.raises(ExecutionBudgetError, match="widens Workflow"):
        resolve_execution_budget(
            workflow_budget={"maxModelInvocations": 5},
            task_budget={"maxModelInvocations": 6},
        )
    with pytest.raises(ExecutionBudgetError, match="widens Task"):
        validate_budget_narrowing(
            workflow_budget={"maxModelInvocations": 5},
            task_budget={"maxModelInvocations": 4},
            agent_budget={"maxModelInvocations": 5},
        )


def test_task_retry_attempts_share_task_and_workflow_consumption() -> None:
    store, workflow, task, resources = budget_runtime(
        workflow_budget={"maxModelInvocations": 5},
        task_budget={"maxModelInvocations": 2},
    )
    first = prepare(store, resources, workflow, task)
    assert first is not None
    first.preflight(
        estimated_input_tokens=10,
        configured_output_token_limit=100,
        timestamp=TIMESTAMP,
    )
    first.record(input_tokens=10, output_tokens=4, latency_ms=1, timestamp=TIMESTAMP)

    retry = store.create(
        task_execution(attempt=2, execution_id="taskexecution-budget00002"),
        deterministic_key="task-retry",
    )
    second = prepare(store, resources, workflow, retry)
    assert second is not None
    second.preflight(
        estimated_input_tokens=10,
        configured_output_token_limit=100,
        timestamp=LATER,
    )
    second.record(input_tokens=10, output_tokens=4, latency_ms=1, timestamp=LATER)

    third = store.create(
        task_execution(attempt=3, execution_id="taskexecution-budget00003"),
        deterministic_key="task-retry-3",
    )
    budget = prepare(store, resources, workflow, third)
    assert budget is not None
    with pytest.raises(ExecutionBudgetExceeded) as raised:
        budget.preflight(
            estimated_input_tokens=10,
            configured_output_token_limit=100,
            timestamp=LATER,
        )

    decision = raised.value.decision
    assert decision["reason"] == BUDGET_EXHAUSTED
    assert decision["dimension"] == "maxModelInvocations"
    assert decision["scope"] == "Task"
    assert decision["resourceRef"] == TASK_REF
    consumed = store.get(WORKFLOW_ID)["budgetConsumption"]
    assert consumed["workflow"]["modelInvocations"] == 2
    assert consumed["tasks"]["Task|analyze-issue|1.0.0"]["modelInvocations"] == 2


def test_execution_level_override_narrows_but_cannot_widen_workflow_budget() -> None:
    store, workflow, task, resources = budget_runtime(
        workflow_budget={"maxModelInvocations": 5}
    )
    store.update_status(
        WORKFLOW_ID,
        "RUNNING",
        expected_status="RUNNING",
        changes={"budgetOverrides": {"maxModelInvocations": 3}},
    )

    narrowed = prepare(store, resources, store.get(WORKFLOW_ID), task)
    assert narrowed is not None
    assert narrowed.resolved.workflow["maxModelInvocations"].value == 3
    assert narrowed.resolved.workflow["maxModelInvocations"].scope == "Execution"

    store.update_status(
        WORKFLOW_ID,
        "RUNNING",
        expected_status="RUNNING",
        changes={"budgetOverrides": {"maxModelInvocations": 6}},
    )
    with pytest.raises(ExecutionBudgetError, match="widens Workflow"):
        prepare(store, resources, store.get(WORKFLOW_ID), task)


def test_execution_override_validates_against_declared_not_narrowed_parent() -> None:
    store, workflow, task, resources = budget_runtime(
        workflow_budget={"maxModelInvocations": 10},
        task_budget={"maxModelInvocations": 5},
    )
    store.update_status(
        WORKFLOW_ID,
        "RUNNING",
        expected_status="RUNNING",
        changes={"budgetOverrides": {"maxModelInvocations": 3}},
    )

    budget = prepare(store, resources, store.get(WORKFLOW_ID), task)
    assert budget is not None
    assert budget.resolved.workflow["maxModelInvocations"].value == 3
    assert budget.resolved.task["maxModelInvocations"].value == 3
    assert budget.resolved.task["maxModelInvocations"].scope == "Execution"


def test_workflow_budget_evidence_remains_stable_for_later_tasks() -> None:
    store = InMemoryRuntimeObjectStore()
    workflow = store.create(workflow_execution(), deterministic_key="workflow")
    first = store.create(
        task_execution(execution_id="taskexecution-budget00011"),
        deterministic_key="task-first",
    )
    second_ref = {"kind": "Task", "name": "plan", "version": "1.0.0"}
    second = store.create(
        {
            **task_execution(execution_id="taskexecution-budget00012"),
            "taskRef": second_ref,
        },
        deterministic_key="task-second",
    )
    workspace = _resource("Workspace", "local", {"repository": "octo/repo"})
    workflow_resource = _resource("Workflow", "issue-to-pr", {
        "triggers": [{"eventRef": {"kind": "Event", "name": "github-issue-created", "version": "1.0.0"}}],
        "tasks": [{"taskRef": TASK_REF}, {"taskRef": second_ref}],
        "budget": {"maxModelInvocations": 10, "maxOutputTokens": 1000},
    })
    first_task = _resource("Task", "analyze-issue", {
        "objective": "Analyze.",
        "agentRef": AGENT_REF,
        "outputs": {"type": "object"},
        "inputContextTokenBudget": 32_000,
        "budget": {"maxModelInvocations": 1},
    })
    second_task = _resource("Task", "plan", {
        "objective": "Plan.",
        "agentRef": AGENT_REF,
        "outputs": {"type": "object"},
        "inputContextTokenBudget": 32_000,
        "budget": {"maxModelInvocations": 1, "maxOutputTokens": 500},
    })
    agent = _resource("Agent", "issue-analyzer", {
        "promptRef": {"kind": "Prompt", "name": "issue-analysis", "version": "1.0.0"},
        "modelRef": MODEL_REF,
    })
    model = _resource("Model", "default-reasoning", {
        "provider": "local",
        "model": "fake-reasoning",
        "tokenLimit": 100,
        "timeoutMs": 1000,
    })
    resources = ResourceCollection(
        workspace, (workspace, workflow_resource, first_task, second_task, agent, model)
    )
    clock = [TIMESTAMP]
    manager = TaskBudgetManager(store, resources, lambda: clock[0])

    first_budget = manager.prepare(
        workflow_execution=workflow,
        task_execution=first,
        task=first_task,
        agent=agent,
        model=model,
    )
    clock[0] = LATER
    second_budget = manager.prepare(
        workflow_execution=store.get(WORKFLOW_ID),
        task_execution=second,
        task=second_task,
        agent=agent,
        model=model,
    )

    assert first_budget is not None and second_budget is not None
    workflow_budget = store.get(WORKFLOW_ID)["executionBudget"]
    assert workflow_budget["limits"]["maxModelInvocations"]["value"] == 10
    assert workflow_budget["resourceRefs"] == [WORKFLOW_REF]
    assert second_budget.resolved.task["maxOutputTokens"].value == 500
    assert second_budget.resolved.task["maxOutputTokens"].scope == "Task"
    # Task ceilings stay on the Task ledger and must not consume the shared
    # Agent ledger used by a different Task.
    assert second_budget.resolved.agent["maxModelInvocations"].value == 10
    assert second_budget.resolved.agent["maxModelInvocations"].scope == "Workflow"
    first_charge = first_budget.preflight(
        estimated_input_tokens=10,
        configured_output_token_limit=100,
        timestamp=TIMESTAMP,
    )
    assert first_charge.max_provider_attempts == 1
    first_budget.record(
        input_tokens=10,
        output_tokens=4,
        latency_ms=1,
        timestamp=TIMESTAMP,
    )
    second_budget.preflight(
        estimated_input_tokens=10,
        configured_output_token_limit=100,
        timestamp=LATER,
    )


def test_concurrent_preflights_reserve_the_full_provider_attempt_allowance() -> None:
    store, workflow, first, second, resources = two_task_runtime(
        workflow_budget={"maxModelInvocations": 3}
    )
    first_budget, second_budget = prepare_two(
        store, resources, workflow, first, second
    )
    assert first_budget is not None and second_budget is not None

    first_reservation = first_budget.preflight(
        estimated_input_tokens=10,
        configured_output_token_limit=100,
        configured_provider_attempts=3,
        timestamp=TIMESTAMP,
    )
    assert first_reservation.max_provider_attempts == 3
    workflow_consumption = store.get(WORKFLOW_ID)["budgetConsumption"]["workflow"]
    assert workflow_consumption["reservedModelInvocations"] == 3

    with pytest.raises(ExecutionBudgetExceeded) as raised:
        second_budget.preflight(
            estimated_input_tokens=10,
            configured_output_token_limit=100,
            configured_provider_attempts=3,
            timestamp=LATER,
        )
    assert raised.value.decision["dimension"] == "maxModelInvocations"

    first_budget.record(
        input_tokens=10,
        output_tokens=4,
        latency_ms=1,
        invocation_count=1,
        timestamp=LATER,
    )
    second_reservation = second_budget.preflight(
        estimated_input_tokens=10,
        configured_output_token_limit=100,
        configured_provider_attempts=3,
        timestamp=LATER,
    )
    assert second_reservation.max_provider_attempts == 2


def test_one_attempt_models_reserve_only_one_invocation_unit() -> None:
    store, workflow, first, second, resources = two_task_runtime(
        workflow_budget={"maxModelInvocations": 3}
    )
    first_budget, second_budget = prepare_two(
        store, resources, workflow, first, second
    )
    assert first_budget is not None and second_budget is not None

    first_reservation = first_budget.preflight(
        estimated_input_tokens=10,
        configured_output_token_limit=100,
        configured_provider_attempts=1,
        timestamp=TIMESTAMP,
    )
    assert first_reservation.max_provider_attempts == 1
    workflow_consumption = store.get(WORKFLOW_ID)["budgetConsumption"]["workflow"]
    assert workflow_consumption["reservedModelInvocations"] == 1

    second_reservation = second_budget.preflight(
        estimated_input_tokens=10,
        configured_output_token_limit=100,
        configured_provider_attempts=1,
        timestamp=LATER,
    )
    assert second_reservation.max_provider_attempts == 1


def test_temporary_output_reservation_does_not_poison_workflow_budget() -> None:
    store, workflow, first, second, resources = two_task_runtime(
        workflow_budget={"maxOutputTokens": 10}
    )
    first_budget, second_budget = prepare_two(
        store, resources, workflow, first, second
    )
    assert first_budget is not None and second_budget is not None

    first_reservation = first_budget.preflight(
        estimated_input_tokens=1,
        configured_output_token_limit=10,
        timestamp=TIMESTAMP,
    )
    assert first_reservation.effective_output_token_limit == 10

    with pytest.raises(ExecutionBudgetExceeded) as raised:
        second_budget.preflight(
            estimated_input_tokens=1,
            configured_output_token_limit=10,
            timestamp=LATER,
        )
    assert raised.value.decision["dimension"] == "maxOutputTokens"

    first_budget.record(
        input_tokens=1,
        output_tokens=1,
        latency_ms=1,
        timestamp=LATER,
    )
    second_reservation = second_budget.preflight(
        estimated_input_tokens=1,
        configured_output_token_limit=10,
        timestamp=LATER,
    )
    assert second_reservation.effective_output_token_limit == 9


def test_durable_budget_admission_is_atomic_across_store_instances(tmp_path) -> None:
    path = tmp_path / "objects.json"
    first_store = DurableJsonRuntimeObjectStore(path)
    first_store.create(workflow_execution(), deterministic_key="workflow")
    first_store.create(
        task_execution(execution_id="taskexecution-budget00031"),
        deterministic_key="task-first",
    )
    second_ref = {"kind": "Task", "name": "plan", "version": "1.0.0"}
    first_store.create(
        {
            **task_execution(execution_id="taskexecution-budget00032"),
            "taskRef": second_ref,
        },
        deterministic_key="task-second",
    )
    resources = two_task_runtime(
        workflow_budget={"maxModelInvocations": 1}
    )[4]
    second_store = DurableJsonRuntimeObjectStore(path)

    def prepare(store, task_id, task_ref):
        return TaskBudgetManager(store, resources, lambda: TIMESTAMP).prepare(
            workflow_execution=workflow_execution(),
            task_execution={
                **task_execution(execution_id=task_id),
                "taskRef": {
                    "kind": task_ref.kind,
                    "name": task_ref.name,
                    "version": task_ref.version,
                },
            },
            task=resources.get(task_ref),
            agent=resources.get(ResourceRef.from_mapping(AGENT_REF)),
            model=resources.get(ResourceRef.from_mapping(MODEL_REF)),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(
            prepare,
            first_store,
            "taskexecution-budget00031",
            ResourceRef.from_mapping(TASK_REF),
        )
        second_future = pool.submit(
            prepare,
            second_store,
            "taskexecution-budget00032",
            ResourceRef("Task", "plan", "1.0.0"),
        )
        first_budget = first_future.result()
        second_budget = second_future.result()
    assert first_budget is not None and second_budget is not None

    def admit(budget) -> str:
        try:
            budget.preflight(
                estimated_input_tokens=10,
                configured_output_token_limit=100,
                timestamp=TIMESTAMP,
            )
            return "admitted"
        except ExecutionBudgetExceeded:
            return "blocked"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(admit, (first_budget, second_budget)))

    assert outcomes == ["admitted", "blocked"]
    reserved = first_store.get(WORKFLOW_ID)["budgetConsumption"]["workflow"][
        "reservedModelInvocations"
    ]
    assert reserved == 1


def test_predispatch_pricing_rejection_is_not_sticky() -> None:
    store, workflow, task, resources = budget_runtime(
        workflow_budget={"maxCostUsd": 1},
        task_budget={"maxCostUsd": 1},
    )
    unpriced = prepare(store, resources, workflow, task)
    assert unpriced is not None
    with pytest.raises(ExecutionBudgetExceeded) as raised:
        unpriced.preflight(
            estimated_input_tokens=10,
            configured_output_token_limit=10,
            timestamp=TIMESTAMP,
        )
    assert raised.value.decision["reason"] == BUDGET_COST_UNAVAILABLE
    workflow_record = store.get(WORKFLOW_ID)["budgetConsumption"]["workflow"]
    assert "blockedDecision" not in workflow_record

    retry = store.create(
        task_execution(attempt=2, execution_id="taskexecution-budget00033"),
        deterministic_key="task-retry-priced",
    )
    priced_resources = budget_resources(
        workflow_budget={"maxCostUsd": 1},
        task_budget={"maxCostUsd": 1},
        model_pricing={
            "inputPerMillionTokens": 1000,
            "outputPerMillionTokens": 1000,
            "currency": "USD",
            "source": "fixture-prices",
        },
    )
    priced = TaskBudgetManager(store, priced_resources, lambda: TIMESTAMP).prepare(
        workflow_execution=workflow,
        task_execution=retry,
        task=priced_resources.get(ResourceRef.from_mapping(TASK_REF)),
        agent=priced_resources.get(ResourceRef.from_mapping(AGENT_REF)),
        model=priced_resources.get(ResourceRef.from_mapping(MODEL_REF)),
    )
    assert priced is not None
    reservation = priced.preflight(
        estimated_input_tokens=10,
        configured_output_token_limit=10,
        timestamp=LATER,
    )
    assert reservation.effective_output_token_limit == 10


def test_input_and_output_budgets_are_independent_and_exhaustable() -> None:
    store, workflow, task, resources = budget_runtime(
        task_budget={"maxInputTokens": 100, "maxOutputTokens": 10}
    )
    budget = prepare(store, resources, workflow, task)
    assert budget is not None
    reservation = budget.preflight(
        estimated_input_tokens=50,
        configured_output_token_limit=100,
        timestamp=TIMESTAMP,
    )

    assert reservation.effective_output_token_limit == 10
    charge = budget.record(
        input_tokens=50,
        output_tokens=10,
        latency_ms=1,
        timestamp=TIMESTAMP,
    )
    assert charge["inputTokens"] == 50
    assert charge["outputTokens"] == 10
    record = store.get(WORKFLOW_ID)["budgetConsumption"]["tasks"][
        "Task|analyze-issue|1.0.0"
    ]
    assert record["inputTokens"] == 50
    assert record["outputTokens"] == 10

    with pytest.raises(ExecutionBudgetExceeded) as raised:
        budget.preflight(
            estimated_input_tokens=1,
            configured_output_token_limit=100,
            timestamp=TIMESTAMP,
        )
    assert raised.value.decision["dimension"] == "maxOutputTokens"


def test_monetary_budget_without_pricing_fails_closed_before_dispatch() -> None:
    store, workflow, task, resources = budget_runtime(
        task_budget={"maxCostUsd": 1}
    )
    budget = prepare(store, resources, workflow, task)
    assert budget is not None

    with pytest.raises(ExecutionBudgetExceeded) as raised:
        budget.preflight(
            estimated_input_tokens=10,
            configured_output_token_limit=10,
            timestamp=TIMESTAMP,
        )

    assert raised.value.decision["reason"] == BUDGET_COST_UNAVAILABLE
    assert raised.value.decision["dimension"] == "maxCostUsd"


def test_provider_cost_is_exact_and_configured_pricing_is_estimated() -> None:
    store, workflow, task, resources = budget_runtime(
        task_budget={"maxModelInvocations": 5}
    )
    budget = prepare(store, resources, workflow, task)
    assert budget is not None
    budget.preflight(
        estimated_input_tokens=10,
        configured_output_token_limit=10,
        timestamp=TIMESTAMP,
    )
    exact = budget.record(
        input_tokens=10,
        output_tokens=5,
        latency_ms=1,
        cost=0.25,
        timestamp=TIMESTAMP,
    )
    assert exact["costStatus"] == "EXACT"
    assert exact["costUsd"] == 0.25

    priced_store, priced_workflow, priced_task, priced_resources = budget_runtime(
        task_budget={"maxModelInvocations": 5},
        model_pricing={
            "inputPerMillionTokens": 1_000_000,
            "outputPerMillionTokens": 2_000_000,
            "currency": "USD",
            "source": "fixture-prices",
        }
    )
    priced = prepare(priced_store, priced_resources, priced_workflow, priced_task)
    assert priced is not None
    priced.preflight(
        estimated_input_tokens=10,
        configured_output_token_limit=10,
        timestamp=TIMESTAMP,
    )
    estimated = priced.record(
        input_tokens=10,
        output_tokens=5,
        latency_ms=1,
        timestamp=TIMESTAMP,
    )
    assert estimated["costStatus"] == "ESTIMATED"
    assert estimated["costUsd"] == pytest.approx(10 * 1.0 + 5 * 2.0)


def test_elapsed_budget_blocks_when_the_deadline_is_reached() -> None:
    store, workflow, task, resources = budget_runtime(
        task_budget={"maxElapsedMs": 1000}
    )
    budget = prepare(store, resources, workflow, task)
    assert budget is not None

    with pytest.raises(ExecutionBudgetExceeded) as raised:
        budget.preflight(
            estimated_input_tokens=10,
            configured_output_token_limit=10,
            timestamp=LATER,
        )

    assert raised.value.decision["dimension"] == "maxElapsedMs"
    assert raised.value.decision["scope"] == "Task"


def test_zero_dispatched_attempts_release_without_charging_budget() -> None:
    store, workflow, task, resources = budget_runtime(
        task_budget={"maxModelInvocations": 1}
    )
    budget = prepare(store, resources, workflow, task)
    assert budget is not None
    budget.preflight(
        estimated_input_tokens=10,
        configured_output_token_limit=10,
        timestamp=TIMESTAMP,
    )

    charge = budget.record(invocation_count=0, timestamp=TIMESTAMP)

    assert charge["invocationCount"] == 0
    consumption = store.get(WORKFLOW_ID)["budgetConsumption"]["tasks"][
        "Task|analyze-issue|1.0.0"
    ]
    assert consumption["modelInvocations"] == 0
    assert consumption["reservedModelInvocations"] == 0
    # A deferred request must not permanently exhaust the one-invocation budget.
    reservation = budget.preflight(
        estimated_input_tokens=10,
        configured_output_token_limit=10,
        timestamp=LATER,
    )
    assert reservation.max_provider_attempts == 1


def test_failed_dispatched_request_retains_token_reservation() -> None:
    store, workflow, task, resources = budget_runtime(
        task_budget={"maxOutputTokens": 5}
    )
    budget = prepare(store, resources, workflow, task)
    assert budget is not None
    reservation = budget.preflight(
        estimated_input_tokens=1,
        configured_output_token_limit=5,
        timestamp=TIMESTAMP,
    )
    assert reservation.effective_output_token_limit == 5

    budget.record(
        invocation_count=1,
        unreconciled_dispatched_attempts=1,
        timestamp=TIMESTAMP,
    )

    consumption = store.get(WORKFLOW_ID)["budgetConsumption"]["tasks"][
        "Task|analyze-issue|1.0.0"
    ]
    assert consumption["outputTokens"] == 0
    assert consumption["reservedOutputTokens"] == 5
    with pytest.raises(ExecutionBudgetExceeded) as raised:
        budget.preflight(
            estimated_input_tokens=1,
            configured_output_token_limit=5,
            timestamp=LATER,
        )
    assert raised.value.decision["dimension"] == "maxOutputTokens"


def test_adapter_retry_allowance_is_clamped_to_remaining_invocations() -> None:
    store, workflow, task, resources = budget_runtime(
        task_budget={"maxModelInvocations": 1}
    )
    budget = prepare(store, resources, workflow, task)
    assert budget is not None
    reservation = budget.preflight(
        estimated_input_tokens=10,
        configured_output_token_limit=10,
        timestamp=TIMESTAMP,
    )
    assert reservation.max_provider_attempts == 1

    from aep.model_invocation import ModelConfiguration

    configured = ModelConfiguration(
        model_ref=MODEL_REF,
        provider="local",
        model="fake-reasoning",
        token_limit=100,
        timeout_ms=1000,
        retry_policy={"maxAttempts": 4, "backoffMs": 10},
    )
    bounded = configured.with_budget_bounds(
        token_limit=10,
        max_provider_attempts=reservation.max_provider_attempts,
    )
    assert bounded.max_attempts == 1
    assert bounded.token_limit == 10


def test_dispatched_usage_unavailable_blocks_further_cost_budget_work() -> None:
    store, workflow, task, resources = budget_runtime(
        task_budget={"maxCostUsd": 100},
        model_pricing={
            "inputPerMillionTokens": 1_000_000,
            "outputPerMillionTokens": 1_000_000,
            "currency": "USD",
            "source": "fixture-prices",
        },
    )
    budget = prepare(store, resources, workflow, task)
    assert budget is not None
    budget.preflight(
        estimated_input_tokens=10,
        configured_output_token_limit=10,
        timestamp=TIMESTAMP,
    )
    budget.record(
        input_tokens=0,
        output_tokens=0,
        latency_ms=1,
        cost=None,
        cost_status="UNAVAILABLE",
        invocation_count=1,
        timestamp=TIMESTAMP,
    )

    consumption = store.get(WORKFLOW_ID)["budgetConsumption"]["tasks"][
        "Task|analyze-issue|1.0.0"
    ]
    assert consumption["blockedReason"] == BUDGET_COST_UNAVAILABLE
    assert consumption["blockedDecision"]["reason"] == BUDGET_COST_UNAVAILABLE
    with pytest.raises(ExecutionBudgetExceeded) as raised:
        budget.preflight(
            estimated_input_tokens=10,
            configured_output_token_limit=10,
            timestamp=LATER,
        )
    assert raised.value.decision["reason"] == BUDGET_COST_UNAVAILABLE


def test_provider_usage_normalizes_into_budget_charge_evidence() -> None:
    store, workflow, task, resources = budget_runtime(
        task_budget={"maxInputTokens": 100, "maxOutputTokens": 100}
    )
    budget = prepare(store, resources, workflow, task)
    assert budget is not None
    budget.preflight(
        estimated_input_tokens=40,
        configured_output_token_limit=100,
        timestamp=TIMESTAMP,
    )

    charge = budget.record(
        input_tokens=37,
        output_tokens=11,
        latency_ms=19,
        cost=0.02,
        cost_status="EXACT",
        invocation_count=2,
        timestamp=LATER,
    )

    assert charge["invocationCount"] == 2
    assert charge["inputTokens"] == 37
    assert charge["outputTokens"] == 11
    assert charge["provider"] == "local"
    assert charge["model"] == "fake-reasoning"
    assert charge["costStatus"] == "EXACT"
    assert charge["remaining"]["task"]["remaining"]["maxOutputTokens"] == 89


def test_budget_evidence_reports_remaining_and_responsible_rule() -> None:
    evidence = budget_evidence(
        {
            "maxModelInvocations": {
                "value": 2,
                "scope": "Task",
                "resourceRef": TASK_REF,
            }
        },
        {"modelInvocations": 1, "reservedModelInvocations": 0},
    )

    assert evidence["configuredLimits"]["maxModelInvocations"]["scope"] == "Task"
    assert evidence["consumed"]["maxModelInvocations"] == 1
    assert evidence["remaining"]["maxModelInvocations"] == 1


def test_inspection_shows_budget_and_explains_the_blocking_rule(tmp_path, capsys) -> None:
    workflow = inspection_workflow()
    task = inspection_task()
    state = tmp_path / "objects.json"
    state.write_text(
        json.dumps({
            "objects": {workflow["id"]: workflow, task["id"]: task},
            "deterministicKeys": {},
            "claims": {},
        }),
        encoding="utf-8",
    )

    assert main(["--state-file", str(state), "--output", "json", "executions", "show", workflow["id"]]) == 0
    execution = json.loads(capsys.readouterr().out)
    budget = execution["budget"]
    assert budget["configuredLimits"]["maxModelInvocations"]["value"] == 1
    assert budget["consumed"]["maxModelInvocations"] == 1
    assert budget["remaining"]["maxModelInvocations"] == 0
    task_budget = execution["tasks"][0]["budget"]
    assert task_budget["task"]["remaining"]["maxModelInvocations"] == 0

    assert main(["--state-file", str(state), "--output", "json", "explain", workflow["id"]]) == 0
    explanation = json.loads(capsys.readouterr().out)
    assert explanation["outcome"] == "BUDGET_BLOCKED"
    assert "execution budget exhausted" in explanation["reason"]
    assert explanation["decisiveEvidence"]["budgetDecision"]["scope"] == "Task"


def budget_runtime(
    *,
    workflow_budget: dict | None = None,
    task_budget: dict | None = None,
    agent_budget: dict | None = None,
    model_pricing: dict | None = None,
):
    store = InMemoryRuntimeObjectStore()
    workflow = store.create(workflow_execution(), deterministic_key="workflow")
    task = store.create(task_execution(), deterministic_key="task")
    resources = budget_resources(
        workflow_budget=workflow_budget,
        task_budget=task_budget,
        agent_budget=agent_budget,
        model_pricing=model_pricing,
    )
    return store, workflow, task, resources


def two_task_runtime(
    *, workflow_budget: dict | None = None
):
    store = InMemoryRuntimeObjectStore()
    workflow = store.create(workflow_execution(), deterministic_key="workflow")
    first = store.create(
        task_execution(execution_id="taskexecution-budget00021"),
        deterministic_key="task-first",
    )
    second_ref = {"kind": "Task", "name": "plan", "version": "1.0.0"}
    second = store.create(
        {
            **task_execution(execution_id="taskexecution-budget00022"),
            "taskRef": second_ref,
        },
        deterministic_key="task-second",
    )
    workspace = _resource("Workspace", "local", {"repository": "octo/repo"})
    workflow_resource = _resource("Workflow", "issue-to-pr", {
        "triggers": [{"eventRef": {"kind": "Event", "name": "github-issue-created", "version": "1.0.0"}}],
        "tasks": [{"taskRef": TASK_REF}, {"taskRef": second_ref}],
        "budget": workflow_budget or {},
    })
    first_task = _resource("Task", "analyze-issue", {
        "objective": "Analyze.",
        "agentRef": AGENT_REF,
        "outputs": {"type": "object"},
        "inputContextTokenBudget": 32_000,
    })
    second_task = _resource("Task", "plan", {
        "objective": "Plan.",
        "agentRef": AGENT_REF,
        "outputs": {"type": "object"},
        "inputContextTokenBudget": 32_000,
    })
    agent = _resource("Agent", "issue-analyzer", {
        "promptRef": {"kind": "Prompt", "name": "issue-analysis", "version": "1.0.0"},
        "modelRef": MODEL_REF,
    })
    model = _resource("Model", "default-reasoning", {
        "provider": "local",
        "model": "fake-reasoning",
        "tokenLimit": 100,
        "timeoutMs": 1000,
    })
    resources = ResourceCollection(
        workspace, (workspace, workflow_resource, first_task, second_task, agent, model)
    )
    return store, workflow, first, second, resources


def prepare_two(store, resources, workflow, first, second):
    manager = TaskBudgetManager(store, resources, lambda: TIMESTAMP)
    return (
        manager.prepare(
            workflow_execution=workflow,
            task_execution=first,
            task=resources.get(ResourceRef.from_mapping(TASK_REF)),
            agent=resources.get(ResourceRef.from_mapping(AGENT_REF)),
            model=resources.get(ResourceRef.from_mapping(MODEL_REF)),
        ),
        manager.prepare(
            workflow_execution=store.get(WORKFLOW_ID),
            task_execution=second,
            task=resources.get(ResourceRef("Task", "plan", "1.0.0")),
            agent=resources.get(ResourceRef.from_mapping(AGENT_REF)),
            model=resources.get(ResourceRef.from_mapping(MODEL_REF)),
        ),
    )


def prepare(store, resources, workflow, task):
    return TaskBudgetManager(store, resources, lambda: TIMESTAMP).prepare(
        workflow_execution=workflow,
        task_execution=task,
        task=resources.get(ResourceRef.from_mapping(TASK_REF)),
        agent=resources.get(ResourceRef.from_mapping(AGENT_REF)),
        model=resources.get(ResourceRef.from_mapping(MODEL_REF)),
    )


def budget_resources(
    *,
    workflow_budget: dict | None = None,
    task_budget: dict | None = None,
    agent_budget: dict | None = None,
    model_pricing: dict | None = None,
) -> ResourceCollection:
    workspace = _resource("Workspace", "local", {"repository": "octo/repo"})
    workflow_spec = {
        "triggers": [{"eventRef": {"kind": "Event", "name": "github-issue-created", "version": "1.0.0"}}],
        "tasks": [{"taskRef": TASK_REF}],
    }
    if workflow_budget is not None:
        workflow_spec["budget"] = workflow_budget
    workflow = _resource("Workflow", "issue-to-pr", workflow_spec)
    task_spec = {
        "objective": "Analyze the issue.",
        "agentRef": AGENT_REF,
        "outputs": {"type": "object"},
        "inputContextTokenBudget": 32_000,
    }
    if task_budget is not None:
        task_spec["budget"] = task_budget
    task = _resource("Task", "analyze-issue", task_spec)
    agent_spec = {
        "promptRef": {"kind": "Prompt", "name": "issue-analysis", "version": "1.0.0"},
        "modelRef": MODEL_REF,
    }
    if agent_budget is not None:
        agent_spec["budget"] = agent_budget
    agent = _resource("Agent", "issue-analyzer", agent_spec)
    model_spec = {
        "provider": "local",
        "model": "fake-reasoning",
        "tokenLimit": 100,
        "timeoutMs": 1000,
    }
    if model_pricing is not None:
        model_spec["pricing"] = model_pricing
    model = _resource("Model", "default-reasoning", model_spec)
    return ResourceCollection(workspace, (workspace, workflow, task, agent, model))


def _resource(kind: str, name: str, spec: dict) -> Resource:
    ref = ResourceRef(kind, name, "1.0.0")
    return Resource(
        ref=ref,
        path=Path(f".ai/{kind.lower()}s/{name}.yaml"),
        data={
            "apiVersion": "aep.dev/v1alpha1",
            "kind": kind,
            "metadata": {"name": name, "version": "1.0.0"},
            "spec": spec,
        },
        references=(),
    )


def workflow_execution() -> dict:
    return {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "WorkflowExecution",
        "id": WORKFLOW_ID,
        "traceId": "trace-budget-1",
        "createdAt": TIMESTAMP,
        "updatedAt": TIMESTAMP,
        "provenance": {"actor": "test", "resourceRefs": []},
        "workflowRef": WORKFLOW_REF,
        "eventRef": {"kind": "Event", "name": "github-issue-created", "version": "1.0.0"},
        "eventId": "event-budget-1",
        "repositoryRevision": "abcdef0",
        "status": "RUNNING",
        "startedAt": TIMESTAMP,
        "taskExecutionIds": [],
    }


def task_execution(*, attempt: int = 1, execution_id: str = TASK_ID) -> dict:
    return {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "TaskExecution",
        "id": execution_id,
        "traceId": "trace-budget-1",
        "createdAt": TIMESTAMP,
        "updatedAt": TIMESTAMP,
        "provenance": {"actor": "test", "resourceRefs": []},
        "workflowExecutionId": WORKFLOW_ID,
        "taskRef": TASK_REF,
        "attempt": attempt,
        "status": "RUNNING",
        "startedAt": TIMESTAMP,
    }


def _consumption(**values) -> dict:
    record = {
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
        "startedAt": TIMESTAMP,
        "updatedAt": TIMESTAMP,
        "lastProvider": "local",
        "lastModel": "fake-reasoning",
        "overrun": False,
        "blockedReason": None,
    }
    record.update(values)
    return record


def inspection_workflow() -> dict:
    rule = {
        "value": 1,
        "scope": "Task",
        "resourceRef": TASK_REF,
    }
    decision = {
        "allowed": False,
        "reason": BUDGET_EXHAUSTED,
        "dimension": "maxModelInvocations",
        "scope": "Task",
        "internalScope": "task",
        "resourceRef": TASK_REF,
        "limit": 1,
        "consumed": 1,
        "reserved": 0,
        "remaining": 0,
        "required": 1,
        "message": (
            "execution budget exhausted: Task analyze-issue/1.0.0 "
            "maxModelInvocations=1 has 0 remaining but requires 1"
        ),
        "decidedAt": LATER,
    }
    return {
        "id": "wf-budget-1",
        "kind": "WorkflowExecution",
        "traceId": "trace-budget-1",
        "status": "FAILED",
        "createdAt": TIMESTAMP,
        "startedAt": TIMESTAMP,
        "completedAt": LATER,
        "workflowRef": WORKFLOW_REF,
        "repositoryRevision": "a" * 40,
        "taskExecutionIds": ["task-budget-1"],
        "provenance": {"actor": "test", "resourceRefs": []},
        "executionBudget": {
            "limits": {
                "maxModelInvocations": {
                    "value": 1,
                    "scope": "Task",
                    "resourceRef": TASK_REF,
                }
            },
            "resolvedAt": TIMESTAMP,
        },
        "budgetConsumption": {
            "workflow": _consumption(
                modelInvocations=1,
                blockedReason=BUDGET_EXHAUSTED,
                blockedDecision=decision,
            ),
            "tasks": {
                "Task|analyze-issue|1.0.0": _consumption(
                    modelInvocations=1,
                    blockedReason=BUDGET_EXHAUSTED,
                    blockedDecision=decision,
                )
            },
            "agents": {},
            "lastDecision": decision,
        },
    }


def inspection_task() -> dict:
    workflow = inspection_workflow()
    decision = workflow["budgetConsumption"]["lastDecision"]
    consumption = workflow["budgetConsumption"]["tasks"]["Task|analyze-issue|1.0.0"]
    scopes = {
        "workflow": {"maxModelInvocations": {"value": 1, "scope": "Task", "resourceRef": TASK_REF}},
        "task": {"maxModelInvocations": {"value": 1, "scope": "Task", "resourceRef": TASK_REF}},
        "agent": {"maxModelInvocations": {"value": 1, "scope": "Task", "resourceRef": TASK_REF}},
    }
    return {
        "id": "task-budget-1",
        "kind": "TaskExecution",
        "traceId": "trace-budget-1",
        "workflowExecutionId": "wf-budget-1",
        "status": "FAILED",
        "attempt": 1,
        "taskRef": TASK_REF,
        "createdAt": TIMESTAMP,
        "executionBudget": {"scopes": scopes, "modelOutputTokenLimit": 10},
        "budgetConsumption": {
            "workflow": deepcopy(consumption),
            "task": deepcopy(consumption),
            "agent": deepcopy(consumption),
        },
        "budgetDecision": deepcopy(decision),
        "failure": {
            "class": "POLICY",
            "message": decision["message"],
        },
        "provenance": {"actor": "test", "resourceRefs": []},
    }
