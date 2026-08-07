from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource as SchemaResource
from referencing.jsonschema import DRAFT202012

from aep.evaluate_acceptance import EvaluateAcceptanceTaskHandler
from aep.generated_artifact_store import InMemoryGeneratedArtifactStore
from aep.resource_loader import Resource, ResourceCollection, ResourceRef
from aep.runtime_store import InMemoryRuntimeObjectStore
from aep.task_execution import FailureClass


TIMESTAMP = "2026-08-06T12:00:00Z"
REVISION = "a" * 40
TRACE_ID = "trace-acceptance-0001"
WORKFLOW_ID = "workflowexecution-aaaaaaaaaaaa"
ANALYZE_ID = "taskexecution-111111111111"
PLAN_ID = "taskexecution-222222222222"
PATCH_ID = "taskexecution-333333333333"
VALIDATION_ID = "taskexecution-bbbbbbbbbbbb"
ACCEPTANCE_ID = "taskexecution-cccccccccccc"
ANALYSIS_ARTIFACT_ID = "generatedartifact-111111111111"
PLAN_ARTIFACT_ID = "generatedartifact-222222222222"
PATCH_ARTIFACT_ID = "generatedartifact-333333333333"
ARTIFACT_ID = "generatedartifact-dddddddddddd"
EXTRA_ARTIFACT_ID = "generatedartifact-999999999999"
ANALYSIS_EVALUATION_ID = "evaluationresult-111111111111"
PLAN_EVALUATION_ID = "evaluationresult-222222222222"
PATCH_EVALUATION_ID = "evaluationresult-333333333333"
BUILD_ID = "evaluationresult-eeeeeeeeeeee"
TEST_ID = "evaluationresult-ffffffffffff"
ANALYZE_INVOCATION_ID = "agentinvocation-111111111111"
PLAN_INVOCATION_ID = "agentinvocation-222222222222"
DOCKER_INVOCATION_ID = "toolinvocation-333333333333"
ROOT = Path(__file__).parents[1]


def test_all_pass_persists_complete_summary_without_model_evidence() -> None:
    store, handler, task, _artifacts = setup_handler()
    assert "repositoryRevision" not in store.get(DOCKER_INVOCATION_ID)["provenance"]

    result = handler.execute(task, store.get(ACCEPTANCE_ID))

    assert result.succeeded is True
    attached = store.get(ACCEPTANCE_ID)["evaluationResultIds"]
    assert len(attached) == 1
    summary = store.get(attached[0])
    assert summary["status"] == "SUCCEEDED"
    assert summary["outcome"] == "PASS"
    assert summary["evaluationRef"] == ref("Evaluation", "acceptance")
    assert summary["target"] == {"type": "TaskExecution", "id": ACCEPTANCE_ID}
    assert summary["evidence"]["requiredArtifactIds"] == [
        ANALYSIS_ARTIFACT_ID,
        PLAN_ARTIFACT_ID,
        PATCH_ARTIFACT_ID,
        ARTIFACT_ID,
    ]
    assert summary["evidence"]["requiredEvaluationResultIds"] == [
        ANALYSIS_EVALUATION_ID,
        PLAN_EVALUATION_ID,
        PATCH_EVALUATION_ID,
        BUILD_ID,
        TEST_ID,
    ]
    assert summary["evidence"]["checks"] == {
        "complete": True,
        "sameExecution": True,
        "sameRevision": True,
        "provenanceConsistent": True,
        "allEvaluationsPassed": True,
    }
    assert summary["evidence"]["issues"] == []
    assert "modelInvocationIds" not in store.get(ACCEPTANCE_ID)
    assert not any(
        value["kind"] == "ModelInvocation"
        for value in store.list_by_workflow_execution(WORKFLOW_ID)
    )


def test_evaluate_acceptance_task_fixture_matches_resource_contract() -> None:
    schema_root = ROOT / "schemas" / "resources" / "v1"
    schemas = [
        json.loads((schema_root / name).read_text(encoding="utf-8"))
        for name in ("resource-definitions.schema.json", "task.schema.json")
    ]
    registry = Registry()
    for schema in schemas:
        registry = registry.with_resource(
            schema["$id"],
            SchemaResource.from_contents(
                schema, default_specification=DRAFT202012
            ),
        )
    validator = Draft202012Validator(schemas[-1], registry=registry)
    fixture = json.loads(
        (
            ROOT
            / "fixtures"
            / "resources"
            / "valid"
            / "evaluate-acceptance-task.json"
        ).read_text(encoding="utf-8")
    )

    assert list(validator.iter_errors(fixture)) == []


