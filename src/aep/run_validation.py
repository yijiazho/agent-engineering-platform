"""Deterministic RunValidation Task handler."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from hashlib import sha256
from typing import Any

from aep.analyze_issue import _correlation, _ref_record, _required_ref, _spec
from aep.build_test_evaluation import (
    BuildTestEvaluationContractError,
    ValidationExpectation,
    evaluate_build_and_test,
)
from aep.docker_validation_tool import (
    DOCKER_RUN_CAPABILITY,
    DockerValidationTool,
)
from aep.generated_artifact_store import (
    GeneratedArtifactStore,
    GeneratedArtifactStoreError,
)
from aep.resource_loader import Resource, ResourceCollection, ResourceRef
from aep.runtime_store import RuntimeObject, RuntimeObjectStore
from aep.runtime_validation import is_rfc3339_timestamp
from aep.task_execution import FailureClass
from aep.tool_runtime import (
    AuthorizationHook,
    ToolCaller,
    ToolFailureClass,
    ToolRequest,
    ToolResult,
    ToolResultStatus,
)
from aep.workflow_scheduler import TaskExecutionResult


JsonMapping = Mapping[str, Any]
Clock = Callable[[], str]


class RunValidationContractError(ValueError):
    """Raised when validation cannot be bound to immutable Task inputs."""


class RunValidationTaskHandler:
    """Execute configured build and test commands and persist all evidence."""

    task_name = "run-validation"
    runtime_id_namespace = "run-validation"

    def __init__(
        self,
        *,
        resources: ResourceCollection,
        runtime_store: RuntimeObjectStore,
        artifact_store: GeneratedArtifactStore,
        docker_tool: DockerValidationTool,
        authorize_docker: AuthorizationHook,
        clock: Clock,
    ) -> None:
        if not isinstance(resources, ResourceCollection):
            raise TypeError("resources must be a ResourceCollection")
        if not isinstance(artifact_store, GeneratedArtifactStore):
            raise TypeError("artifact_store must implement GeneratedArtifactStore")
        if not isinstance(docker_tool, DockerValidationTool):
            raise TypeError("docker_tool must be a DockerValidationTool")
        if not callable(authorize_docker) or not callable(clock):
            raise TypeError("authorize_docker and clock must be callable")
        self._resources = resources
        self._runtime_store = runtime_store
        self._artifact_store = artifact_store
        self._docker_tool = docker_tool
        self._authorize_docker = authorize_docker
        self._clock = clock

    def execute(
        self, task: Resource, task_execution: RuntimeObject
    ) -> TaskExecutionResult:
        try:
            workflow = self._validate_inputs(task, task_execution)
            patch = self._generated_patch(task_execution, workflow)
            configuration = self._configuration(task, task_execution)
            docker_ref = _ref_record(configuration["tool"].ref)
            build_evaluation, test_evaluation = self._evaluations(
                task, docker_ref
            )

            request = ToolRequest(
                tool_ref=docker_ref,
                input=configuration["input"],
                caller=ToolCaller(
                    kind="TaskExecution", id=str(task_execution["id"])
                ),
                capabilities=(DOCKER_RUN_CAPABILITY,),
                timeout_ms=configuration["timeoutMs"],
                correlation=_correlation(task_execution),
            )
            invocation_id = self._runtime_id(
                "toolinvocation", str(task_execution["id"])
            )
            result, invocation = self._docker_tool.invoke(
                invocation_id=invocation_id,
                task_execution_id=str(task_execution["id"]),
                request=request,
                authorize=self._authorize_docker,
            )
            self._attach(
                task_execution["id"], {"toolInvocationIds": [invocation_id]}
            )

            # An unavailable or incompatible pinned image is a deployment
            # configuration defect, not evidence that the candidate patch failed.
            # Do not manufacture build/test EvaluationResults for commands that
            # were deliberately never started.
            if result.failure_class is ToolFailureClass.CONFIGURATION:
                return _tool_failure(result)

            build_id = self._runtime_id(
                "evaluationresult", f"{task_execution['id']}:build"
            )
            test_id = self._runtime_id(
                "evaluationresult", f"{task_execution['id']}:test"
            )
            build_result, test_result = evaluate_build_and_test(
                store=self._runtime_store,
                build_result_id=build_id,
                test_result_id=test_id,
                task_execution_id=str(task_execution["id"]),
                tool_invocation=invocation,
                docker_tool_ref=docker_ref,
                build_expectation=ValidationExpectation(
                    _ref_record(build_evaluation.ref),
                    configuration["commandIndexes"]["build"],
                ),
                test_expectation=ValidationExpectation(
                    _ref_record(test_evaluation.ref),
                    configuration["commandIndexes"]["test"],
                ),
                correlation=_correlation(task_execution),
                timestamp=self._timestamp(),
                provenance={
                    "actor": "build-test-evaluator",
                    "workflowExecutionId": workflow["id"],
                    "taskExecutionId": task_execution["id"],
                    "repositoryRevision": workflow["repositoryRevision"],
                    "resourceRefs": [
                        _ref_record(build_evaluation.ref),
                        _ref_record(test_evaluation.ref),
                        docker_ref,
                    ],
                },
            )
            self._attach(
                task_execution["id"],
                {"evaluationResultIds": [build_id, test_id]},
            )

            report = _validation_report(
                invocation_id=invocation_id,
                result=result,
                build_result=build_result,
                test_result=test_result,
            )
            artifact_id = self._runtime_id(
                "generatedartifact", str(task_execution["id"])
            )
            artifact = self._artifact_store.publish(
                {
                    "apiVersion": "aep.dev/v1alpha1",
                    "kind": "GeneratedArtifact",
                    "id": artifact_id,
                    "traceId": task_execution["traceId"],
                    "createdAt": self._timestamp(),
                    "updatedAt": self._timestamp(),
                    "provenance": {
                        "actor": "run-validation-task-handler",
                        "workflowExecutionId": workflow["id"],
                        "taskExecutionId": task_execution["id"],
                        "repositoryRevision": workflow["repositoryRevision"],
                        "resourceRefs": [
                            _ref_record(task.ref),
                            docker_ref,
                            _ref_record(build_evaluation.ref),
                            _ref_record(test_evaluation.ref),
                        ],
                        "inputArtifactRefs": [
                            {
                                "generatedArtifactId": patch["id"],
                                "contentAddress": patch["contentAddress"],
                            }
                        ],
                    },
                    "taskExecutionId": task_execution["id"],
                    "artifactType": "EVALUATION_REPORT",
                    "repositoryRevision": workflow["repositoryRevision"],
                    "mediaType": "application/json",
                    "evaluationResultIds": [build_id, test_id],
                },
                report,
            )
            self._attach(
                task_execution["id"],
                {"generatedArtifactIds": [artifact["id"]]},
            )

            if result.status is not ToolResultStatus.SUCCEEDED:
                return _tool_failure(result)
            if build_result["status"] != "SUCCEEDED" or test_result["status"] != "SUCCEEDED":
                return TaskExecutionResult.failure(
                    FailureClass.CONFIGURATION,
                    "RunValidation produced invalid build or test evidence",
                )
            if build_result["outcome"] != "PASS" or test_result["outcome"] != "PASS":
                return TaskExecutionResult.failure(
                    FailureClass.EVALUATION,
                    "RunValidation build or test Evaluation failed",
                )
            return TaskExecutionResult.success()
        except (
            BuildTestEvaluationContractError,
            GeneratedArtifactStoreError,
            RunValidationContractError,
            KeyError,
            TypeError,
            ValueError,
        ) as error:
            return TaskExecutionResult.failure(FailureClass.CONFIGURATION, str(error))

    def _validate_inputs(
        self, task: Resource, task_execution: RuntimeObject
    ) -> RuntimeObject:
        if not isinstance(task, Resource) or task.kind != "Task":
            raise RunValidationContractError("task must be a loaded Task Resource")
        if task.name != self.task_name:
            raise RunValidationContractError(
                "RunValidation handler requires Task run-validation"
            )
        if dict(task_execution.get("taskRef", {})) != _ref_record(task.ref):
            raise RunValidationContractError(
                "TaskExecution.taskRef does not match Task"
            )
        if (
            task_execution.get("kind") != "TaskExecution"
            or task_execution.get("status") != "RUNNING"
        ):
            raise RunValidationContractError("TaskExecution must be RUNNING")
        workflow = self._runtime_store.get(
            str(task_execution.get("workflowExecutionId"))
        )
        if workflow is None or workflow.get("kind") != "WorkflowExecution":
            raise RunValidationContractError("WorkflowExecution was not found")
        return workflow

    def _generated_patch(
        self, task_execution: JsonMapping, workflow: JsonMapping
    ) -> JsonMapping:
        dependencies = task_execution.get("dependencyTaskExecutionIds")
        if (
            isinstance(dependencies, (str, bytes))
            or not isinstance(dependencies, Sequence)
            or len(dependencies) != 1
            or not isinstance(dependencies[0], str)
        ):
            raise RunValidationContractError(
                "RunValidation requires exactly one dependency TaskExecution"
            )
        producer_id = dependencies[0]
        producer = self._runtime_store.get(producer_id)
        producer_ref = producer.get("taskRef") if producer is not None else None
        if not (
            isinstance(producer, Mapping)
            and producer.get("status") == "SUCCEEDED"
            and producer.get("workflowExecutionId") == workflow.get("id")
            and producer.get("traceId") == workflow.get("traceId")
            and producer.get("traceId") == task_execution.get("traceId")
            and isinstance(producer_ref, Mapping)
            and producer_ref.get("kind") == "Task"
            and producer_ref.get("name") == "generate-patch"
            and producer_ref.get("version") not in {None, "", "latest"}
        ):
            raise RunValidationContractError(
                "RunValidation dependency must be a successful versioned generate-patch Task"
            )
        artifacts = self._artifact_store.list_by_task_execution(producer_id)
        patches = [item for item in artifacts if item.get("artifactType") == "PATCH"]
        if len(artifacts) != 1 or len(patches) != 1:
            raise RunValidationContractError(
                "RunValidation requires exactly one prior PATCH GeneratedArtifact"
            )
        patch = patches[0]
        if list(producer.get("generatedArtifactIds", ())) != [patch.get("id")]:
            raise RunValidationContractError(
                "prior PATCH is not attached to its producer TaskExecution"
            )
        if patch.get("repositoryRevision") != workflow.get("repositoryRevision"):
            raise RunValidationContractError(
                "prior PATCH repository revision does not match WorkflowExecution"
            )
        if (
            patch.get("taskExecutionId") != producer_id
            or patch.get("traceId") != producer.get("traceId")
            or patch.get("traceId") != task_execution.get("traceId")
        ):
            raise RunValidationContractError(
                "prior PATCH identity does not match its producer TaskExecution"
            )
        evaluation_ids = patch.get("evaluationResultIds")
        if (
            isinstance(evaluation_ids, (str, bytes))
            or not isinstance(evaluation_ids, Sequence)
            or len(evaluation_ids) != 1
            or evaluation_ids[0] not in producer.get("evaluationResultIds", ())
        ):
            raise RunValidationContractError(
                "prior PATCH must reference its producer EvaluationResult"
            )
        evaluation = self._runtime_store.get(str(evaluation_ids[0]))
        target = evaluation.get("target") if isinstance(evaluation, Mapping) else None
        provenance = (
            evaluation.get("provenance")
            if isinstance(evaluation, Mapping)
            else None
        )
        patch_provenance = patch.get("provenance")
        if not (
            isinstance(evaluation, Mapping)
            and evaluation.get("kind") == "EvaluationResult"
            and evaluation.get("status") == "SUCCEEDED"
            and evaluation.get("outcome") == "PASS"
            and evaluation.get("taskExecutionId") == producer_id
            and evaluation.get("traceId") == producer.get("traceId")
            and evaluation.get("traceId") == task_execution.get("traceId")
            and isinstance(target, Mapping)
            and dict(target)
            == {"type": "GeneratedArtifact", "id": patch.get("id")}
            and isinstance(provenance, Mapping)
            and provenance.get("workflowExecutionId") == workflow.get("id")
            and provenance.get("taskExecutionId") == producer_id
            and provenance.get("repositoryRevision")
            == workflow.get("repositoryRevision")
            and isinstance(patch_provenance, Mapping)
            and patch_provenance.get("workflowExecutionId") == workflow.get("id")
            and patch_provenance.get("taskExecutionId") == producer_id
            and patch_provenance.get("repositoryRevision")
            == workflow.get("repositoryRevision")
        ):
            raise RunValidationContractError(
                "prior PATCH does not have a correlated PASS EvaluationResult"
            )
        return patch

    def _configuration(
        self, task: Resource, task_execution: JsonMapping
    ) -> dict[str, Any]:
        validation = _spec(task).get("validation")
        if not isinstance(validation, Mapping):
            raise RunValidationContractError(
                "RunValidation Task requires spec.validation"
            )
        tool_ref = _required_ref(
            validation.get("toolRef"), "Tool", "Task.spec.validation.toolRef"
        )
        tool = self._require_resource(tool_ref, "Tool")
        tool_spec = _spec(tool)
        if DOCKER_RUN_CAPABILITY not in tool_spec.get("capabilities", ()):
            raise RunValidationContractError(
                "RunValidation Docker Tool must declare docker.run"
            )
        commands = validation.get("commands")
        required_executables = validation.get("requiredExecutables")
        if (
            isinstance(required_executables, (str, bytes))
            or not isinstance(required_executables, Sequence)
            or not required_executables
            or any(
                not isinstance(item, Mapping)
                or not isinstance(item.get("argv"), Sequence)
                or isinstance(item.get("argv"), (str, bytes))
                or not item.get("argv")
                or not isinstance(item.get("versionPattern"), str)
                or not item["versionPattern"]
                for item in required_executables
            )
        ):
            raise RunValidationContractError(
                "RunValidation requires declared image readiness executables"
            )
        if (
            isinstance(commands, (str, bytes))
            or not isinstance(commands, Sequence)
            or len(commands) != 2
        ):
            raise RunValidationContractError(
                "RunValidation requires exactly one build and one test command"
            )
        indexes: dict[str, int] = {}
        docker_commands: list[dict[str, Any]] = []
        for index, command in enumerate(commands):
            if not isinstance(command, Mapping) or command.get("type") not in {
                "build",
                "test",
            }:
                raise RunValidationContractError(
                    "RunValidation commands must be labeled build or test"
                )
            command_type = str(command["type"])
            if command_type in indexes:
                raise RunValidationContractError(
                    f"RunValidation repeats {command_type} command"
                )
            indexes[command_type] = index
            docker_commands.append({"argv": deepcopy(command.get("argv"))})
        if set(indexes) != {"build", "test"}:
            raise RunValidationContractError(
                "RunValidation requires one build and one test command"
            )
        workspace_path = task_execution.get("workspacePath")
        if not isinstance(workspace_path, str) or not workspace_path:
            raise RunValidationContractError(
                "TaskExecution.workspacePath must identify the execution checkout"
            )
        mount = validation.get("workspaceMount")
        resources = validation.get("resources")
        timeout_ms = validation.get("timeoutMs")
        if not isinstance(mount, Mapping) or not isinstance(resources, Mapping):
            raise RunValidationContractError(
                "RunValidation mount and resources must be configured"
            )
        if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) or timeout_ms < 1:
            raise RunValidationContractError(
                "RunValidation timeoutMs must be a positive integer"
            )
        return {
            "tool": tool,
            "timeoutMs": timeout_ms,
            "commandIndexes": indexes,
            "input": {
                "image": validation.get("image"),
                "requiredExecutables": deepcopy(validation.get("requiredExecutables")),
                "commands": docker_commands,
                "workspaceMount": {
                    "hostPath": workspace_path,
                    "containerPath": mount.get("containerPath"),
                    "readOnly": mount.get("readOnly"),
                },
                "resources": deepcopy(dict(resources)),
            },
        }

    def _evaluations(
        self, task: Resource, docker_ref: JsonMapping
    ) -> tuple[Resource, Resource]:
        values = _spec(task).get("evaluations", ())
        if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
            raise RunValidationContractError(
                "Task.spec.evaluations must be an array"
            )
        evaluations = tuple(
            self._require_resource(
                _required_ref(value, "Evaluation", "Task.spec.evaluations"),
                "Evaluation",
            )
            for value in values
        )
        builds = [item for item in evaluations if _spec(item).get("type") == "build"]
        tests = [item for item in evaluations if _spec(item).get("type") == "test"]
        if len(evaluations) != 2 or len(builds) != 1 or len(tests) != 1:
            raise RunValidationContractError(
                "RunValidation requires exactly one build and one test Evaluation"
            )
        for evaluation in evaluations:
            configured_ref = _required_ref(
                _spec(evaluation).get("toolRef"),
                "Tool",
                "Evaluation.spec.toolRef",
            )
            if _ref_record(configured_ref) != dict(docker_ref):
                raise RunValidationContractError(
                    "RunValidation Evaluations must reference the configured Docker Tool"
                )
        return builds[0], tests[0]

    def _require_resource(self, ref: ResourceRef, expected_kind: str) -> Resource:
        if ref.kind != expected_kind or ref.version == "latest":
            raise RunValidationContractError(
                f"expected explicit {expected_kind} reference"
            )
        resource = self._resources.get(ref)
        if resource is None:
            raise RunValidationContractError(
                f"missing Resource {_ref_record(ref)!r}"
            )
        return resource

    def _attach(self, task_execution_id: object, changes: JsonMapping) -> None:
        execution_id = str(task_execution_id)
        current = self._runtime_store.get(execution_id)
        if current is None or current.get("status") != "RUNNING":
            raise RunValidationContractError("TaskExecution is no longer RUNNING")
        merged: dict[str, Any] = {}
        for field, values in changes.items():
            prior = current.get(field, ())
            merged[field] = list(dict.fromkeys([*prior, *values]))
        self._runtime_store.update_status(
            execution_id,
            "RUNNING",
            expected_status="RUNNING",
            updated_at=self._timestamp(),
            changes=merged,
        )

    def _timestamp(self) -> str:
        value = self._clock()
        if not isinstance(value, str) or not is_rfc3339_timestamp(value):
            raise RunValidationContractError(
                "clock must return an RFC3339 timestamp"
            )
        return value

    def _runtime_id(self, prefix: str, discriminator: str) -> str:
        digest = sha256(
            f"{self.runtime_id_namespace}:{discriminator}:{prefix}".encode()
        ).hexdigest()[:24]
        return f"{prefix}-{digest}"


def _validation_report(
    *,
    invocation_id: str,
    result: ToolResult,
    build_result: JsonMapping,
    test_result: JsonMapping,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "status": (
            "PASSED"
            if result.status is ToolResultStatus.SUCCEEDED
            and build_result.get("outcome") == "PASS"
            and test_result.get("outcome") == "PASS"
            else "FAILED"
        ),
        "toolInvocationId": invocation_id,
        "toolResultStatus": result.status.value,
        "build": _evaluation_summary(build_result),
        "test": _evaluation_summary(test_result),
    }
    if result.failure_class is not None:
        report["toolFailure"] = {
            "class": result.failure_class.value,
            "message": result.failure_message or result.failure_class.value,
        }
    return report


def _evaluation_summary(result: JsonMapping) -> dict[str, Any]:
    return {
        "evaluationResultId": result["id"],
        "status": result["status"],
        "outcome": result["outcome"],
        "evidence": deepcopy(dict(result["evidence"])),
    }


def _tool_failure(result: ToolResult) -> TaskExecutionResult:
    failure = result.failure_class or ToolFailureClass.ADAPTER
    classification = {
        ToolFailureClass.VALIDATION: FailureClass.CONFIGURATION,
        ToolFailureClass.POLICY: FailureClass.POLICY,
        ToolFailureClass.TIMEOUT: FailureClass.RECOVERABLE,
        ToolFailureClass.ADAPTER: FailureClass.PERMANENT,
        ToolFailureClass.STARTUP: FailureClass.RECOVERABLE,
        ToolFailureClass.NONZERO_EXIT: FailureClass.EVALUATION,
        ToolFailureClass.BOUNDARY: FailureClass.POLICY,
        ToolFailureClass.NOT_FOUND: FailureClass.PERMANENT,
        ToolFailureClass.IO: FailureClass.RECOVERABLE,
        ToolFailureClass.CONFIGURATION: FailureClass.CONFIGURATION,
    }[failure]
    return TaskExecutionResult.failure(
        classification,
        "Docker validation failed: "
        f"{result.failure_message or result.status.value}",
    )
