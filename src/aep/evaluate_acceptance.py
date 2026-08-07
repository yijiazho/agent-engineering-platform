"""Deterministic EvaluateAcceptance Task handler."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from functools import cache
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource as SchemaResource
from referencing.jsonschema import DRAFT202012

from aep.analyze_issue import _ref_record, _spec
from aep.generated_artifact_store import (
    GeneratedArtifactStore,
    GeneratedArtifactStoreError,
)
from aep.resource_loader import Resource, ResourceCollection, ResourceRef
from aep.runtime_store import RuntimeObject, RuntimeObjectStore
from aep.runtime_validation import is_rfc3339_timestamp
from aep.task_execution import FailureClass
from aep.workflow_scheduler import TaskExecutionResult


JsonMapping = Mapping[str, Any]
Clock = Callable[[], str]

MVP_EVIDENCE_CONTRACT: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("analyze-issue", "ISSUE_ANALYSIS", ("schema",)),
    ("build-implementation-plan", "IMPLEMENTATION_PLAN", ("schema",)),
    ("generate-patch", "PATCH", ("patch",)),
    ("run-validation", "EVALUATION_REPORT", ("build", "test")),
)


class EvaluateAcceptanceContractError(ValueError):
    """Raised when acceptance cannot be bound to immutable workflow inputs."""


class EvaluateAcceptanceTaskHandler:
    """Aggregate prior workflow evidence without model or policy invocation."""

    task_name = "evaluate-acceptance"
    runtime_id_namespace = "evaluate-acceptance"

    def __init__(
        self,
        *,
        resources: ResourceCollection,
        runtime_store: RuntimeObjectStore,
        artifact_store: GeneratedArtifactStore,
        clock: Clock,
    ) -> None:
        if not isinstance(resources, ResourceCollection):
            raise TypeError("resources must be a ResourceCollection")
        if not isinstance(artifact_store, GeneratedArtifactStore):
            raise TypeError("artifact_store must implement GeneratedArtifactStore")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._resources = resources
        self._runtime_store = runtime_store
        self._artifact_store = artifact_store
        self._clock = clock

    def execute(
        self, task: Resource, task_execution: RuntimeObject
    ) -> TaskExecutionResult:
        try:
            workflow = self._validate_inputs(task, task_execution)
            acceptance_evaluation = self._acceptance_evaluation(task)
            predecessors = self._predecessors(task_execution, workflow)
            timestamp = self._timestamp()
            evidence = self._aggregate(predecessors, workflow, task_execution)
            outcome = "PASS" if not evidence["issues"] else "FAIL"
            result_id = self._runtime_id(
                "evaluationresult", str(task_execution["id"])
            )
            evidence_json = json.dumps(
                evidence, sort_keys=True, separators=(",", ":")
            )
            result: dict[str, Any] = {
                "apiVersion": "aep.dev/v1alpha1",
                "kind": "EvaluationResult",
                "id": result_id,
                "traceId": task_execution["traceId"],
                "createdAt": timestamp,
                "updatedAt": timestamp,
                "provenance": {
                    "actor": "evaluate-acceptance-task-handler",
                    "workflowExecutionId": workflow["id"],
                    "taskExecutionId": task_execution["id"],
                    "repositoryRevision": workflow["repositoryRevision"],
                    "resourceRefs": [
                        _ref_record(task.ref),
                        _ref_record(acceptance_evaluation.ref),
                    ],
                    "inputArtifactRefs": [
                        {
                            "generatedArtifactId": item["id"],
                            "contentAddress": item["contentAddress"],
                        }
                        for item in evidence["artifacts"]
                    ],
                },
                "taskExecutionId": task_execution["id"],
                "evaluationRef": _ref_record(acceptance_evaluation.ref),
                "target": {
                    "type": "TaskExecution",
                    "id": task_execution["id"],
                },
                "status": "SUCCEEDED",
                "outcome": outcome,
                "metrics": {
                    "artifacts": len(evidence["artifacts"]),
                    "evaluations": len(evidence["evaluations"]),
                    "passedEvaluations": sum(
                        item["outcome"] == "PASS"
                        for item in evidence["evaluations"]
                    ),
                    "issues": len(evidence["issues"]),
                },
                "logs": [
                    "Acceptance evidence passed"
                    if outcome == "PASS"
                    else f"Acceptance evidence failed with {len(evidence['issues'])} issue(s)"
                ],
                "evidence": evidence,
                "evidenceAddress": (
                    f"sha256:{sha256(evidence_json.encode()).hexdigest()}"
                ),
                "startedAt": timestamp,
                "completedAt": timestamp,
            }
            _validate_result(result)
            saved = self._runtime_store.create(
                result,
                deterministic_key=f"acceptance-evaluation:{result_id}",
            )
            self._attach(task_execution["id"], result_id)
            if saved.get("outcome") != "PASS":
                return TaskExecutionResult.failure(
                    FailureClass.EVALUATION,
                    "EvaluateAcceptance found incomplete, failed, stale, or inconsistent evidence",
                )
            return TaskExecutionResult.success()
        except (EvaluateAcceptanceContractError, KeyError, TypeError, ValueError) as error:
            return TaskExecutionResult.failure(FailureClass.CONFIGURATION, str(error))

    def _validate_inputs(
        self, task: Resource, task_execution: RuntimeObject
    ) -> RuntimeObject:
        if not isinstance(task, Resource) or task.kind != "Task":
            raise EvaluateAcceptanceContractError(
                "task must be a loaded Task Resource"
            )
        if task.name != self.task_name:
            raise EvaluateAcceptanceContractError(
                "EvaluateAcceptance handler requires Task evaluate-acceptance"
            )
        if not isinstance(task_execution, Mapping):
            raise EvaluateAcceptanceContractError("task_execution must be a mapping")
        if dict(task_execution.get("taskRef", {})) != _ref_record(task.ref):
            raise EvaluateAcceptanceContractError(
                "TaskExecution.taskRef does not match Task"
            )
        if (
            task_execution.get("kind") != "TaskExecution"
            or task_execution.get("status") != "RUNNING"
        ):
            raise EvaluateAcceptanceContractError("TaskExecution must be RUNNING")
        workflow = self._runtime_store.get(
            str(task_execution.get("workflowExecutionId"))
        )
        if workflow is None or workflow.get("kind") != "WorkflowExecution":
            raise EvaluateAcceptanceContractError("WorkflowExecution was not found")
        if (
            workflow.get("status") != "RUNNING"
            or workflow.get("traceId") != task_execution.get("traceId")
            or not isinstance(workflow.get("repositoryRevision"), str)
        ):
            raise EvaluateAcceptanceContractError(
                "WorkflowExecution is not a correlated running execution"
            )
        return workflow

    def _acceptance_evaluation(self, task: Resource) -> Resource:
        values = _spec(task).get("evaluations", ())
        if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
            raise EvaluateAcceptanceContractError(
                "Task.spec.evaluations must be an array"
            )
        resolved: list[Resource] = []
        for value in values:
            ref = _resource_ref(value, "Evaluation", "Task.spec.evaluations")
            resource = self._resources.get(ref)
            if resource is None or resource.kind != "Evaluation":
                raise EvaluateAcceptanceContractError(
                    f"missing Evaluation Resource {_ref_record(ref)!r}"
                )
            resolved.append(resource)
        accepted = [item for item in resolved if _spec(item).get("type") == "acceptance"]
        if len(resolved) != 1 or len(accepted) != 1:
            raise EvaluateAcceptanceContractError(
                "EvaluateAcceptance requires exactly one acceptance Evaluation"
            )
        return accepted[0]

    def _predecessors(
        self, task_execution: JsonMapping, workflow: JsonMapping
    ) -> tuple[RuntimeObject, ...]:
        direct = _string_list(
            task_execution.get("dependencyTaskExecutionIds"),
            "EvaluateAcceptance dependencyTaskExecutionIds",
        )
        if len(direct) != 1:
            raise EvaluateAcceptanceContractError(
                "EvaluateAcceptance requires exactly one dependency TaskExecution"
            )
        ordered: list[RuntimeObject] = []
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(execution_id: str) -> None:
            if execution_id in visiting:
                raise EvaluateAcceptanceContractError(
                    "TaskExecution dependency evidence contains a cycle"
                )
            if execution_id in visited:
                return
            value = self._runtime_store.get(execution_id)
            if value is None or value.get("kind") != "TaskExecution":
                raise EvaluateAcceptanceContractError(
                    f"dependency TaskExecution {execution_id!r} was not found"
                )
            if (
                value.get("workflowExecutionId") != workflow.get("id")
                or value.get("traceId") != workflow.get("traceId")
                or value.get("status") != "SUCCEEDED"
            ):
                raise EvaluateAcceptanceContractError(
                    f"dependency TaskExecution {execution_id!r} is not a correlated success"
                )
            task_ref = _resource_ref(
                value.get("taskRef"), "Task", "dependency TaskExecution.taskRef"
            )
            if self._resources.get(task_ref) is None:
                raise EvaluateAcceptanceContractError(
                    f"dependency Task Resource {_ref_record(task_ref)!r} was not found"
                )
            visiting.add(execution_id)
            for dependency_id in _optional_string_list(
                value.get("dependencyTaskExecutionIds"),
                "dependency TaskExecution.dependencyTaskExecutionIds",
            ):
                visit(dependency_id)
            visiting.remove(execution_id)
            visited.add(execution_id)
            ordered.append(value)

        visit(direct[0])
        actual_names = tuple(item.get("taskRef", {}).get("name") for item in ordered)
        expected_names = tuple(item[0] for item in MVP_EVIDENCE_CONTRACT)
        if actual_names != expected_names:
            raise EvaluateAcceptanceContractError(
                "EvaluateAcceptance requires the complete ordered MVP predecessor chain "
                f"{expected_names!r}"
            )
        return tuple(ordered)

    def _aggregate(
        self,
        predecessors: Sequence[JsonMapping],
        workflow: JsonMapping,
        task_execution: JsonMapping,
    ) -> dict[str, Any]:
        issues: list[dict[str, str]] = []
        artifact_summaries: list[dict[str, Any]] = []
        evaluation_summaries: list[dict[str, Any]] = []
        required_artifact_ids: list[str] = []
        required_evaluation_ids: list[str] = []
        expected_revision = workflow["repositoryRevision"]
        expected_trace = workflow["traceId"]

        for producer, contract in zip(
            predecessors, MVP_EVIDENCE_CONTRACT, strict=True
        ):
            producer_id = str(producer["id"])
            producer_task = self._resources.get(
                _resource_ref(producer["taskRef"], "Task", "TaskExecution.taskRef")
            )
            if producer_task is None:
                raise EvaluateAcceptanceContractError(
                    f"Task Resource for {producer_id!r} was not found"
                )
            self._validate_predecessor(
                producer, producer_task, workflow, issues
            )
            _, expected_artifact_type, expected_evaluation_types = contract

            attached_artifact_ids = _optional_string_list(
                producer.get("generatedArtifactIds"),
                "TaskExecution.generatedArtifactIds",
            )
            required_artifact_ids.extend(attached_artifact_ids)
            stored_artifact_ids = tuple(
                str(item["id"])
                for item in self._artifact_store.list_by_task_execution(producer_id)
            )
            producer_artifacts: list[RuntimeObject] = []
            if not attached_artifact_ids:
                _issue(issues, "MISSING_ARTIFACT", producer_id, "produced no GeneratedArtifact")
            elif len(attached_artifact_ids) != 1:
                _issue(
                    issues,
                    "INCONSISTENT_ARTIFACT_ATTACHMENTS",
                    producer_id,
                    "MVP predecessor must produce exactly one GeneratedArtifact",
                )
            if set(attached_artifact_ids) != set(stored_artifact_ids):
                _issue(
                    issues,
                    "INCONSISTENT_ARTIFACT_ATTACHMENTS",
                    producer_id,
                    "attached GeneratedArtifact identifiers do not match persisted artifacts",
                )
            for artifact_id in attached_artifact_ids:
                artifact = self._artifact_store.get(artifact_id)
                if artifact is None:
                    _issue(
                        issues,
                        "MISSING_ARTIFACT",
                        artifact_id,
                        "GeneratedArtifact was not found",
                    )
                    continue
                artifact_summaries.append(
                    {
                        "id": artifact_id,
                        "artifactType": artifact.get("artifactType"),
                        "taskExecutionId": artifact.get("taskExecutionId"),
                        "contentAddress": artifact.get("contentAddress"),
                    }
                )
                producer_artifacts.append(artifact)
                self._check_common_evidence(
                    artifact, producer_id, expected_trace, expected_revision, workflow, issues
                )
                if artifact.get("artifactType") != expected_artifact_type:
                    _issue(
                        issues,
                        "WRONG_ARTIFACT_TYPE",
                        artifact_id,
                        f"expected {expected_artifact_type} from {producer_task.name}",
                    )
                try:
                    self._artifact_store.get_content(artifact_id)
                except GeneratedArtifactStoreError as error:
                    _issue(issues, "MISSING_ARTIFACT_CONTENT", artifact_id, str(error))

            expected_refs = tuple(
                _resource_ref(value, "Evaluation", "Task.spec.evaluations")
                for value in _evaluation_values(producer_task)
            )
            evaluation_resources: list[Resource] = []
            for expected_ref in expected_refs:
                evaluation = self._resources.get(expected_ref)
                if evaluation is None or evaluation.kind != "Evaluation":
                    raise EvaluateAcceptanceContractError(
                        f"required Evaluation {_ref_record(expected_ref)!r} was not found"
                    )
                evaluation_resources.append(evaluation)
            actual_types = tuple(
                sorted(str(_spec(item).get("type")) for item in evaluation_resources)
            )
            if actual_types != tuple(sorted(expected_evaluation_types)):
                raise EvaluateAcceptanceContractError(
                    f"Task {producer_task.name!r} must declare Evaluation types "
                    f"{expected_evaluation_types!r}"
                )
            attached_evaluation_ids = _optional_string_list(
                producer.get("evaluationResultIds"),
                "TaskExecution.evaluationResultIds",
            )
            required_evaluation_ids.extend(attached_evaluation_ids)
            stored_evaluation_ids = tuple(
                str(item["id"])
                for item in self._runtime_store.list_by_task_execution(producer_id)
                if item.get("kind") == "EvaluationResult"
            )
            if set(attached_evaluation_ids) != set(stored_evaluation_ids):
                _issue(
                    issues,
                    "INCONSISTENT_EVALUATION_ATTACHMENTS",
                    producer_id,
                    "attached EvaluationResult identifiers do not match persisted results",
                )
            for artifact in producer_artifacts:
                artifact_evaluation_ids = _optional_string_list(
                    artifact.get("evaluationResultIds"),
                    "GeneratedArtifact.evaluationResultIds",
                )
                if set(artifact_evaluation_ids) != set(attached_evaluation_ids):
                    _issue(
                        issues,
                        "INCONSISTENT_EVALUATION_ATTACHMENTS",
                        str(artifact["id"]),
                        "GeneratedArtifact does not reference all producer evaluations",
                    )
            results: list[RuntimeObject] = []
            for result_id in attached_evaluation_ids:
                result = self._runtime_store.get(result_id)
                if result is None or result.get("kind") != "EvaluationResult":
                    _issue(
                        issues,
                        "MISSING_EVALUATION",
                        result_id,
                        "EvaluationResult was not found",
                    )
                    continue
                results.append(result)
                _validate_runtime_object(result, "evaluationresult.schema.json")
                evaluation_summaries.append(
                    {
                        "id": result_id,
                        "evaluationRef": deepcopy(dict(result.get("evaluationRef", {}))),
                        "taskExecutionId": result.get("taskExecutionId"),
                        "status": result.get("status"),
                        "outcome": result.get("outcome"),
                        "evidenceAddress": result.get("evidenceAddress"),
                    }
                )
                self._check_common_evidence(
                    result, producer_id, expected_trace, expected_revision, workflow, issues
                )
                evaluation_ref = _resource_ref(
                    result.get("evaluationRef"),
                    "Evaluation",
                    "EvaluationResult.evaluationRef",
                )
                evaluation = self._resources.get(evaluation_ref)
                if evaluation is None:
                    raise EvaluateAcceptanceContractError(
                        "EvaluationResult references missing Evaluation "
                        f"{_ref_record(evaluation_ref)!r}"
                    )
                self._check_evaluation_target(
                    result,
                    evaluation,
                    producer_id,
                    attached_artifact_ids,
                    expected_trace,
                    expected_revision,
                    workflow,
                    issues,
                )
                status = result.get("status")
                outcome = result.get("outcome")
                if status not in {"SUCCEEDED", "FAILED"} or outcome not in {"PASS", "FAIL"}:
                    _issue(
                        issues,
                        "INCOMPLETE_EVALUATION",
                        result_id,
                        "EvaluationResult is not terminal",
                    )
                elif status == "FAILED" and outcome != "FAIL":
                    _issue(
                        issues,
                        "INCONSISTENT_EVALUATION",
                        result_id,
                        "failed EvaluationResult cannot pass",
                    )
                elif status != "SUCCEEDED" or outcome != "PASS":
                    _issue(issues, "FAILED_EVALUATION", result_id, "EvaluationResult did not pass")
                elif result.get("failure") is not None:
                    _issue(
                        issues,
                        "INCONSISTENT_EVALUATION",
                        result_id,
                        "passing EvaluationResult contains failure evidence",
                    )

            actual_refs = [
                _canonical_ref(result.get("evaluationRef")) for result in results
            ]
            for expected_ref in expected_refs:
                count = actual_refs.count(_ref_record(expected_ref))
                if count == 0:
                    _issue(
                        issues,
                        "MISSING_EVALUATION",
                        producer_id,
                        f"required Evaluation {_ref_record(expected_ref)!r} is missing",
                    )
                elif count > 1:
                    _issue(
                        issues,
                        "INCONSISTENT_EVALUATION",
                        producer_id,
                        f"required Evaluation {_ref_record(expected_ref)!r} has multiple results",
                    )
            expected_records = [_ref_record(ref) for ref in expected_refs]
            for actual_ref in actual_refs:
                if actual_ref not in expected_records:
                    _issue(
                        issues,
                        "UNDECLARED_EVALUATION",
                        producer_id,
                        f"EvaluationResult references undeclared Evaluation {actual_ref!r}",
                    )

        issues.sort(key=lambda item: (item["code"], item["subjectId"], item["message"]))
        checks = {
            "complete": not any(item["code"].startswith("MISSING_") for item in issues),
            "sameExecution": not any(item["code"] == "CROSS_EXECUTION" for item in issues),
            "sameRevision": not any(item["code"] == "STALE_REVISION" for item in issues),
            "provenanceConsistent": not any(
                item["code"] in {"INCONSISTENT_PROVENANCE", "INCONSISTENT_ARTIFACT_ATTACHMENTS"}
                for item in issues
            ),
            "allEvaluationsPassed": bool(evaluation_summaries)
            and all(
                item["status"] == "SUCCEEDED" and item["outcome"] == "PASS"
                for item in evaluation_summaries
            ),
        }
        return {
            "type": "acceptance-summary",
            "workflowExecutionId": workflow["id"],
            "taskExecutionId": task_execution["id"],
            "repositoryRevision": expected_revision,
            "predecessorTaskExecutionIds": [item["id"] for item in predecessors],
            "requiredArtifactIds": required_artifact_ids,
            "requiredEvaluationResultIds": required_evaluation_ids,
            "artifacts": artifact_summaries,
            "evaluations": evaluation_summaries,
            "checks": checks,
            "issues": issues,
        }

    def _validate_predecessor(
        self,
        producer: JsonMapping,
        task: Resource,
        workflow: JsonMapping,
        issues: list[dict[str, str]],
    ) -> None:
        producer_id = str(producer.get("id"))
        try:
            _validate_runtime_object(producer, "taskexecution.schema.json")
        except EvaluateAcceptanceContractError as error:
            _issue(
                issues,
                "INVALID_TASK_EXECUTION",
                producer_id,
                str(error),
            )
        provenance = producer.get("provenance")
        if not isinstance(provenance, Mapping):
            _issue(
                issues,
                "INCONSISTENT_PROVENANCE",
                producer_id,
                "TaskExecution provenance is missing",
            )
            return
        resource_refs = provenance.get("resourceRefs")
        if provenance.get("workflowExecutionId") != workflow.get("id"):
            _issue(
                issues,
                "CROSS_EXECUTION",
                producer_id,
                "TaskExecution provenance identifies another workflow",
            )
        if provenance.get("repositoryRevision") != workflow.get(
            "repositoryRevision"
        ):
            _issue(
                issues,
                "STALE_REVISION",
                producer_id,
                "TaskExecution provenance has a stale repository revision",
            )
        if (
            not isinstance(resource_refs, Sequence)
            or isinstance(resource_refs, (str, bytes))
            or _ref_record(task.ref) not in resource_refs
        ):
            _issue(
                issues,
                "INCONSISTENT_PROVENANCE",
                producer_id,
                "TaskExecution provenance does not bind the exact Task Resource",
            )

    def _check_evaluation_target(
        self,
        result: JsonMapping,
        evaluation: Resource,
        producer_id: str,
        artifact_ids: Sequence[str],
        expected_trace: str,
        expected_revision: str,
        workflow: JsonMapping,
        issues: list[dict[str, str]],
    ) -> None:
        result_id = str(result.get("id"))
        target = result.get("target")
        if not isinstance(target, Mapping):
            _issue(issues, "INVALID_EVALUATION_TARGET", result_id, "target is missing")
            return
        evaluation_type = _spec(evaluation).get("type")
        expected_kind = {
            "schema": "AgentInvocation",
            "patch": "GeneratedArtifact",
            "build": "ToolInvocation",
            "test": "ToolInvocation",
        }.get(evaluation_type)
        target_id = target.get("id")
        if target.get("type") != expected_kind or not isinstance(target_id, str):
            _issue(
                issues,
                "INVALID_EVALUATION_TARGET",
                result_id,
                f"{evaluation_type} Evaluation must target {expected_kind}",
            )
            return
        if expected_kind == "GeneratedArtifact":
            if target_id not in artifact_ids:
                _issue(
                    issues,
                    "INVALID_EVALUATION_TARGET",
                    result_id,
                    "patch Evaluation does not target the producer PATCH",
                )
            return
        target_value = self._runtime_store.get(target_id)
        if not (
            isinstance(target_value, Mapping)
            and target_value.get("kind") == expected_kind
            and target_value.get("taskExecutionId") == producer_id
            and target_value.get("traceId") == expected_trace
        ):
            _issue(
                issues,
                "INVALID_EVALUATION_TARGET",
                result_id,
                f"target {target_id!r} is not correlated producer evidence",
            )
            return
        target_schema = (
            "agentinvocation.schema.json"
            if expected_kind == "AgentInvocation"
            else "toolinvocation.schema.json"
        )
        try:
            _validate_runtime_object(target_value, target_schema)
        except EvaluateAcceptanceContractError as error:
            _issue(
                issues,
                "INVALID_EVALUATION_TARGET",
                result_id,
                f"target {target_id!r} violates its runtime schema: {error}",
            )
            return
        self._check_target_evidence(
            target_value,
            producer_id,
            expected_trace,
            expected_revision,
            workflow,
            issues,
        )
        if (
            result.get("outcome") == "PASS"
            and target_value.get("status") != "SUCCEEDED"
        ):
            _issue(
                issues,
                "INVALID_EVALUATION_TARGET",
                result_id,
                "passing EvaluationResult targets unsuccessful producer evidence",
            )

    @staticmethod
    def _check_target_evidence(
        value: JsonMapping,
        producer_id: str,
        expected_trace: str,
        expected_revision: str,
        workflow: JsonMapping,
        issues: list[dict[str, str]],
    ) -> None:
        """Bind invocation targets through their revision-validated producer.

        Invocation runtime contracts do not require repositoryRevision. When an
        adapter records one it must match, but absence is not stale evidence:
        the owning TaskExecution and EvaluationResult provide that binding.
        """

        subject_id = str(value.get("id"))
        provenance = value.get("provenance")
        if (
            value.get("taskExecutionId") != producer_id
            or value.get("traceId") != expected_trace
        ):
            _issue(
                issues,
                "CROSS_EXECUTION",
                subject_id,
                "evaluation target does not match its producer and trace",
            )
        if not isinstance(provenance, Mapping):
            _issue(
                issues,
                "INCONSISTENT_PROVENANCE",
                subject_id,
                "evaluation target provenance is missing",
            )
            return
        if (
            provenance.get("workflowExecutionId") != workflow.get("id")
            or provenance.get("taskExecutionId") != producer_id
        ):
            _issue(
                issues,
                "CROSS_EXECUTION",
                subject_id,
                "evaluation target provenance identifies another execution",
            )
        revision = provenance.get("repositoryRevision")
        if revision is not None and revision != expected_revision:
            _issue(
                issues,
                "STALE_REVISION",
                subject_id,
                "evaluation target records a stale repository revision",
            )

    @staticmethod
    def _check_common_evidence(
        value: JsonMapping,
        producer_id: str,
        expected_trace: str,
        expected_revision: str,
        workflow: JsonMapping,
        issues: list[dict[str, str]],
    ) -> None:
        subject_id = str(value.get("id"))
        provenance = value.get("provenance")
        if value.get("taskExecutionId") != producer_id or value.get("traceId") != expected_trace:
            _issue(
                issues,
                "CROSS_EXECUTION",
                subject_id,
                "evidence identity does not match its producer and trace",
            )
        if not isinstance(provenance, Mapping):
            _issue(issues, "INCONSISTENT_PROVENANCE", subject_id, "evidence provenance is missing")
            return
        if (
            provenance.get("workflowExecutionId") != workflow.get("id")
            or provenance.get("taskExecutionId") != producer_id
        ):
            _issue(
                issues,
                "CROSS_EXECUTION",
                subject_id,
                "evidence provenance identifies another execution",
            )
        revision = value.get(
            "repositoryRevision", provenance.get("repositoryRevision")
        )
        if (
            revision != expected_revision
            or provenance.get("repositoryRevision") != expected_revision
        ):
            _issue(
                issues,
                "STALE_REVISION",
                subject_id,
                "evidence repository revision does not match WorkflowExecution",
            )

    def _attach(self, task_execution_id: object, result_id: str) -> None:
        execution_id = str(task_execution_id)
        current = self._runtime_store.get(execution_id)
        if current is None or current.get("status") != "RUNNING":
            raise EvaluateAcceptanceContractError("TaskExecution is no longer RUNNING")
        values = list(current.get("evaluationResultIds", ()))
        if result_id not in values:
            values.append(result_id)
        self._runtime_store.update_status(
            execution_id,
            "RUNNING",
            expected_status="RUNNING",
            updated_at=self._timestamp(),
            changes={"evaluationResultIds": values},
        )

    def _timestamp(self) -> str:
        value = self._clock()
        if not isinstance(value, str) or not is_rfc3339_timestamp(value):
            raise EvaluateAcceptanceContractError(
                "clock must return an RFC3339 timestamp"
            )
        return value

    def _runtime_id(self, prefix: str, discriminator: str) -> str:
        digest = sha256(
            f"{self.runtime_id_namespace}:{discriminator}:{prefix}".encode()
        ).hexdigest()[:24]
        return f"{prefix}-{digest}"


def _evaluation_values(task: Resource) -> Sequence[Any]:
    values = _spec(task).get("evaluations", ())
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise EvaluateAcceptanceContractError("Task.spec.evaluations must be an array")
    return values


def _resource_ref(value: Any, kind: str, field: str) -> ResourceRef:
    if not isinstance(value, Mapping):
        raise EvaluateAcceptanceContractError(f"{field} must contain {kind} references")
    try:
        ref = ResourceRef.from_mapping(dict(value))
    except (KeyError, TypeError, ValueError):
        raise EvaluateAcceptanceContractError(
            f"{field} contains an invalid Resource reference"
        ) from None
    if ref.kind != kind or ref.version in {"", "latest"}:
        raise EvaluateAcceptanceContractError(f"{field} must contain explicit {kind} versions")
    return ref


def _canonical_ref(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {key: value.get(key) for key in ("kind", "name", "version")}


def _string_list(value: Any, field: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise EvaluateAcceptanceContractError(f"{field} must be an array")
    if not all(isinstance(item, str) and item for item in value) or len(set(value)) != len(value):
        raise EvaluateAcceptanceContractError(f"{field} must contain unique runtime identifiers")
    return tuple(value)


def _optional_string_list(value: Any, field: str) -> tuple[str, ...]:
    return () if value is None else _string_list(value, field)


def _issue(
    issues: list[dict[str, str]], code: str, subject_id: str, message: str
) -> None:
    issues.append({"code": code, "subjectId": subject_id, "message": message})


def _validate_result(result: JsonMapping) -> None:
    _validate_runtime_object(result, "evaluationresult.schema.json")


def _validate_runtime_object(value: JsonMapping, schema_name: str) -> None:
    errors = sorted(
        _runtime_object_validator(schema_name).iter_errors(dict(value)),
        key=lambda error: (list(error.absolute_path), error.message),
    )
    if errors:
        error = errors[0]
        path = "$" + "".join(
            f"[{part}]" if isinstance(part, int) else f".{part}"
            for part in error.absolute_path
        )
        raise EvaluateAcceptanceContractError(
            f"invalid runtime object at {path}: {error.message}"
        )


@cache
def _runtime_object_validator(schema_name: str) -> Draft202012Validator:
    schema_root = Path(__file__).parents[2] / "schemas"
    paths = (
        schema_root / "resources" / "v1" / "resource-definitions.schema.json",
        schema_root / "runtime" / "v1" / "runtime-definitions.schema.json",
        schema_root / "runtime" / "v1" / schema_name,
    )
    schemas = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    registry = Registry()
    for schema in schemas:
        registry = registry.with_resource(
            schema["$id"],
            SchemaResource.from_contents(schema, default_specification=DRAFT202012),
        )
    return Draft202012Validator(schemas[-1], registry=registry)
