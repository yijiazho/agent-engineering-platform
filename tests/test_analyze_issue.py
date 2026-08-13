import json
from copy import deepcopy
from pathlib import Path

from aep.analyze_issue import AnalyzeIssueTaskHandler
from aep.context_builder import ContextBuilder
from aep.generated_artifact_store import InMemoryGeneratedArtifactStore
from aep.model_invocation import (
    FakeModelAdapter,
    ModelErrorClass,
    ModelInvocationError,
    ModelResponse,
    ModelUsage,
)
from aep.repository_knowledge import (
    InMemoryRepositoryKnowledgeProvider,
    RepositoryKnowledgeSnapshot,
)
from aep.resource_loader import Resource, ResourceCollection, ResourceRef
from aep.runtime_store import InMemoryRuntimeObjectStore
from aep.task_execution import FailureClass
from aep.task_dag import TaskDagPlan, TaskPlanNode
from aep.workflow_scheduler import WorkflowScheduler


TIMESTAMP = "2026-08-06T12:00:00Z"
REVISION = "abc1234"
WORKFLOW_ID = "workflowexecution-bbbbbbbbbbbb"
TASK_EXECUTION_ID = "taskexecution-aaaaaaaaaaaa"
EVENT_ID = "event-analyze0001"


ISSUE_ANALYSIS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "requestedChange",
        "acceptanceCriteria",
        "risks",
        "likelyRepositoryAreas",
    ],
    "properties": {
        "requestedChange": {"type": "string", "minLength": 1},
        "acceptanceCriteria": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
        },
        "risks": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
        },
        "likelyRepositoryAreas": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
        },
    },
}


VALID_ANALYSIS = {
    "requestedChange": "Implement deterministic issue analysis.",
    "acceptanceCriteria": ["Persist a typed issue analysis artifact."],
    "risks": ["Provider output may violate the schema."],
    "likelyRepositoryAreas": ["src/aep", "tests"],
}


def test_success_composes_boundaries_and_attaches_complete_task_evidence() -> None:
    store, handler, task, adapter = setup_handler(
        ModelResponse(
            output=VALID_ANALYSIS,
            usage=ModelUsage(input_tokens=31, output_tokens=19),
            latency_ms=12,
            provider_metadata={"requestId": "fake-analyze-1"},
        )
    )

    result = handler.execute(task, store.get(TASK_EXECUTION_ID))

    assert result.succeeded is True
    assert len(adapter.requests) == 1
    request = adapter.requests[0]
    assert request.configuration.token_limit == 4_096
    assert request.input["contextPackage"]["elements"][1]["content"]["issue"]["title"] == (
        "Add AnalyzeIssue handling"
    )

    execution = store.get(TASK_EXECUTION_ID)
    assert execution["status"] == "RUNNING"
    assert execution["contextPackageId"].startswith("contextpackage-")
    assert store.get(execution["contextPackageId"])["tokenBudget"] == 32_000
    assert execution["resolvedAgentId"].startswith("resolvedagent-")
    assert len(execution["agentInvocationIds"]) == 1
    assert len(execution["evaluationResultIds"]) == 1
    assert len(execution["generatedArtifactIds"]) == 1

    evaluation = store.get(execution["evaluationResultIds"][0])
    assert evaluation["outcome"] == "PASS"
    assert evaluation["target"] == {
        "type": "AgentInvocation",
        "id": execution["agentInvocationIds"][0],
    }
    artifact = store.get(execution["generatedArtifactIds"][0])
    assert artifact["artifactType"] == "ISSUE_ANALYSIS"
    assert artifact["evaluationResultIds"] == [evaluation["id"]]
    assert artifact["repositoryRevision"] == REVISION
    assert json.loads(handler._artifact_store.get_content(artifact["id"])) == VALID_ANALYSIS


