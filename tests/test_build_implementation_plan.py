import json
from pathlib import Path

from aep.build_implementation_plan import BuildImplementationPlanTaskHandler
from aep.context_builder import ContextBuilder
from aep.generated_artifact_store import InMemoryGeneratedArtifactStore
from aep.model_invocation import FakeModelAdapter, ModelResponse, ModelUsage
from aep.repository_knowledge import (
    InMemoryRepositoryKnowledgeProvider,
    RepositoryFile,
    RepositoryKnowledgeSnapshot,
    SourceProvenance,
)
from aep.resource_loader import Resource, ResourceCollection, ResourceRef
from aep.runtime_store import InMemoryRuntimeObjectStore
from aep.task_execution import FailureClass


TIMESTAMP = "2026-08-06T12:00:00Z"
REVISION = "abc1234"
WORKFLOW_ID = "workflowexecution-bbbbbbbbbbbb"
PRODUCER_ID = "taskexecution-111111111111"
TASK_EXECUTION_ID = "taskexecution-222222222222"
UPSTREAM_INVOCATION_ID = "agentinvocation-444444444444"
UPSTREAM_EVALUATION_ID = "evaluationresult-555555555555"

PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "intendedFiles",
        "tests",
        "assumptions",
        "risks",
        "implementationSteps",
    ],
    "properties": {
        "intendedFiles": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "minItems": 1,
        },
        "tests": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "minItems": 1,
        },
        "assumptions": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
        },
        "risks": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
        },
        "implementationSteps": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "minItems": 1,
        },
    },
}

VALID_PLAN = {
    "intendedFiles": ["src/aep/build_implementation_plan.py"],
    "tests": ["python -m pytest tests/test_build_implementation_plan.py"],
    "assumptions": ["The prior analysis is approved."],
    "risks": ["Malformed model output must not become an artifact."],
    "implementationSteps": [
        "Read the issue analysis and repository context.",
        "Implement the handler and its tests.",
    ],
}


def test_success_consumes_analysis_and_persists_evaluated_plan() -> None:
    store, handler, task, adapter = setup_handler(VALID_PLAN)

    result = handler.execute(task, store.get(TASK_EXECUTION_ID))

    assert result.succeeded is True
    assert len(adapter.requests) == 1
    elements = adapter.requests[0].input["contextPackage"]["elements"]
    artifact_elements = [element for element in elements if element["type"] == "artifact"]
    repository_elements = [
        element for element in elements if element["type"] == "repository"
    ]
    assert len(artifact_elements) == 1
    assert artifact_elements[0]["content"]["metadata"]["artifactType"] == (
        "ISSUE_ANALYSIS"
    )
    assert artifact_elements[0]["content"]["content"]["requestedChange"] == (
        "Add implementation-plan handling."
    )
    assert repository_elements

    execution = store.get(TASK_EXECUTION_ID)
    evaluation = store.get(execution["evaluationResultIds"][0])
    artifact = store.get(execution["generatedArtifactIds"][0])
    assert execution["status"] == "RUNNING"
    assert evaluation["outcome"] == "PASS"
    assert artifact["artifactType"] == "IMPLEMENTATION_PLAN"
    assert artifact["evaluationResultIds"] == [evaluation["id"]]
    assert json.loads(handler._artifact_store.get_content(artifact["id"])) == VALID_PLAN


def test_missing_prior_analysis_fails_before_context_or_model_invocation() -> None:
    store, handler, task, adapter = setup_handler(VALID_PLAN, publish_analysis=False)

    result = handler.execute(task, store.get(TASK_EXECUTION_ID))

    assert result.succeeded is False
    assert result.failure_class is FailureClass.CONFIGURATION
    assert "prior ISSUE_ANALYSIS" in result.message
    assert adapter.requests == []
    assert "contextPackageId" not in store.get(TASK_EXECUTION_ID)


def test_unaudited_prior_analysis_is_rejected() -> None:
    store, handler, task, adapter = setup_handler(VALID_PLAN, audit_analysis=False)

    result = handler.execute(task, store.get(TASK_EXECUTION_ID))

    assert result.succeeded is False
    assert result.failure_class is FailureClass.CONFIGURATION
    assert "not attached" in result.message
    assert adapter.requests == []
    assert "contextPackageId" not in store.get(TASK_EXECUTION_ID)


def test_missing_required_plan_section_is_an_evaluation_failure() -> None:
    output = dict(VALID_PLAN)
    del output["risks"]
    store, handler, task, adapter = setup_handler(output)

    result = handler.execute(task, store.get(TASK_EXECUTION_ID))

    assert result.succeeded is False
    assert result.failure_class is FailureClass.EVALUATION
    assert "risks" in result.message
    execution = store.get(TASK_EXECUTION_ID)
    assert "generatedArtifactIds" not in execution
    evaluation = store.get(execution["evaluationResultIds"][0])
    assert evaluation["outcome"] == "FAIL"
    assert any(error["path"] == "$.risks" for error in evaluation["evidence"]["errors"])