@pytest.mark.parametrize("missing", ["artifact", "evaluation"])
def test_missing_required_evidence_persists_failed_summary(missing: str) -> None:
    store, handler, task, _artifacts = setup_handler(missing=missing)

    result = handler.execute(task, store.get(ACCEPTANCE_ID))

    assert result.failure_class is FailureClass.EVALUATION
    summary = acceptance_summary(store)
    assert summary["status"] == "SUCCEEDED"
    assert summary["outcome"] == "FAIL"
    assert summary["evidence"]["checks"]["complete"] is False
    assert any(
        issue["code"] == ("MISSING_ARTIFACT" if missing == "artifact" else "MISSING_EVALUATION")
        for issue in summary["evidence"]["issues"]
    )


def test_failed_evaluation_fails_acceptance_with_supporting_reference() -> None:
    store, handler, task, _artifacts = setup_handler(failed_evaluation=True)

    result = handler.execute(task, store.get(ACCEPTANCE_ID))

    assert result.failure_class is FailureClass.EVALUATION
    summary = acceptance_summary(store)
    assert summary["outcome"] == "FAIL"
    assert summary["evidence"]["checks"]["allEvaluationsPassed"] is False
    assert any(
        issue["code"] == "FAILED_EVALUATION" and issue["subjectId"] == TEST_ID
        for issue in summary["evidence"]["issues"]
    )


def test_stale_revision_fails_acceptance_deterministically() -> None:
    store, handler, task, _artifacts = setup_handler(stale_evaluation=True)

    first = handler.execute(task, store.get(ACCEPTANCE_ID))
    saved = deepcopy(dict(acceptance_summary(store)))
    second = handler.execute(task, store.get(ACCEPTANCE_ID))

    assert first.failure_class is FailureClass.EVALUATION
    assert second.failure_class is FailureClass.EVALUATION
    assert acceptance_summary(store) == saved
    assert saved["evidence"]["checks"]["sameRevision"] is False
    assert any(issue["code"] == "STALE_REVISION" for issue in saved["evidence"]["issues"])


def test_stale_predecessor_provenance_fails_acceptance() -> None:
    store, handler, task, _artifacts = setup_handler(
        inconsistency="stale-task-provenance"
    )

    result = handler.execute(task, store.get(ACCEPTANCE_ID))

    assert result.failure_class is FailureClass.EVALUATION
    assert any(
        issue["code"] == "STALE_REVISION"
        and issue["subjectId"] == PATCH_ID
        for issue in acceptance_summary(store)["evidence"]["issues"]
    )


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ("failed-pass", "INCONSISTENT_EVALUATION"),
        ("cross-execution", "CROSS_EXECUTION"),
        ("unattached-artifact", "INCONSISTENT_ARTIFACT_ATTACHMENTS"),
        ("duplicate-artifact", "INCONSISTENT_ARTIFACT_ATTACHMENTS"),
        ("wrong-artifact-type", "WRONG_ARTIFACT_TYPE"),
        ("wrong-evaluation-target", "INVALID_EVALUATION_TARGET"),
        ("malformed-evaluation-target", "INVALID_EVALUATION_TARGET"),
    ],
)
def test_inconsistent_evidence_fails_closed(mutation: str, code: str) -> None:
    store, handler, task, _artifacts = setup_handler(inconsistency=mutation)

    result = handler.execute(task, store.get(ACCEPTANCE_ID))

    assert result.failure_class is FailureClass.EVALUATION
    assert any(
        issue["code"] == code
        for issue in acceptance_summary(store)["evidence"]["issues"]
    )


def test_invalid_handler_configuration_does_not_persist_summary() -> None:
    store, handler, task, _artifacts = setup_handler()
    task_without_acceptance = resource(
        "Task",
        "evaluate-acceptance",
        {"objective": "Evaluate.", "outputs": {"type": "object"}, "evaluations": []},
    )

    result = handler.execute(task_without_acceptance, store.get(ACCEPTANCE_ID))

    assert result.failure_class is FailureClass.CONFIGURATION
    assert "exactly one acceptance Evaluation" in result.message
    assert store.get(ACCEPTANCE_ID).get("evaluationResultIds", []) == []