def test_scheduler_owns_terminal_transition_and_propagates_revision_evidence() -> None:
    resources, task = resource_collection()
    store = InMemoryRuntimeObjectStore()
    workflow = workflow_execution()
    workflow["taskExecutionIds"] = []
    store.create(workflow, deterministic_key="workflow")
    artifact_store = InMemoryGeneratedArtifactStore(runtime_store=store)
    handler = AnalyzeIssueTaskHandler(
        resources=resources,
        runtime_store=store,
        context_builder=ContextBuilder(
            repository_knowledge=repository_provider(),
            artifact_store=artifact_store,
            runtime_store=store,
        ),
        artifact_store=artifact_store,
        model_adapter=FakeModelAdapter(
            [ModelResponse(output=VALID_ANALYSIS, usage=ModelUsage(1, 1), latency_ms=1)]
        ),
        event_resolver=lambda event_id: normalized_event() if event_id == EVENT_ID else None,
        clock=lambda: TIMESTAMP,
    )
    workflow_ref = ResourceRef("Workflow", "issue-to-pr", "1.0.0")
    plan = TaskDagPlan(
        workflow_ref=workflow_ref,
        nodes=(TaskPlanNode(task.ref, task, (), ()),),
        ready_groups=((task.ref,),),
    )

    result = WorkflowScheduler(store, handler, clock=lambda: TIMESTAMP).reconcile(
        plan, store.get(WORKFLOW_ID)
    )

    execution = result.task_executions[0]
    assert execution["status"] == "SUCCEEDED"
    assert execution["provenance"]["repositoryRevision"] == REVISION
    assert execution["provenance"]["knowledgeGraphVersion"] == "snapshot-analyze-v1"
    assert len(execution["generatedArtifactIds"]) == 1


def test_invalid_model_output_is_an_evaluation_failure_without_artifact() -> None:
    store, handler, task, adapter = setup_handler(
        ModelResponse(
            output={"requestedChange": "Missing required fields"},
            usage=ModelUsage(input_tokens=10, output_tokens=4),
            latency_ms=2,
        )
    )

    result = handler.execute(task, store.get(TASK_EXECUTION_ID))

    assert result.succeeded is False
    assert result.failure_class is FailureClass.EVALUATION
    assert "acceptanceCriteria" in result.message
    assert len(adapter.requests) == 1
    execution = store.get(TASK_EXECUTION_ID)
    assert len(execution["agentInvocationIds"]) == 1
    assert "generatedArtifactIds" not in execution
    assert len(execution["evaluationResultIds"]) == 1
    invocation = store.get(execution["agentInvocationIds"][0])
    assert invocation["status"] == "FAILED"
    assert invocation["failure"]["class"] == "EVALUATION"
    evaluation = store.get(execution["evaluationResultIds"][0])
    assert evaluation["outcome"] == "FAIL"
    assert evaluation["target"] == {
        "type": "AgentInvocation",
        "id": invocation["id"],
    }
    assert any(
        error["path"] == "$.acceptanceCriteria"
        for error in evaluation["evidence"]["errors"]
    )


def test_missing_context_fails_configuration_before_model_invocation() -> None:
    store, handler, task, adapter = setup_handler(
        ModelResponse(output=VALID_ANALYSIS, usage=ModelUsage(1, 1), latency_ms=1),
        include_event=False,
    )

    result = handler.execute(task, store.get(TASK_EXECUTION_ID))

    assert result.succeeded is False
    assert result.failure_class is FailureClass.CONFIGURATION
    assert "event is required" in result.message
    assert adapter.requests == []
    execution = store.get(TASK_EXECUTION_ID)
    assert "contextPackageId" not in execution


def test_recoverable_provider_failure_is_returned_for_scheduler_retry() -> None:
    store, handler, task, adapter = setup_handler(
        ModelInvocationError(
            "provider temporarily unavailable",
            classification=ModelErrorClass.RECOVERABLE,
            code="unavailable",
        )
    )

    result = handler.execute(task, store.get(TASK_EXECUTION_ID))

    assert result.succeeded is False
    assert result.failure_class is FailureClass.RECOVERABLE
    assert result.message == "provider temporarily unavailable"
    assert len(adapter.requests) == 1
    execution = store.get(TASK_EXECUTION_ID)
    assert execution["contextPackageId"].startswith("contextpackage-")
    assert execution["resolvedAgentId"].startswith("resolvedagent-")
    invocation = store.get(execution["agentInvocationIds"][0])
    assert invocation["status"] == "FAILED"
    assert invocation["failure"]["retryable"] is True
    assert "generatedArtifactIds" not in execution


