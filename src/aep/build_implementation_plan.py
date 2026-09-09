"""BuildImplementationPlan Task handler composed from AEP runtime boundaries."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
from typing import Any

from aep.analyze_issue import AnalyzeIssueContractError, AnalyzeIssueTaskHandler
from aep.planning_evidence import PlanningEvidenceError, validate_plan_path_contract


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

    def _authoritative_output(
        self, output: Any, task_execution: Mapping[str, Any], workflow: Mapping[str, Any],
        context_package: Mapping[str, Any],
    ) -> Any:
        if not isinstance(output, Mapping):
            return output
        intended = output.get("intendedFiles")
        if isinstance(intended, (str, bytes)) or not isinstance(intended, Sequence):
            return output
        evidence = {
            item.get("content", {}).get("path"): item.get("content")
            for item in context_package.get("elements", ())
            if isinstance(item, Mapping) and item.get("type") == "planning-evidence"
            and isinstance(item.get("content"), Mapping)
        }
        selection = context_package.get("selection", {})
        required_context = selection.get("requiredContext", ()) if isinstance(selection, Mapping) else ()
        evidence_required = "planning-evidence" in required_context
        if not evidence and evidence_required:
            raise BuildImplementationPlanContractError(
                "authoritative plan requires trusted planning evidence"
            )
        if not evidence:
            return output
        proposed = sorted({str(path) for path in intended})
        missing = [path for path in proposed if path not in evidence]
        if missing:
            raise BuildImplementationPlanContractError(
                f"planned paths lack trusted planning evidence: {missing!r}"
            )
        # The model may propose a useful subset, but it cannot omit an exact
        # bounded target whose Task-declared predicate was evaluated. The
        # trusted evidence set is the authoritative authorization universe.
        authorized = sorted(evidence)
        required, no_change, unsupported = [], [], []
        selected = []
        for path in authorized:
            record = evidence[path]
            states = [item.get("result") for item in record.get("predicateResults", ())]
            if "UNSUPPORTED" in states:
                unsupported.append(path)
            elif states and all(state == "MATCH" for state in states):
                required.append(path)
            elif all(
                item.get("result") == "MATCH"
                for item in record.get("postconditionResults", ())
            ) and record.get("postconditionResults"):
                no_change.append(path)
            else:
                unsupported.append(path)
            selected.append(record)
        canonical = dict(output)
        canonical.update({
            "authorizedPaths": authorized,
            "requiredChangePaths": required,
            "verifiedNoChangePaths": no_change,
            "unsupportedPaths": unsupported,
            "pathEvidence": selected,
            # Backward-compatible aliases consumed by the current patch boundary.
            "intendedFiles": authorized,
            "noChangeFiles": no_change,
        })
        try:
            validate_plan_path_contract(canonical, str(workflow["repositoryRevision"]),
                trusted_path_evidence=selected)
        except PlanningEvidenceError as error:
            raise BuildImplementationPlanContractError(str(error)) from error
        canonical["pathEvidence"] = [item["selectionId"] for item in selected]
        return canonical

    def _invocation_output_errors(
        self, task_execution: Mapping[str, Any], context_package: Mapping[str, Any],
        output: Any,
    ) -> list[str]:
        try:
            evidence = [
                item.get("content", {})
                for item in context_package.get("elements", ())
                if isinstance(item, Mapping)
                and item.get("type") == "planning-evidence"
                and isinstance(item.get("content"), Mapping)
            ]
            evidence_paths = {item.get("path") for item in evidence}
            unsupported_evidence_paths = set()
            for item in evidence:
                states = [
                    result.get("result")
                    for result in item.get("predicateResults", ())
                    if isinstance(result, Mapping)
                ]
                post_states = [
                    result.get("result")
                    for result in item.get("postconditionResults", ())
                    if isinstance(result, Mapping)
                ]
                required = bool(states) and all(state == "MATCH" for state in states)
                no_change = bool(post_states) and all(
                    state == "MATCH" for state in post_states
                )
                if "UNSUPPORTED" in states or not (required or no_change):
                    unsupported_evidence_paths.add(item.get("path"))
            self._validate_acceptance_criteria_accounting(
                task_execution, output,
                unsupported_evidence_paths=unsupported_evidence_paths,
            )
            insertion_paths = {
                item.get("path") for item in output.get("requiredInsertions", ())
                if isinstance(item, Mapping)
            } if isinstance(output, Mapping) else set()
            if evidence_paths and not insertion_paths.issubset(evidence_paths):
                raise BuildImplementationPlanContractError(
                    "required insertions must target trusted authorized paths"
                )
        except BuildImplementationPlanContractError as error:
            return [str(error)]
        return []

    def _validate_acceptance_criteria_accounting(
        self, task_execution: Mapping[str, Any], plan: Any,
        *, unsupported_evidence_paths: set[Any] | None = None,
    ) -> None:
        if not isinstance(plan, Mapping):
            return
        dependencies = task_execution.get("dependencyTaskExecutionIds", ())
        if not isinstance(dependencies, Sequence) or not dependencies:
            return
        artifacts = self._artifact_store.list_by_task_execution(str(dependencies[0]))
        analyses = [item for item in artifacts if item.get("artifactType") == "ISSUE_ANALYSIS"]
        if len(analyses) != 1:
            return
        analysis = json.loads(
            self._artifact_store.get_content(str(analyses[0]["id"])).decode("utf-8")
        )
        criteria = analysis.get("acceptanceCriteria", ())
        expected_records = analysis.get("acceptanceCriterionInsertions", ())
        if (
            isinstance(expected_records, (str, bytes))
            or not isinstance(expected_records, Sequence)
        ):
            raise BuildImplementationPlanContractError(
                "issue analysis must provide per-criterion insertion requirements"
            )
        expected_by_criterion: dict[str, set[tuple[Any, Any]]] = {}
        for record in expected_records:
            if not isinstance(record, Mapping):
                raise BuildImplementationPlanContractError(
                    "issue analysis must provide per-criterion insertion requirements"
                )
            criterion = record.get("criterion")
            values = record.get("requiredInsertions")
            if (
                not isinstance(criterion, str)
                or not criterion
                or criterion in expected_by_criterion
                or isinstance(values, (str, bytes))
                or not isinstance(values, Sequence)
            ):
                raise BuildImplementationPlanContractError(
                    "issue analysis must provide per-criterion insertion requirements"
                )
            expected = []
            for value in values:
                if not isinstance(value, Mapping):
                    raise BuildImplementationPlanContractError(
                        "issue analysis must provide per-criterion insertion requirements"
                    )
                key = (value.get("path"), value.get("value"))
                if any(not isinstance(part, str) or not part for part in key):
                    raise BuildImplementationPlanContractError(
                        "issue analysis must provide per-criterion insertion requirements"
                    )
                if key in expected:
                    raise BuildImplementationPlanContractError(
                        "issue analysis insertion requirements must be unique per criterion"
                    )
                expected.append(key)
            expected_by_criterion[criterion] = set(expected)
        classifications = plan.get("acceptanceCriteriaClassifications", ())
        classified = [
            item.get("criterion") for item in classifications if isinstance(item, Mapping)
        ] if isinstance(classifications, Sequence) and not isinstance(classifications, (str, bytes)) else []
        if (
            not isinstance(criteria, Sequence)
            or isinstance(criteria, (str, bytes))
            or sorted(classified) != sorted(criteria)
            or len(classified) != len(set(classified))
        ):
            raise BuildImplementationPlanContractError(
                "implementation plan must classify every analyzed acceptance criterion exactly once"
            )
        if set(expected_by_criterion) != set(criteria):
            raise BuildImplementationPlanContractError(
                "issue analysis must map every acceptance criterion exactly once"
            )
        unsupported_values = plan.get("unsupportedAcceptanceCriteria", ())
        if (
            isinstance(unsupported_values, (str, bytes))
            or not isinstance(unsupported_values, Sequence)
            or any(not isinstance(value, str) or not value for value in unsupported_values)
            or len(set(unsupported_values)) != len(unsupported_values)
        ):
            raise BuildImplementationPlanContractError(
                "unsupportedAcceptanceCriteria must contain unique criteria"
            )
        unsupported = set(unsupported_values)
        insertions = {
            (item.get("path"), item.get("value"))
            for item in plan.get("requiredInsertions", ())
            if isinstance(item, Mapping)
        }
        bound_by_criterion: dict[str, list[tuple[Any, Any]]] = {}
        for item in classifications:
            disposition = item.get("classification")
            criterion = item.get("criterion")
            plural = item.get("requiredInsertions")
            legacy = item.get("requiredInsertion")
            if plural is not None and legacy is not None:
                raise BuildImplementationPlanContractError(
                    "plural requiredInsertions cannot conflict with legacy requiredInsertion"
                )
            if plural is None and legacy is not None:
                plural = [legacy]
            if plural is None:
                plural = []
            if not isinstance(plural, Sequence) or isinstance(plural, (str, bytes)):
                raise BuildImplementationPlanContractError(
                    "required-insertion bindings must be an array"
                )
            bindings = []
            for binding in plural:
                if not isinstance(binding, Mapping):
                    raise BuildImplementationPlanContractError(
                        "each required-insertion classification must bind its own insertion evidence"
                    )
                key = (binding.get("path"), binding.get("value"))
                if key in bindings:
                    raise BuildImplementationPlanContractError(
                        "required-insertion bindings must be unique"
                    )
                if key not in insertions:
                    raise BuildImplementationPlanContractError(
                        "each required-insertion classification must bind its own insertion evidence"
                    )
                bindings.append(key)
            bound_by_criterion[str(criterion)] = bindings
            if (
                disposition == "REQUIRED_INSERTION"
                and set(bindings) != expected_by_criterion[str(criterion)]
            ):
                raise BuildImplementationPlanContractError(
                    "criterion insertion bindings must exactly match analyzed requirements"
                )
            if disposition == "UNSUPPORTED" and criterion not in unsupported:
                raise BuildImplementationPlanContractError(
                    "unsupported criterion classification must be preserved in unsupportedAcceptanceCriteria"
                )
            if disposition == "UNSUPPORTED" and bindings:
                raise BuildImplementationPlanContractError(
                    "unsupported criterion classification must have no insertion bindings"
                )
            if disposition == "UNSUPPORTED" and expected_by_criterion[str(criterion)]:
                expected_paths = {
                    path for path, _value in expected_by_criterion[str(criterion)]
                }
                if (
                    unsupported_evidence_paths is not None
                    and not expected_paths.intersection(unsupported_evidence_paths)
                ):
                    raise BuildImplementationPlanContractError(
                        "unsupported criterion requires trusted unsupported path evidence"
                    )
            if disposition == "REQUIRED_INSERTION" and not bindings:
                raise BuildImplementationPlanContractError(
                    "each required-insertion classification must bind at least one insertion"
                )
        binders: dict[tuple[Any, Any], list[str]] = {}
        for criterion, values in bound_by_criterion.items():
            for value in values:
                binders.setdefault(value, []).append(criterion)
        # A shared canonical insertion may support multiple criteria. Its owner
        # is derived deterministically without removing the non-owner bindings.
        lexical_owners = {
            value: min(criteria) for value, criteria in binders.items()
        }
        bound = set(lexical_owners)
        if bound != insertions:
            raise BuildImplementationPlanContractError(
                "every required insertion must have deterministic criterion ownership"
            )
        classified_unsupported = {
            str(item.get("criterion"))
            for item in classifications
            if item.get("classification") == "UNSUPPORTED"
        }
        if unsupported != classified_unsupported:
            raise BuildImplementationPlanContractError(
                "unsupportedAcceptanceCriteria must exactly match UNSUPPORTED classifications"
            )

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