def setup_handler(
    *,
    missing: str | None = None,
    failed_evaluation: bool = False,
    stale_evaluation: bool = False,
    inconsistency: str | None = None,
):
    workspace = resource("Workspace", "workspace", {"repository": {}})
    analysis_evaluation = resource(
        "Evaluation", "analysis-schema", {"type": "schema"}
    )
    plan_evaluation = resource("Evaluation", "plan-schema", {"type": "schema"})
    patch_evaluation = resource("Evaluation", "patch", {"type": "patch"})
    build_evaluation = resource("Evaluation", "build", {"type": "build"})
    test_evaluation = resource("Evaluation", "test", {"type": "test"})
    acceptance_evaluation = resource(
        "Evaluation", "acceptance", {"type": "acceptance"}
    )
    analyze_task = evidence_task(
        "analyze-issue", "analysis-schema"
    )
    plan_task = evidence_task(
        "build-implementation-plan", "plan-schema"
    )
    patch_task = evidence_task("generate-patch", "patch")
    validation_task = resource(
        "Task",
        "run-validation",
        {
            "objective": "Build and test.",
            "outputs": {"type": "object"},
            "evaluations": [
                ref("Evaluation", "build"),
                ref("Evaluation", "test"),
            ],
        },
    )
    acceptance_task = resource(
        "Task",
        "evaluate-acceptance",
        {
            "objective": "Aggregate required evidence.",
            "outputs": {"type": "object"},
            "evaluations": [ref("Evaluation", "acceptance")],
        },
    )
    resources = ResourceCollection(
        workspace,
        (
            workspace,
            analyze_task,
            plan_task,
            patch_task,
            validation_task,
            acceptance_task,
            analysis_evaluation,
            plan_evaluation,
            patch_evaluation,
            build_evaluation,
            test_evaluation,
            acceptance_evaluation,
        ),
    )
    store = InMemoryRuntimeObjectStore()
    store.create(workflow_execution(), deterministic_key="workflow")

    predecessor_records = [
        predecessor_execution(
            ANALYZE_ID,
            "analyze-issue",
            (),
            ANALYSIS_ARTIFACT_ID,
            (ANALYSIS_EVALUATION_ID,),
        ),
        predecessor_execution(
            PLAN_ID,
            "build-implementation-plan",
            (ANALYZE_ID,),
            PLAN_ARTIFACT_ID,
            (PLAN_EVALUATION_ID,),
        ),
        predecessor_execution(
            PATCH_ID,
            "generate-patch",
            (PLAN_ID,),
            PATCH_ARTIFACT_ID,
            (PATCH_EVALUATION_ID,),
            revision=(
                "b" * 40
                if inconsistency == "stale-task-provenance"
                else REVISION
            ),
        ),
    ]
    for record in predecessor_records:
        store.create(record, deterministic_key=f"task:{record['id']}")

    producer = validation_execution()
    if missing == "evaluation":
        producer["evaluationResultIds"] = [BUILD_ID]
    if inconsistency == "unattached-artifact":
        producer["generatedArtifactIds"] = []
    if inconsistency == "duplicate-artifact":
        producer["generatedArtifactIds"] = [ARTIFACT_ID, EXTRA_ARTIFACT_ID]
    store.create(producer, deterministic_key="validation")
    store.create(acceptance_execution(), deterministic_key="acceptance")

    store.create(
        target_runtime(ANALYZE_INVOCATION_ID, "AgentInvocation", ANALYZE_ID),
        deterministic_key="analyze-invocation",
    )
    store.create(
        target_runtime(PLAN_INVOCATION_ID, "AgentInvocation", PLAN_ID),
        deterministic_key="plan-invocation",
    )
    docker_invocation = target_runtime(
        DOCKER_INVOCATION_ID, "ToolInvocation", VALIDATION_ID
    )
    if inconsistency == "malformed-evaluation-target":
        del docker_invocation["input"]
    store.create(docker_invocation, deterministic_key="docker-invocation")

    prior_evaluations = [
        evaluation_result(
            ANALYSIS_EVALUATION_ID,
            "analysis-schema",
            task_execution_id=ANALYZE_ID,
            target={"type": "AgentInvocation", "id": ANALYZE_INVOCATION_ID},
        ),
        evaluation_result(
            PLAN_EVALUATION_ID,
            "plan-schema",
            task_execution_id=PLAN_ID,
            target={"type": "AgentInvocation", "id": PLAN_INVOCATION_ID},
        ),
        evaluation_result(
            PATCH_EVALUATION_ID,
            "patch",
            task_execution_id=PATCH_ID,
            target={"type": "GeneratedArtifact", "id": PATCH_ARTIFACT_ID},
        ),
    ]
    for evaluation in prior_evaluations:
        store.create(evaluation, deterministic_key=f"evaluation:{evaluation['id']}")

    build = evaluation_result(BUILD_ID, "build")
    store.create(build, deterministic_key="build")
    if missing != "evaluation":
        test = evaluation_result(
            TEST_ID,
            "test",
            outcome="FAIL" if failed_evaluation else "PASS",
            status="FAILED" if inconsistency == "failed-pass" else "SUCCEEDED",
            revision="b" * 40 if stale_evaluation else REVISION,
            task_execution_id=(
                "taskexecution-999999999999"
                if inconsistency == "cross-execution"
                else VALIDATION_ID
            ),
            target=(
                {"type": "ToolInvocation", "id": ANALYZE_INVOCATION_ID}
                if inconsistency == "wrong-evaluation-target"
                else {"type": "ToolInvocation", "id": DOCKER_INVOCATION_ID}
            ),
        )
        store.create(test, deterministic_key="test")

    artifacts = InMemoryGeneratedArtifactStore(runtime_store=store)
    artifacts.publish(
        artifact_metadata(
            ANALYSIS_ARTIFACT_ID,
            ANALYZE_ID,
            "ISSUE_ANALYSIS",
            (ANALYSIS_EVALUATION_ID,),
        ),
        {"requestedChange": "test"},
    )
    artifacts.publish(
        artifact_metadata(
            PLAN_ARTIFACT_ID,
            PLAN_ID,
            "IMPLEMENTATION_PLAN",
            (PLAN_EVALUATION_ID,),
        ),
        {"steps": ["test"]},
    )
    artifacts.publish(
        artifact_metadata(
            PATCH_ARTIFACT_ID,
            PATCH_ID,
            "PATCH",
            (PATCH_EVALUATION_ID,),
        ),
        "diff --git a/app.py b/app.py\n",
    )
    if missing != "artifact":
        artifacts.publish(
            artifact_metadata(
                ARTIFACT_ID,
                VALIDATION_ID,
                (
                    "PULL_REQUEST_DESCRIPTION"
                    if inconsistency == "wrong-artifact-type"
                    else "EVALUATION_REPORT"
                ),
                (BUILD_ID, TEST_ID),
            ),
            {"status": "PASSED"},
        )
        if inconsistency == "duplicate-artifact":
            artifacts.publish(
                artifact_metadata(
                    EXTRA_ARTIFACT_ID,
                    VALIDATION_ID,
                    "EVALUATION_REPORT",
                    (BUILD_ID, TEST_ID),
                ),
                {"status": "PASSED", "duplicate": True},
            )
    handler = EvaluateAcceptanceTaskHandler(
        resources=resources,
        runtime_store=store,
        artifact_store=artifacts,
        clock=lambda: TIMESTAMP,
    )
    return store, handler, acceptance_task, artifacts