def test_unconditional_agent_tool_denial_is_classified_as_policy() -> None:
    store, handler, task, adapter = setup_handler(
        ModelResponse(output=VALID_ANALYSIS, usage=ModelUsage(1, 1), latency_ms=1),
        resource_set=denied_resource_collection(),
    )

    result = handler.execute(task, store.get(TASK_EXECUTION_ID))

    assert result.succeeded is False
    assert result.failure_class is FailureClass.POLICY
    assert "denies Tool/github-read-issue:1.0.0" in result.message
    assert adapter.requests == []
    execution = store.get(TASK_EXECUTION_ID)
    assert execution["contextPackageId"].startswith("contextpackage-")
    assert "resolvedAgentId" not in execution


def setup_handler(
    outcome: ModelResponse | ModelInvocationError,
    *,
    include_event: bool = True,
    resource_set: tuple[ResourceCollection, Resource] | None = None,
) -> tuple[
    InMemoryRuntimeObjectStore,
    AnalyzeIssueTaskHandler,
    Resource,
    FakeModelAdapter,
]:
    resources, task = resource_set or resource_collection()
    store = InMemoryRuntimeObjectStore()
    store.create(workflow_execution(), deterministic_key="workflow")
    store.create(task_execution(), deterministic_key="task")
    artifact_store = InMemoryGeneratedArtifactStore(runtime_store=store)
    context_builder = ContextBuilder(
        repository_knowledge=repository_provider(),
        artifact_store=artifact_store,
        runtime_store=store,
    )
    adapter = FakeModelAdapter([outcome])
    event = normalized_event() if include_event else None
    handler = AnalyzeIssueTaskHandler(
        resources=resources,
        runtime_store=store,
        context_builder=context_builder,
        artifact_store=artifact_store,
        model_adapter=adapter,
        event_resolver=lambda event_id: event if event_id == EVENT_ID else None,
        clock=lambda: TIMESTAMP,
    )
    return store, handler, task, adapter


def resource_collection() -> tuple[ResourceCollection, Resource]:
    workspace = resource("Workspace", "local", {"repository": "octo/repo"})
    task = resource(
        "Task",
        "analyze-issue",
        {
            "objective": "Analyze the normalized GitHub issue.",
            "agentRef": ref("Agent", "issue-analyzer"),
            "outputs": ISSUE_ANALYSIS_SCHEMA,
            "requiredContext": ["issue"],
            "inputContextTokenBudget": 32_000,
            "evaluations": [ref("Evaluation", "issue-analysis-schema")],
        },
    )
    agent = resource(
        "Agent",
        "issue-analyzer",
        {
            "role": "Issue Analyzer",
            "promptRef": ref("Prompt", "issue-analysis"),
            "modelRef": ref("Model", "fake-reasoning"),
            "outputSchema": ISSUE_ANALYSIS_SCHEMA,
        },
    )
    prompt = resource(
        "Prompt",
        "issue-analysis",
        {
            "system": "Analyze only the supplied ContextPackage.",
            "formatting": "Return JSON matching the output schema.",
        },
    )
    model = resource(
        "Model",
        "fake-reasoning",
        {
            "provider": "local",
            "model": "fake-analyzer-v1",
            "parameters": {"temperature": 0},
            "tokenLimit": 4_096,
            "timeoutMs": 5_000,
        },
    )
    evaluation = resource(
        "Evaluation",
        "issue-analysis-schema",
        {"type": "schema", "inputSchema": ISSUE_ANALYSIS_SCHEMA},
    )
    values = (workspace, task, agent, prompt, model, evaluation)
    return ResourceCollection(workspace=workspace, resources=values), task