def test_invalid_non_object_output_is_rejected_without_artifact() -> None:
    store, handler, task, adapter = setup_handler(["not", "a", "plan"])

    result = handler.execute(task, store.get(TASK_EXECUTION_ID))

    assert result.succeeded is False
    assert result.failure_class is FailureClass.EVALUATION
    assert len(adapter.requests) == 1
    execution = store.get(TASK_EXECUTION_ID)
    assert "generatedArtifactIds" not in execution
    assert store.get(execution["evaluationResultIds"][0])["outcome"] == "FAIL"


def test_mismatched_task_and_evaluation_schemas_fail_closed() -> None:
    store, handler, task, adapter = setup_handler(
        VALID_PLAN,
        evaluation_schema={"type": "object"},
    )

    result = handler.execute(task, store.get(TASK_EXECUTION_ID))

    assert result.succeeded is False
    assert result.failure_class is FailureClass.CONFIGURATION
    assert "inputSchema must match Task.spec.outputs" in result.message
    assert adapter.requests == []
    assert "contextPackageId" not in store.get(TASK_EXECUTION_ID)


def setup_handler(
    output: object,
    *,
    publish_analysis: bool = True,
    audit_analysis: bool = True,
    evaluation_schema: dict | None = None,
) -> tuple[
    InMemoryRuntimeObjectStore,
    BuildImplementationPlanTaskHandler,
    Resource,
    FakeModelAdapter,
]:
    resources, task = resource_collection(evaluation_schema=evaluation_schema)
    store = InMemoryRuntimeObjectStore()
    store.create(workflow_execution(), deterministic_key="workflow")
    store.create(producer_execution(audited=audit_analysis), deterministic_key="producer")
    store.create(task_execution(), deterministic_key="planner")
    if audit_analysis:
        store.create(upstream_evaluation_result(), deterministic_key="upstream-evaluation")
    artifact_store = InMemoryGeneratedArtifactStore(runtime_store=store)
    if publish_analysis:
        artifact_store.publish(issue_analysis_metadata(), issue_analysis())
    adapter = FakeModelAdapter(
        [ModelResponse(output=output, usage=ModelUsage(20, 15), latency_ms=3)]
    )
    handler = BuildImplementationPlanTaskHandler(
        resources=resources,
        runtime_store=store,
        context_builder=ContextBuilder(
            repository_knowledge=repository_provider(),
            artifact_store=artifact_store,
            runtime_store=store,
        ),
        artifact_store=artifact_store,
        model_adapter=adapter,
        event_resolver=lambda _: None,
        clock=lambda: TIMESTAMP,
    )
    return store, handler, task, adapter


def resource_collection(
    *, evaluation_schema: dict | None = None
) -> tuple[ResourceCollection, Resource]:
    workspace = resource("Workspace", "local", {"repository": "octo/repo"})
    task = resource(
        "Task",
        "build-implementation-plan",
        {
            "objective": "Build an actionable implementation plan.",
            "agentRef": ref("Agent", "planner"),
            "outputs": PLAN_SCHEMA,
            "requiredContext": ["prior-artifacts", "repository-inventory"],
            "evaluations": [ref("Evaluation", "implementation-plan-schema")],
        },
    )
    agent = resource(
        "Agent",
        "planner",
        {
            "role": "Planner",
            "promptRef": ref("Prompt", "implementation-plan"),
            "modelRef": ref("Model", "fake-planner"),
            "outputSchema": PLAN_SCHEMA,
        },
    )
    prompt = resource(
        "Prompt",
        "implementation-plan",
        {
            "system": "Plan only from the supplied ContextPackage.",
            "formatting": "Return JSON matching the output schema.",
        },
    )
    model = resource(
        "Model",
        "fake-planner",
        {
            "provider": "local",
            "model": "fake-planner-v1",
            "parameters": {"temperature": 0},
            "tokenLimit": 4_096,
            "timeoutMs": 5_000,
        },
    )
    evaluation = resource(
        "Evaluation",
        "implementation-plan-schema",
        {"type": "schema", "inputSchema": evaluation_schema or PLAN_SCHEMA},
    )
    values = (workspace, task, agent, prompt, model, evaluation)
    return ResourceCollection(workspace=workspace, resources=values), task


def workflow_execution() -> dict:
    return {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "WorkflowExecution",
        "id": WORKFLOW_ID,
        "traceId": "trace-plan-0001",
        "createdAt": TIMESTAMP,
        "updatedAt": TIMESTAMP,
        "provenance": {
            "actor": "workflow-controller",
            "repositoryRevision": REVISION,
            "resourceRefs": [],
        },
        "workflowRef": ref("Workflow", "issue-to-pr"),
        "repositoryRevision": REVISION,
        "knowledgeGraphVersion": "snapshot-plan-v1",
        "status": "RUNNING",
        "startedAt": TIMESTAMP,
        "taskExecutionIds": [PRODUCER_ID, TASK_EXECUTION_ID],
    }