def workflow_execution() -> dict:
    return {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "WorkflowExecution",
        "id": WORKFLOW_ID,
        "traceId": TRACE_ID,
        "createdAt": TIMESTAMP,
        "updatedAt": TIMESTAMP,
        "provenance": {"actor": "test", "resourceRefs": []},
        "repositoryRevision": REVISION,
        "status": "RUNNING",
    }


def validation_execution() -> dict:
    return predecessor_execution(
        VALIDATION_ID,
        "run-validation",
        (PATCH_ID,),
        ARTIFACT_ID,
        (BUILD_ID, TEST_ID),
    )


def acceptance_execution() -> dict:
    return {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "TaskExecution",
        "id": ACCEPTANCE_ID,
        "traceId": TRACE_ID,
        "createdAt": TIMESTAMP,
        "updatedAt": TIMESTAMP,
        "provenance": {"actor": "test", "resourceRefs": []},
        "workflowExecutionId": WORKFLOW_ID,
        "taskRef": ref("Task", "evaluate-acceptance"),
        "attempt": 1,
        "status": "RUNNING",
        "dependencyTaskExecutionIds": [VALIDATION_ID],
    }


def evaluation_result(
    result_id: str,
    name: str,
    *,
    outcome: str = "PASS",
    status: str = "SUCCEEDED",
    revision: str = REVISION,
    task_execution_id: str = VALIDATION_ID,
    target: dict | None = None,
) -> dict:
    return {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "EvaluationResult",
        "id": result_id,
        "traceId": TRACE_ID,
        "createdAt": TIMESTAMP,
        "updatedAt": TIMESTAMP,
        "provenance": {
            "actor": "test-evaluator",
            "workflowExecutionId": WORKFLOW_ID,
            "taskExecutionId": task_execution_id,
            "repositoryRevision": revision,
            "resourceRefs": [ref("Evaluation", name)],
        },
        "taskExecutionId": task_execution_id,
        "evaluationRef": ref("Evaluation", name),
        "target": target or {"type": "ToolInvocation", "id": DOCKER_INVOCATION_ID},
        "status": status,
        "outcome": outcome,
        "evidence": {"name": name},
        "evidenceAddress": "sha256:" + ("1" if name == "build" else "2") * 64,
    }


