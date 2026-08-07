from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from threading import Event, Thread
from time import sleep

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource as SchemaResource
from referencing.jsonschema import DRAFT202012

from aep.docker_validation_tool import (
    DOCKER_VALIDATION_INPUT_SCHEMA,
    DOCKER_VALIDATION_OUTPUT_SCHEMA,
    DockerCommandResult,
    DockerExecution,
    DockerExecutionResult,
    DockerExecutor,
    DockerTimeoutResult,
    DockerValidationAdapter,
    DockerValidationTool,
    _pending_invocation_record,
    _request_fingerprint,
)
from aep.analyze_issue import _correlation, _ref_record
from aep.generated_artifact_store import InMemoryGeneratedArtifactStore
from aep.resource_loader import Resource, ResourceCollection, ResourceRef
from aep.run_validation import RunValidationTaskHandler
from aep.runtime_store import InMemoryRuntimeObjectStore
from aep.task_execution import FailureClass
from aep.tool_runtime import ToolCaller, ToolRequest


TIMESTAMP = "2026-08-06T12:00:00Z"
REVISION = "a" * 40
WORKFLOW_ID = "workflowexecution-aaaaaaaaaaaa"
PRODUCER_ID = "taskexecution-bbbbbbbbbbbb"
TASK_EXECUTION_ID = "taskexecution-cccccccccccc"
PATCH_ARTIFACT_ID = "generatedartifact-dddddddddddd"
PATCH_EVALUATION_ID = "evaluationresult-eeeeeeeeeeee"
TOOL_REF = {"kind": "Tool", "name": "docker-validation", "version": "1.0.0"}
ROOT = Path(__file__).parents[1]


class FakeExecution(DockerExecution):
    def __init__(self, outcome) -> None:
        self.outcome = outcome
        self.waits = 0

    def wait(self, timeout_ms: int):
        self.waits += 1
        return self.outcome

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass

    def cleanup(self) -> None:
        pass


class FakeExecutor(DockerExecutor):
    def __init__(self, outcome) -> None:
        self.execution = FakeExecution(outcome)
        self.configurations = []

    def start(self, configuration):
        self.configurations.append(configuration)
        return self.execution

    def cleanup_startup(self) -> None:
        pass


def test_run_validation_task_fixture_satisfies_resource_contract() -> None:
    fixture = run_validation_fixture()

    task_validator().validate(fixture)


@pytest.mark.parametrize("command_types", [["build", "build"], ["build", "test", "test"]])
def test_run_validation_task_schema_rejects_duplicate_or_extra_commands(
    command_types: list[str],
) -> None:
    fixture = run_validation_fixture()
    template = fixture["spec"]["validation"]["commands"]
    fixture["spec"]["validation"]["commands"] = [
        {"type": value, "argv": template[0]["argv"]} for value in command_types
    ]

    assert list(task_validator().iter_errors(fixture))


def task_validator() -> Draft202012Validator:
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
    return Draft202012Validator(schemas[-1], registry=registry)


def run_validation_fixture() -> dict:
    return json.loads(
        (ROOT / "fixtures" / "resources" / "valid" / "run-validation-task.json").read_text(
            encoding="utf-8"
        )
    )


def command(argv: tuple[str, ...], exit_code: int = 0) -> DockerCommandResult:
    return DockerCommandResult(
        argv=argv,
        stdout="ok\n" if exit_code == 0 else "",
        stderr="" if exit_code == 0 else "failed\n",
        exit_code=exit_code,
        duration_ms=10,
        logs_ref="sha256:" + ("b" if exit_code == 0 else "c") * 64,
    )


def completed(*commands: DockerCommandResult) -> DockerExecutionResult:
    return DockerExecutionResult(
        commands=commands,
        logs_ref="sha256:" + "d" * 64,
        started_at=TIMESTAMP,
        completed_at=TIMESTAMP,
    )


def timed_out(*commands: DockerCommandResult) -> DockerTimeoutResult:
    return DockerTimeoutResult(
        commands=commands,
        logs_ref="sha256:" + "e" * 64,
        started_at=TIMESTAMP,
        completed_at=TIMESTAMP,
    )


