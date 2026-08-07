"""BuildImplementationPlan Task handler composed from AEP runtime boundaries."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from aep.analyze_issue import AnalyzeIssueContractError, AnalyzeIssueTaskHandler


class BuildImplementationPlanContractError(AnalyzeIssueContractError):
    """Raised when planner inputs do not identify one issue analysis."""


class BuildImplementationPlanTaskHandler(AnalyzeIssueTaskHandler):
    """Build an evaluated implementation plan without modifying the checkout."""

    task_name = "build-implementation-plan"
    task_label = "BuildImplementationPlan"
    invocation_label = "Planner"
    artifact_type = "IMPLEMENTATION_PLAN"
    artifact_actor = "build-implementation-plan-task-handler"
    runtime_id_namespace = "build-implementation-plan"

    def _context_arguments(
        self, task_execution: Mapping[str, Any], workflow: Mapping[str, Any]
    ) -> dict[str, object]:
        dependencies = task_execution.get("dependencyTaskExecutionIds", ())
        if (
            isinstance(dependencies, (str, bytes))
            or not isinstance(dependencies, Sequence)
            or len(dependencies) != 1
            or not isinstance(dependencies[0], str)
            or not dependencies[0]
        ):
            raise BuildImplementationPlanContractError(
                "BuildImplementationPlan requires exactly one dependency TaskExecution"
            )

        producer_id = dependencies[0]
        producer = self._runtime_store.get(producer_id)
        if producer is None or producer.get("kind") != "TaskExecution":
            raise BuildImplementationPlanContractError(
                "BuildImplementationPlan dependency TaskExecution was not found"
            )
        producer_ref = producer.get("taskRef")
        if (
            not isinstance(producer_ref, Mapping)
            or producer_ref.get("kind") != "Task"
            or producer_ref.get("name") != "analyze-issue"
            or not isinstance(producer_ref.get("version"), str)
            or not producer_ref["version"]
            or producer_ref["version"] == "latest"
        ):
            raise BuildImplementationPlanContractError(
                "BuildImplementationPlan dependency must be a versioned analyze-issue Task"
            )

        artifacts = self._artifact_store.list_by_task_execution(producer_id)
        issue_analyses = tuple(
            artifact
            for artifact in artifacts
            if artifact.get("artifactType") == "ISSUE_ANALYSIS"
        )
        if len(artifacts) != 1 or len(issue_analyses) != 1:
            raise BuildImplementationPlanContractError(
                "BuildImplementationPlan requires exactly one prior ISSUE_ANALYSIS "
                "GeneratedArtifact"
            )
        analysis = issue_analyses[0]
        artifact_id = analysis.get("id")
        if not _contains_only(producer.get("generatedArtifactIds"), artifact_id):
            raise BuildImplementationPlanContractError(
                "prior ISSUE_ANALYSIS is not attached to its producer TaskExecution"
            )

        evaluation_ids = analysis.get("evaluationResultIds")
        if (
            isinstance(evaluation_ids, (str, bytes))
            or not isinstance(evaluation_ids, Sequence)
            or len(evaluation_ids) != 1
            or not isinstance(evaluation_ids[0], str)
        ):
            raise BuildImplementationPlanContractError(
                "prior ISSUE_ANALYSIS must reference exactly one EvaluationResult"
            )
        evaluation_id = evaluation_ids[0]
        if not _contains(producer.get("evaluationResultIds"), evaluation_id):
            raise BuildImplementationPlanContractError(
                "prior ISSUE_ANALYSIS EvaluationResult is not attached to its producer"
            )
        evaluation = self._runtime_store.get(evaluation_id)
        if not _is_correlated_pass(
            evaluation,
            producer=producer,
            workflow=workflow,
        ):
            raise BuildImplementationPlanContractError(
                "prior ISSUE_ANALYSIS does not have a correlated PASS EvaluationResult"
            )
        return {"prior_task_execution_ids": (producer_id,)}


def _contains(values: object, expected: object) -> bool:
    return (
        not isinstance(values, (str, bytes))
        and isinstance(values, Sequence)
        and expected in values
    )


def _contains_only(values: object, expected: object) -> bool:
    return (
        not isinstance(values, (str, bytes))
        and isinstance(values, Sequence)
        and list(values) == [expected]
    )


def _is_correlated_pass(
    evaluation: object,
    *,
    producer: Mapping[str, Any],
    workflow: Mapping[str, Any],
) -> bool:
    if not isinstance(evaluation, Mapping):
        return False
    provenance = evaluation.get("provenance")
    target = evaluation.get("target")
    return (
        evaluation.get("kind") == "EvaluationResult"
        and evaluation.get("status") == "SUCCEEDED"
        and evaluation.get("outcome") == "PASS"
        and evaluation.get("taskExecutionId") == producer.get("id")
        and evaluation.get("traceId") == producer.get("traceId")
        and isinstance(provenance, Mapping)
        and provenance.get("workflowExecutionId") == workflow.get("id")
        and provenance.get("taskExecutionId") == producer.get("id")
        and provenance.get("repositoryRevision") == workflow.get("repositoryRevision")
        and isinstance(target, Mapping)
        and target.get("type") == "AgentInvocation"
        and _contains(producer.get("agentInvocationIds"), target.get("id"))
    )
