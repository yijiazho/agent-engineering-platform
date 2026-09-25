"""BuildImplementationPlan Task handler composed from AEP runtime boundaries."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
from typing import Any

from aep.analyze_issue import AnalyzeIssueContractError, AnalyzeIssueTaskHandler
from aep.planning_evidence import (
    PlanningEvidenceError, scope_disposition, validate_plan_path_contract,
)


class BuildImplementationPlanContractError(AnalyzeIssueContractError):
    """Raised when planner inputs do not identify one issue analysis."""


def _classification_criterion_id(
    item: Mapping[str, Any], criteria_by_id: Mapping[str, str],
) -> str | None:
    """Resolve the versioned classifier field, retaining old test fixtures."""
    criterion_id = item.get("criterionId")
    if isinstance(criterion_id, str):
        return criterion_id if criterion_id in criteria_by_id else None
    criterion = item.get("criterion")
    if not isinstance(criterion, str):
        return None
    if criterion in criteria_by_id:
        return criterion
    matches = [key for key, text in criteria_by_id.items() if text == criterion]
    return matches[0] if len(matches) == 1 else None


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
        evidence: dict[str, list[Mapping[str, Any]]] = {}
        for item in context_package.get("elements", ()):
            content = item.get("content") if isinstance(item, Mapping) else None
            if isinstance(item, Mapping) and item.get("type") == "planning-evidence" and isinstance(content, Mapping):
                path = content.get("path")
                if isinstance(path, str):
                    evidence.setdefault(path, []).append(content)
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
        authorized = sorted(
            path for path, records in evidence.items()
            if any(record.get("authorizationRole") != "EVALUATOR_ONLY" for record in records)
        )
        evaluator_evidence = [
            record for records in evidence.values() for record in records
            if record.get("authorizationRole") == "EVALUATOR_ONLY"
        ]
        required, no_change, unsupported = [], [], []
        selected = []
        for path in authorized:
            # Null-scope evidence is evaluator-owned and cannot decide a path
            # disposition or grant localized mutation authority.
            records = evidence[path]
            editable = [record for record in records
                        if record.get("authorizationRole") != "EVALUATOR_ONLY"]
            deciding = editable or records
            dispositions = [scope_disposition(record) for record in deciding]
            if "UNSUPPORTED" in dispositions:
                unsupported.append(path)
            elif "CHANGE" in dispositions:
                required.append(path)
            elif dispositions and all(value == "NO_CHANGE" for value in dispositions):
                no_change.append(path)
            else:
                unsupported.append(path)
            selected.extend(records)
        canonical = dict(output)
        canonical.update({
            "authorizedPaths": authorized,
            "requiredChangePaths": required,
            "verifiedNoChangePaths": no_change,
            "unsupportedPaths": unsupported,
            "pathEvidence": selected,
            # Evaluator evidence is preserved for its named deterministic
            # evaluator, but never enters the mutation authorization universe.
            "evaluatorEvidence": evaluator_evidence,
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
        canonical["evaluatorEvidence"] = [item["selectionId"] for item in evaluator_evidence]
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
            unsupported_evidence_bindings: set[tuple[str, Any]] = set()
            for item in evidence:
                # Whole-file evaluator evidence is deliberately not planning
                # authorization. Its evaluator owns the criterion result and
                # it must not poison a scoped insertion on the same path.
                if item.get("authorizationRole") == "EVALUATOR_ONLY":
                    continue
                bindings = item.get("criterionBindings")
                if isinstance(bindings, Sequence) and not isinstance(bindings, (str, bytes)):
                    for binding in bindings:
                        if not isinstance(binding, Mapping):
                            continue
                        criterion_id = binding.get("criterionId")
                        predicate_result = binding.get("predicateResult")
                        postcondition_result = binding.get("postconditionResult")
                        if not isinstance(criterion_id, str) or not criterion_id:
                            continue
                        if (
                            predicate_result == "UNSUPPORTED"
                            or (predicate_result != "MATCH"
                                and postcondition_result != "MATCH")
                        ):
                            unsupported_evidence_bindings.add(
                                (criterion_id, item.get("path"))
                            )
                    continue
                # Older direct Task declarations do not carry an analyzed
                # criterion reference.  Preserve their conservative path-wide
                # behavior, while analysis-derived declarations reconcile by
                # the durable criterion binding above.
                states = [result.get("result") for result in item.get("predicateResults", ())
                          if isinstance(result, Mapping)]
                post_states = [result.get("result") for result in item.get("postconditionResults", ())
                               if isinstance(result, Mapping)]
                if "UNSUPPORTED" in states or not (
                    bool(states) and all(state == "MATCH" for state in states)
                    or bool(post_states) and all(state == "MATCH" for state in post_states)
                ):
                    unsupported_evidence_bindings.add(("__legacy__", item.get("path")))
            self._validate_acceptance_criteria_accounting(
                task_execution, output,
                unsupported_evidence_bindings=unsupported_evidence_bindings,
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
        *, unsupported_evidence_bindings: set[tuple[str, Any]] | None = None,
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
        criterion_records = analysis.get("acceptanceCriteria", ())
        criteria_by_id = {
            item.get("id"): item.get("text") for item in criterion_records
            if isinstance(item, Mapping) and isinstance(item.get("id"), str)
            and isinstance(item.get("text"), str)
        } if isinstance(criterion_records, Sequence) and not isinstance(criterion_records, (str, bytes)) else {}
        if not criteria_by_id and isinstance(criterion_records, Sequence) and not isinstance(criterion_records, (str, bytes)):
            criteria_by_id = {value: value for value in criterion_records if isinstance(value, str)}
        criteria = list(criteria_by_id)
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
            criterion_id = record.get("criterionId", record.get("criterion"))
            values = record.get("requiredInsertions")
            if (
                not isinstance(criterion_id, str)
                or criterion_id not in criteria_by_id
                or criterion_id in expected_by_criterion
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
            expected_by_criterion[criterion_id] = set(expected)
        classifications = plan.get("acceptanceCriteriaClassifications", ())
        classified = [
            _classification_criterion_id(item, criteria_by_id)
            for item in classifications if isinstance(item, Mapping)
        ] if isinstance(classifications, Sequence) and not isinstance(classifications, (str, bytes)) else []
        if (
            not criteria_by_id
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
        evaluator_requirements = analysis.get("evaluatorRequirements", ())
        evaluator_criteria = {
            item.get("criterionId", item.get("criterion")) for item in evaluator_requirements
            if isinstance(item, Mapping)
        } if isinstance(evaluator_requirements, Sequence) and not isinstance(evaluator_requirements, (str, bytes)) else set()
        evaluator_criteria &= set(criteria_by_id)
        insertions = {
            (item.get("path"), item.get("value"))
            for item in plan.get("requiredInsertions", ())
            if isinstance(item, Mapping)
        }
        bound_by_criterion: dict[str, list[tuple[Any, Any]]] = {}
        for item in classifications:
            disposition = item.get("classification")
            criterion = _classification_criterion_id(item, criteria_by_id)
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
            if criterion is None:
                raise BuildImplementationPlanContractError(
                    "implementation plan classifications require a known criterionId"
                )
            bound_by_criterion[criterion] = bindings
            if (
                disposition == "REQUIRED_INSERTION"
                and set(bindings) != expected_by_criterion[criterion]
            ):
                raise BuildImplementationPlanContractError(
                    "criterion insertion bindings must exactly match analyzed requirements"
                )
            if disposition == "REQUIRED_INSERTION":
                expected_paths = {
                    path for path, _value in expected_by_criterion[criterion]
                }
                if (
                    unsupported_evidence_bindings is not None
                    and any(
                        (criterion, path) in unsupported_evidence_bindings
                        or ("__legacy__", path) in unsupported_evidence_bindings
                        for path in expected_paths
                    )
                ):
                    raise BuildImplementationPlanContractError(
                        "required-insertion criterion cannot rely on unsupported path evidence"
                    )
            if disposition == "UNSUPPORTED" and criterion not in unsupported:
                raise BuildImplementationPlanContractError(
                    "unsupported criterion classification must be preserved in unsupportedAcceptanceCriteria"
                )
            if disposition == "UNSUPPORTED" and bindings:
                raise BuildImplementationPlanContractError(
                    "unsupported criterion classification must have no insertion bindings"
                )
            if disposition == "UNSUPPORTED" and expected_by_criterion[criterion]:
                expected_paths = {
                    path for path, _value in expected_by_criterion[criterion]
                }
                if (
                    unsupported_evidence_bindings is not None
                    and not any(
                        (criterion, path) in unsupported_evidence_bindings
                        or ("__legacy__", path) in unsupported_evidence_bindings
                        for path in expected_paths
                    )
                ):
                    raise BuildImplementationPlanContractError(
                        "unsupported criterion requires trusted unsupported path evidence"
                    )
            if disposition == "REQUIRED_INSERTION" and not bindings:
                raise BuildImplementationPlanContractError(
                    "each required-insertion classification must bind at least one insertion"
                )
            if disposition == "EVALUATOR_OWNED":
                if (
                    criterion not in evaluator_criteria
                    or bindings
                    or criterion in unsupported
                    or expected_by_criterion[criterion]
                ):
                    raise BuildImplementationPlanContractError(
                        "evaluator-owned criterion must bind one typed evaluator requirement and no insertions"
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
            _classification_criterion_id(item, criteria_by_id)
            for item in classifications
            if item.get("classification") == "UNSUPPORTED"
        }
        if unsupported != classified_unsupported:
            raise BuildImplementationPlanContractError(
                "unsupportedAcceptanceCriteria must exactly match UNSUPPORTED classifications"
            )
        classified_evaluator_owned = {
            _classification_criterion_id(item, criteria_by_id) for item in classifications
            if item.get("classification") == "EVALUATOR_OWNED"
        }
        if classified_evaluator_owned != evaluator_criteria:
            raise BuildImplementationPlanContractError(
                "EVALUATOR_OWNED classifications must exactly match typed evaluator requirements"
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