def denied_resource_collection() -> tuple[ResourceCollection, Resource]:
    resources, task = resource_collection()
    original_agent = next(item for item in resources.resources if item.kind == "Agent")
    agent_spec = deepcopy(original_agent.data["spec"])
    agent_spec["toolRefs"] = [ref("Tool", "github-read-issue")]
    agent_spec["policyRefs"] = [ref("Policy", "deny-github-read")]
    denied_agent = resource("Agent", "issue-analyzer", agent_spec)
    tool = resource(
        "Tool",
        "github-read-issue",
        {
            "category": "external-service",
            "capabilities": ["github.issue.read"],
            "inputSchema": {"type": "object"},
            "outputSchema": {"type": "object"},
        },
    )
    policy = resource(
        "Policy",
        "deny-github-read",
        {
            "type": "pre-execution-capability",
            "rules": [
                {
                    "effect": "deny",
                    "capabilities": ["github.issue.read"],
                    "reason": "Issue reads are disabled for this Task.",
                }
            ],
        },
    )
    values = tuple(
        item for item in resources.resources if item.kind != "Agent"
    ) + (denied_agent, tool, policy)
    return ResourceCollection(workspace=resources.workspace, resources=values), task


def resource(kind: str, name: str, spec: dict) -> Resource:
    resource_ref = ResourceRef(kind, name, "1.0.0")
    return Resource(
        ref=resource_ref,
        path=Path(f".ai/{kind.lower()}s/{name}.yaml"),
        data={
            "apiVersion": "aep.dev/v1alpha1",
            "kind": kind,
            "metadata": {"name": name, "version": "1.0.0"},
            "spec": spec,
        },
        references=(),
    )


def ref(kind: str, name: str) -> dict[str, str]:
    return {"kind": kind, "name": name, "version": "1.0.0"}


def repository_provider() -> InMemoryRepositoryKnowledgeProvider:
    return InMemoryRepositoryKnowledgeProvider(
        RepositoryKnowledgeSnapshot(
            api_version="aep.dev/repository-knowledge/v1",
            snapshot_version="snapshot-analyze-v1",
            repository_revision=REVISION,
            created_at=TIMESTAMP,
            scanner_version="mvp-scanner/1.0.0",
            files=(),
            documentation=(),
            dependency_manifests=(),
            test_command_hints=(),
        )
    )


def workflow_execution() -> dict:
    return {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "WorkflowExecution",
        "id": WORKFLOW_ID,
        "traceId": "trace-analyze-0001",
        "createdAt": TIMESTAMP,
        "updatedAt": TIMESTAMP,
        "provenance": {
            "actor": "workflow-controller",
            "repositoryRevision": REVISION,
            "resourceRefs": [ref("Event", "github-issue-created")],
        },
        "workflowRef": ref("Workflow", "issue-to-pr"),
        "eventRef": ref("Event", "github-issue-created"),
        "eventId": EVENT_ID,
        "repositoryRevision": REVISION,
        "knowledgeGraphVersion": "snapshot-analyze-v1",
        "status": "RUNNING",
        "startedAt": TIMESTAMP,
        "taskExecutionIds": [TASK_EXECUTION_ID],
    }


def task_execution() -> dict:
    return {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "TaskExecution",
        "id": TASK_EXECUTION_ID,
        "traceId": "trace-analyze-0001",
        "createdAt": TIMESTAMP,
        "updatedAt": TIMESTAMP,
        "provenance": {
            "actor": "workflow-scheduler",
            "workflowExecutionId": WORKFLOW_ID,
            "repositoryRevision": REVISION,
            "resourceRefs": [ref("Task", "analyze-issue")],
        },
        "workflowExecutionId": WORKFLOW_ID,
        "taskRef": ref("Task", "analyze-issue"),
        "attempt": 1,
        "status": "RUNNING",
        "dependencyTaskExecutionIds": [],
        "startedAt": TIMESTAMP,
    }


def normalized_event() -> dict:
    return {
        "id": EVENT_ID,
        "source": "github",
        "type": "github.issue.created",
        "repository": {"id": 123, "full_name": "octo/repo"},
        "issue": {
            "id": 456,
            "number": 29,
            "title": "Add AnalyzeIssue handling",
            "body": "Create the first cognitive Task handler.",
        },
        "sender": {"id": 789, "login": "octocat"},
        "receivedAt": TIMESTAMP,
        "deduplicationKey": "github:delivery:analyze-0001",
    }