def artifact_metadata(
    artifact_id: str,
    task_execution_id: str,
    artifact_type: str,
    evaluation_ids: tuple[str, ...],
) -> dict:
    return {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "GeneratedArtifact",
        "id": artifact_id,
        "traceId": TRACE_ID,
        "createdAt": TIMESTAMP,
        "updatedAt": TIMESTAMP,
        "provenance": {
            "actor": "test-task-handler",
            "workflowExecutionId": WORKFLOW_ID,
            "taskExecutionId": task_execution_id,
            "repositoryRevision": REVISION,
            "resourceRefs": [],
        },
        "taskExecutionId": task_execution_id,
        "artifactType": artifact_type,
        "repositoryRevision": REVISION,
        "mediaType": "application/json",
        "evaluationResultIds": list(evaluation_ids),
    }


def predecessor_execution(
    execution_id: str,
    task_name: str,
    dependencies: tuple[str, ...],
    artifact_id: str,
    evaluation_ids: tuple[str, ...],
    *,
    revision: str = REVISION,
) -> dict:
    task_ref = ref("Task", task_name)
    return {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "TaskExecution",
        "id": execution_id,
        "traceId": TRACE_ID,
        "createdAt": TIMESTAMP,
        "updatedAt": TIMESTAMP,
        "provenance": {
            "actor": "workflow-scheduler",
            "workflowExecutionId": WORKFLOW_ID,
            "repositoryRevision": revision,
            "resourceRefs": [task_ref],
        },
        "workflowExecutionId": WORKFLOW_ID,
        "taskRef": task_ref,
        "attempt": 1,
        "status": "SUCCEEDED",
        "dependencyTaskExecutionIds": list(dependencies),
        "generatedArtifactIds": [artifact_id],
        "evaluationResultIds": list(evaluation_ids),
    }


def target_runtime(runtime_id: str, kind: str, task_execution_id: str) -> dict:
    value = {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": kind,
        "id": runtime_id,
        "traceId": TRACE_ID,
        "createdAt": TIMESTAMP,
        "updatedAt": TIMESTAMP,
        "provenance": {
            "actor": "test",
            "workflowExecutionId": WORKFLOW_ID,
            "taskExecutionId": task_execution_id,
            "resourceRefs": [],
        },
        "taskExecutionId": task_execution_id,
        "status": "SUCCEEDED",
    }
    if kind == "AgentInvocation":
        value.update(
            {
                "resolvedAgentId": "resolvedagent-111111111111",
                "contextPackageId": "contextpackage-111111111111",
            }
        )
    if kind == "ToolInvocation":
        tool_ref = ref("Tool", "docker-validation")
        value["toolRef"] = tool_ref
        value["input"] = {"commands": []}
        value["provenance"].update(
            {
                "actor": "tool-runtime",
                "caller": f"TaskExecution:{task_execution_id}",
                "resourceRefs": [tool_ref],
            }
        )
    return value


def evidence_task(name: str, evaluation_name: str) -> Resource:
    return resource(
        "Task",
        name,
        {
            "objective": f"Run {name}.",
            "outputs": {"type": "object"},
            "evaluations": [ref("Evaluation", evaluation_name)],
        },
    )


def acceptance_summary(store: InMemoryRuntimeObjectStore):
    return store.get(store.get(ACCEPTANCE_ID)["evaluationResultIds"][0])


def ref(kind: str, name: str) -> dict[str, str]:
    return {"kind": kind, "name": name, "version": "1.0.0"}


def resource(kind: str, name: str, spec: dict) -> Resource:
    return Resource(
        ResourceRef(kind, name, "1.0.0"),
        Path(f"{name}.yaml"),
        {
            "apiVersion": "aep.dev/v1alpha1",
            "kind": kind,
            "metadata": {"name": name, "version": "1.0.0"},
            "spec": deepcopy(spec),
        },
        (),
    )