def test_pass_persists_tool_evaluations_and_validation_report(tmp_path: Path) -> None:
    store, handler, task, artifacts, executor = setup_handler(
        tmp_path,
        completed(
            command(("python", "-m", "build")),
            command(("python", "-m", "pytest")),
        ),
    )

    result = handler.execute(task, store.get(TASK_EXECUTION_ID))

    assert result.succeeded is True
    execution = store.get(TASK_EXECUTION_ID)
    invocation = store.get(execution["toolInvocationIds"][0])
    assert invocation["status"] == "SUCCEEDED"
    assert invocation["resultStatus"] == "SUCCEEDED"
    assert invocation["input"]["workspaceMount"]["hostPath"] == str(tmp_path)
    assert len(execution["evaluationResultIds"]) == 2
    assert [store.get(value)["outcome"] for value in execution["evaluationResultIds"]] == [
        "PASS",
        "PASS",
    ]
    artifact = artifacts.get(execution["generatedArtifactIds"][0])
    assert artifact["artifactType"] == "EVALUATION_REPORT"
    report = artifacts.get_content(artifact["id"]).decode("utf-8")
    assert '"status":"PASSED"' in report
    assert executor.configurations[0].timeout_ms == 30_000


def test_nonzero_build_persists_both_results_and_fails_evaluation(tmp_path: Path) -> None:
    store, handler, task, artifacts, _executor = setup_handler(
        tmp_path,
        completed(command(("python", "-m", "build"), 2)),
    )

    result = handler.execute(task, store.get(TASK_EXECUTION_ID))

    assert result.succeeded is False
    assert result.failure_class is FailureClass.EVALUATION
    execution = store.get(TASK_EXECUTION_ID)
    build, test = [store.get(value) for value in execution["evaluationResultIds"]]
    assert build["evidence"]["commandStatus"] == "FAILED"
    assert test["evidence"]["commandStatus"] == "NOT_RUN"
    assert artifacts.list_by_task_execution(TASK_EXECUTION_ID)[0]["artifactType"] == "EVALUATION_REPORT"


def test_nonzero_test_preserves_passing_build(tmp_path: Path) -> None:
    store, handler, task, _artifacts, _executor = setup_handler(
        tmp_path,
        completed(
            command(("python", "-m", "build")),
            command(("python", "-m", "pytest"), 7),
        ),
    )

    result = handler.execute(task, store.get(TASK_EXECUTION_ID))

    assert result.failure_class is FailureClass.EVALUATION
    build, test = [
        store.get(value)
        for value in store.get(TASK_EXECUTION_ID)["evaluationResultIds"]
    ]
    assert build["outcome"] == "PASS"
    assert test["outcome"] == "FAIL"


def test_timeout_is_recoverable_and_preserves_completed_build(tmp_path: Path) -> None:
    store, handler, task, artifacts, _executor = setup_handler(
        tmp_path,
        timed_out(command(("python", "-m", "build"))),
    )

    result = handler.execute(task, store.get(TASK_EXECUTION_ID))

    assert result.failure_class is FailureClass.RECOVERABLE
    invocation = store.get(store.get(TASK_EXECUTION_ID)["toolInvocationIds"][0])
    assert invocation["resultStatus"] == "TIMED_OUT"
    build, test = [
        store.get(value)
        for value in store.get(TASK_EXECUTION_ID)["evaluationResultIds"]
    ]
    assert build["outcome"] == "PASS"
    assert test["evidence"]["commandStatus"] == "TIMED_OUT"
    assert artifacts.list_by_task_execution(TASK_EXECUTION_ID)


def test_denial_is_policy_failure_with_complete_evidence(tmp_path: Path) -> None:
    store, handler, task, artifacts, executor = setup_handler(
        tmp_path,
        completed(
            command(("python", "-m", "build")),
            command(("python", "-m", "pytest")),
        ),
        authorize=lambda _request: False,
    )

    result = handler.execute(task, store.get(TASK_EXECUTION_ID))

    assert result.failure_class is FailureClass.POLICY
    invocation = store.get(store.get(TASK_EXECUTION_ID)["toolInvocationIds"][0])
    assert invocation["resultStatus"] == "DENIED"
    assert executor.configurations == []
    assert len(store.get(TASK_EXECUTION_ID)["evaluationResultIds"]) == 2
    assert artifacts.list_by_task_execution(TASK_EXECUTION_ID)