def producer_execution(*, audited: bool) -> dict:
    record = task_execution()
    record.update(
        {
            "id": PRODUCER_ID,
            "taskRef": ref("Task", "analyze-issue"),
            "status": "SUCCEEDED",
            "completedAt": TIMESTAMP,
            "dependencyTaskExecutionIds": [],
        }
    )
    record["provenance"] = {
        **record["provenance"],
        "resourceRefs": [ref("Task", "analyze-issue")],
    }
    if audited:
        record["agentInvocationIds"] = [UPSTREAM_INVOCATION_ID]
        record["evaluationResultIds"] = [UPSTREAM_EVALUATION_ID]
        record["generatedArtifactIds"] = ["generatedartifact-333333333333"]
    return record


def task_execution() -> dict:
    return {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "TaskExecution",
        "id": TASK_EXECUTION_ID,
        "traceId": "trace-plan-0001",
        "createdAt": TIMESTAMP,
        "updatedAt": TIMESTAMP,
        "provenance": {
            "actor": "workflow-scheduler",
            "workflowExecutionId": WORKFLOW_ID,
            "repositoryRevision": REVISION,
            "resourceRefs": [ref("Task", "build-implementation-plan")],
        },
        "workflowExecutionId": WORKFLOW_ID,
        "taskRef": ref("Task", "build-implementation-plan"),
        "attempt": 1,
        "status": "RUNNING",
        "dependencyTaskExecutionIds": [PRODUCER_ID],
        "startedAt": TIMESTAMP,
    }


def issue_analysis_metadata() -> dict:
    return {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "GeneratedArtifact",
        "id": "generatedartifact-333333333333",
        "traceId": "trace-plan-0001",
        "createdAt": TIMESTAMP,
        "updatedAt": TIMESTAMP,
        "provenance": {
            "actor": "analyze-issue-task-handler",
            "workflowExecutionId": WORKFLOW_ID,
            "taskExecutionId": PRODUCER_ID,
            "repositoryRevision": REVISION,
            "resourceRefs": [],
        },
        "taskExecutionId": PRODUCER_ID,
        "artifactType": "ISSUE_ANALYSIS",
        "repositoryRevision": REVISION,
        "mediaType": "application/json",
        "evaluationResultIds": [UPSTREAM_EVALUATION_ID],
    }


def upstream_evaluation_result() -> dict:
    return {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "EvaluationResult",
        "id": UPSTREAM_EVALUATION_ID,
        "traceId": "trace-plan-0001",
        "createdAt": TIMESTAMP,
        "updatedAt": TIMESTAMP,
        "provenance": {
            "actor": "schema-evaluator",
            "workflowExecutionId": WORKFLOW_ID,
            "taskExecutionId": PRODUCER_ID,
            "repositoryRevision": REVISION,
            "resourceRefs": [ref("Evaluation", "issue-analysis-schema")],
        },
        "taskExecutionId": PRODUCER_ID,
        "evaluationRef": ref("Evaluation", "issue-analysis-schema"),
        "target": {"type": "AgentInvocation", "id": UPSTREAM_INVOCATION_ID},
        "status": "SUCCEEDED",
        "outcome": "PASS",
        "startedAt": TIMESTAMP,
        "completedAt": TIMESTAMP,
    }


def issue_analysis() -> dict:
    return {
        "requestedChange": "Add implementation-plan handling.",
        "acceptanceCriteria": ["Persist an evaluated plan."],
        "risks": ["The plan could omit required sections."],
        "likelyRepositoryAreas": ["src/aep", "tests"],
    }


def repository_provider() -> InMemoryRepositoryKnowledgeProvider:
    provenance = SourceProvenance(
        source_path="src/aep/analyze_issue.py",
        repository_revision=REVISION,
        scanned_at=TIMESTAMP,
        scanner_version="mvp-scanner/1.0.0",
    )
    source = RepositoryFile(
        path="src/aep/analyze_issue.py",
        language="Python",
        is_documentation=False,
        provenance=provenance,
    )
    return InMemoryRepositoryKnowledgeProvider(
        RepositoryKnowledgeSnapshot(
            api_version="aep.dev/repository-knowledge/v1",
            snapshot_version="snapshot-plan-v1",
            repository_revision=REVISION,
            created_at=TIMESTAMP,
            scanner_version="mvp-scanner/1.0.0",
            files=(source,),
            documentation=(),
            dependency_manifests=(),
            test_command_hints=(),
        )
    )


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