def test_malformed_executor_evidence_is_classified_and_reported(tmp_path: Path) -> None:
    store, handler, task, artifacts, _executor = setup_handler(
        tmp_path,
        completed(command(("unexpected", "command"))),
    )

    result = handler.execute(task, store.get(TASK_EXECUTION_ID))

    assert result.failure_class is FailureClass.PERMANENT
    invocation = store.get(store.get(TASK_EXECUTION_ID)["toolInvocationIds"][0])
    assert invocation["failureClass"] == "ADAPTER"
    assert len(store.get(TASK_EXECUTION_ID)["evaluationResultIds"]) == 2
    assert artifacts.list_by_task_execution(TASK_EXECUTION_ID)


def test_retry_reuses_terminal_docker_invocation_and_artifact(tmp_path: Path) -> None:
    store, handler, task, artifacts, executor = setup_handler(
        tmp_path,
        completed(
            command(("python", "-m", "build")),
            command(("python", "-m", "pytest")),
        ),
    )

    first = handler.execute(task, store.get(TASK_EXECUTION_ID))
    first_execution = deepcopy(dict(store.get(TASK_EXECUTION_ID)))
    second = handler.execute(task, store.get(TASK_EXECUTION_ID))

    assert first.succeeded is True
    assert second.succeeded is True
    assert executor.execution.waits == 1
    assert store.get(TASK_EXECUTION_ID)["toolInvocationIds"] == first_execution["toolInvocationIds"]
    assert len(artifacts.list_by_task_execution(TASK_EXECUTION_ID)) == 1


def test_matching_concurrent_replay_waits_for_long_running_owner(tmp_path: Path) -> None:
    outcome = completed(
        command(("python", "-m", "build")),
        command(("python", "-m", "pytest")),
    )
    store, handler, task, artifacts, executor = setup_handler(tmp_path, outcome)
    entered = Event()
    release = Event()

    class SlowExecution(FakeExecution):
        def wait(self, timeout_ms: int):
            self.waits += 1
            entered.set()
            assert release.wait(5)
            return self.outcome

    executor.execution = SlowExecution(outcome)
    results = []
    errors = []

    def execute() -> None:
        try:
            results.append(handler.execute(task, store.get(TASK_EXECUTION_ID)))
        except Exception as error:  # pragma: no cover - assertion reports details
            errors.append(error)

    owner = Thread(target=execute)
    owner.start()
    assert entered.wait(1)
    duplicate = Thread(target=execute)
    duplicate.start()
    sleep(1.1)
    release.set()
    owner.join(5)
    duplicate.join(5)

    assert errors == []
    assert len(results) == 2
    assert all(result.succeeded for result in results)
    assert executor.execution.waits == 1
    assert len(artifacts.list_by_task_execution(TASK_EXECUTION_ID)) == 1


def test_abandoned_invocation_becomes_recoverable_terminal_evidence(
    tmp_path: Path,
) -> None:
    outcome = completed(
        command(("python", "-m", "build")),
        command(("python", "-m", "pytest")),
    )
    store, handler, task, artifacts, executor = setup_handler(
        tmp_path,
        outcome,
        timeout_ms=1,
        replay_grace_ms=1,
    )
    task_execution_value = store.get(TASK_EXECUTION_ID)
    configuration = handler._configuration(task, task_execution_value)
    request = ToolRequest(
        tool_ref=_ref_record(configuration["tool"].ref),
        input=configuration["input"],
        caller=ToolCaller(kind="TaskExecution", id=TASK_EXECUTION_ID),
        capabilities=("docker.run",),
        timeout_ms=1,
        correlation=_correlation(task_execution_value),
    )
    invocation_id = handler._runtime_id("toolinvocation", TASK_EXECUTION_ID)
    fingerprint = _request_fingerprint(TASK_EXECUTION_ID, request, None)
    store.create(
        _pending_invocation_record(
            invocation_id,
            TASK_EXECUTION_ID,
            request,
            fingerprint,
            None,
            TIMESTAMP,
            "abandoned-owner",
        ),
        deterministic_key=f"docker-tool-invocation:{invocation_id}",
    )

    result = handler.execute(task, task_execution_value)

    assert result.failure_class is FailureClass.RECOVERABLE
    invocation = store.get(invocation_id)
    assert invocation["status"] == "FAILED"
    assert invocation["resultStatus"] == "TIMED_OUT"
    assert executor.configurations == []
    assert artifacts.list_by_task_execution(TASK_EXECUTION_ID)


@pytest.mark.parametrize(
    ("patch_target_id", "evaluation_workflow_id"),
    [
        ("generatedartifact-ffffffffffff", WORKFLOW_ID),
        (PATCH_ARTIFACT_ID, "workflowexecution-ffffffffffff"),
    ],
)
def test_unrelated_or_cross_workflow_patch_evaluation_is_rejected_before_docker(
    tmp_path: Path,
    patch_target_id: str,
    evaluation_workflow_id: str,
) -> None:
    store, handler, task, artifacts, executor = setup_handler(
        tmp_path,
        completed(
            command(("python", "-m", "build")),
            command(("python", "-m", "pytest")),
        ),
        patch_target_id=patch_target_id,
        evaluation_workflow_id=evaluation_workflow_id,
    )

    result = handler.execute(task, store.get(TASK_EXECUTION_ID))

    assert result.failure_class is FailureClass.CONFIGURATION
    assert "correlated PASS" in result.message
    assert executor.configurations == []
    assert artifacts.list_by_task_execution(TASK_EXECUTION_ID) == ()


@pytest.mark.parametrize(
    ("producer_workflow_id", "patch_trace_id"),
    [
        ("workflowexecution-ffffffffffff", "trace-validation-0001"),
        (WORKFLOW_ID, "trace-unrelated-0001"),
    ],
)
def test_cross_workflow_producer_or_inconsistent_patch_is_rejected_before_docker(
    tmp_path: Path,
    producer_workflow_id: str,
    patch_trace_id: str,
) -> None:
    store, handler, task, _artifacts, executor = setup_handler(
        tmp_path,
        completed(
            command(("python", "-m", "build")),
            command(("python", "-m", "pytest")),
        ),
        producer_workflow_id=producer_workflow_id,
        patch_trace_id=patch_trace_id,
    )

    result = handler.execute(task, store.get(TASK_EXECUTION_ID))

    assert result.failure_class is FailureClass.CONFIGURATION
    assert executor.configurations == []


def setup_handler(
    tmp_path: Path,
    outcome,
    *,
    authorize=lambda _request: True,
    timeout_ms: int = 30_000,
    replay_grace_ms: int = 1_000,
    patch_target_id: str = PATCH_ARTIFACT_ID,
    evaluation_workflow_id: str = WORKFLOW_ID,
    producer_workflow_id: str = WORKFLOW_ID,
    patch_trace_id: str = "trace-validation-0001",
):
    resources, task = resource_collection(timeout_ms=timeout_ms)
    store = InMemoryRuntimeObjectStore()
    store.create(workflow_execution(), deterministic_key="workflow")
    store.create(
        producer_execution(workflow_id=producer_workflow_id),
        deterministic_key="producer",
    )
    store.create(task_execution(tmp_path), deterministic_key="validation")
    store.create(
        patch_evaluation(
            target_id=patch_target_id,
            workflow_id=evaluation_workflow_id,
        ),
        deterministic_key="patch-evaluation",
    )
    artifacts = InMemoryGeneratedArtifactStore(runtime_store=store)
    artifacts.publish(
        patch_metadata(trace_id=patch_trace_id),
        "diff --git a/app.py b/app.py\n",
    )
    executor = FakeExecutor(outcome)
    docker_tool = DockerValidationTool(
        DockerValidationAdapter(executor, tmp_path),
        store,
        replay_grace_ms=replay_grace_ms,
    )
    handler = RunValidationTaskHandler(
        resources=resources,
        runtime_store=store,
        artifact_store=artifacts,
        docker_tool=docker_tool,
        authorize_docker=authorize,
        clock=lambda: TIMESTAMP,
    )
    return store, handler, task, artifacts, executor


def resource_collection(*, timeout_ms: int = 30_000) -> tuple[ResourceCollection, Resource]:
    workspace = resource("Workspace", "workspace", {"repository": {}})
    tool = resource(
        "Tool",
        "docker-validation",
        {
            "category": "execution",
            "capabilities": ["docker.run"],
            "inputSchema": deepcopy(DOCKER_VALIDATION_INPUT_SCHEMA),
            "outputSchema": deepcopy(DOCKER_VALIDATION_OUTPUT_SCHEMA),
        },
    )
    build = resource("Evaluation", "build", {"type": "build", "toolRef": TOOL_REF})
    test = resource("Evaluation", "test", {"type": "test", "toolRef": TOOL_REF})
    task = resource(
        "Task",
        "run-validation",
        {
            "objective": "Build and test the generated checkout.",
            "outputs": {"type": "object"},
            "evaluations": [
                {"kind": "Evaluation", "name": "build", "version": "1.0.0"},
                {"kind": "Evaluation", "name": "test", "version": "1.0.0"},
            ],
            "validation": {
                "toolRef": TOOL_REF,
                "image": "python@sha256:" + "a" * 64,
                "commands": [
                    {"type": "build", "argv": ["python", "-m", "build"]},
                    {"type": "test", "argv": ["python", "-m", "pytest"]},
                ],
                "workspaceMount": {
                    "containerPath": "/workspace",
                    "readOnly": False,
                },
                "resources": {"cpuLimit": 2, "memoryBytes": 536_870_912},
                "timeoutMs": timeout_ms,
            },
        },
    )
    return ResourceCollection(workspace, (workspace, task, tool, build, test)), task


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


def workflow_execution() -> dict:
    return {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "WorkflowExecution",
        "id": WORKFLOW_ID,
        "traceId": "trace-validation-0001",
        "createdAt": TIMESTAMP,
        "updatedAt": TIMESTAMP,
        "provenance": {"actor": "test", "resourceRefs": []},
        "repositoryRevision": REVISION,
        "status": "RUNNING",
    }


def producer_execution(*, workflow_id: str = WORKFLOW_ID) -> dict:
    return {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "TaskExecution",
        "id": PRODUCER_ID,
        "traceId": "trace-validation-0001",
        "createdAt": TIMESTAMP,
        "updatedAt": TIMESTAMP,
        "provenance": {"actor": "test", "resourceRefs": []},
        "workflowExecutionId": workflow_id,
        "taskRef": {"kind": "Task", "name": "generate-patch", "version": "1.0.0"},
        "status": "SUCCEEDED",
        "generatedArtifactIds": [PATCH_ARTIFACT_ID],
        "evaluationResultIds": [PATCH_EVALUATION_ID],
    }


def task_execution(workspace: Path) -> dict:
    return {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "TaskExecution",
        "id": TASK_EXECUTION_ID,
        "traceId": "trace-validation-0001",
        "createdAt": TIMESTAMP,
        "updatedAt": TIMESTAMP,
        "provenance": {"actor": "test", "resourceRefs": []},
        "workflowExecutionId": WORKFLOW_ID,
        "taskRef": {"kind": "Task", "name": "run-validation", "version": "1.0.0"},
        "status": "RUNNING",
        "dependencyTaskExecutionIds": [PRODUCER_ID],
        "workspacePath": str(workspace),
    }


def patch_evaluation(
    *,
    target_id: str = PATCH_ARTIFACT_ID,
    workflow_id: str = WORKFLOW_ID,
) -> dict:
    return {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "EvaluationResult",
        "id": PATCH_EVALUATION_ID,
        "traceId": "trace-validation-0001",
        "createdAt": TIMESTAMP,
        "updatedAt": TIMESTAMP,
        "provenance": {
            "actor": "test",
            "workflowExecutionId": workflow_id,
            "taskExecutionId": PRODUCER_ID,
            "repositoryRevision": REVISION,
            "resourceRefs": [],
        },
        "taskExecutionId": PRODUCER_ID,
        "evaluationRef": {"kind": "Evaluation", "name": "patch", "version": "1.0.0"},
        "target": {"type": "GeneratedArtifact", "id": target_id},
        "status": "SUCCEEDED",
        "outcome": "PASS",
    }


def patch_metadata(*, trace_id: str = "trace-validation-0001") -> dict:
    return {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "GeneratedArtifact",
        "id": PATCH_ARTIFACT_ID,
        "traceId": trace_id,
        "createdAt": TIMESTAMP,
        "updatedAt": TIMESTAMP,
        "provenance": {
            "actor": "test",
            "workflowExecutionId": WORKFLOW_ID,
            "taskExecutionId": PRODUCER_ID,
            "repositoryRevision": REVISION,
            "resourceRefs": [],
        },
        "taskExecutionId": PRODUCER_ID,
        "artifactType": "PATCH",
        "repositoryRevision": REVISION,
        "evaluationResultIds": [PATCH_EVALUATION_ID],
    }
