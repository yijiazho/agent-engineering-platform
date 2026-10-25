"""Versioned, durable evaluation suite execution and aggregation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable

import yaml

from aep.runtime_store import RuntimeObjectStore


class EvaluationSuiteError(ValueError):
    """Invalid suite definition or case evidence."""


def load_suite(path: Path | str) -> dict[str, Any]:
    try:
        value = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        raise EvaluationSuiteError("evaluation suite could not be read") from None
    if not isinstance(value, Mapping):
        raise EvaluationSuiteError("evaluation suite must be an object")
    suite = deepcopy(dict(value))
    if suite.get("apiVersion") != "aep.dev/v1alpha1" or suite.get("kind") != "EvaluationSuite":
        raise EvaluationSuiteError("suite must declare aep.dev/v1alpha1 EvaluationSuite")
    metadata, spec = suite.get("metadata"), suite.get("spec")
    if not isinstance(metadata, Mapping) or not isinstance(metadata.get("name"), str) or not metadata.get("version"):
        raise EvaluationSuiteError("suite metadata requires name and version")
    if not isinstance(spec, Mapping) or not isinstance(spec.get("cases"), list) or not spec["cases"]:
        raise EvaluationSuiteError("suite spec requires at least one case")
    _validate_ref(spec.get("workflowRef"), "Workflow")
    evals = spec.get("evaluationRefs")
    if not isinstance(evals, list) or not evals:
        raise EvaluationSuiteError("suite requires versioned evaluationRefs")
    for ref in evals:
        _validate_ref(ref, "Evaluation")
    seen: set[str] = set()
    for case in spec["cases"]:
        if not isinstance(case, Mapping) or not isinstance(case.get("id"), str) or not case["id"] or case["id"] in seen:
            raise EvaluationSuiteError("cases require unique non-empty ids")
        seen.add(case["id"])
        kind = case.get("kind")
        if kind == "historical":
            if not isinstance(case.get("workflowExecutionId"), str) or not case["workflowExecutionId"]:
                raise EvaluationSuiteError("historical cases require workflowExecutionId")
        elif kind == "fixture":
            if not isinstance(case.get("sourceRevision"), str) or not isinstance(case.get("inputIdentity"), str):
                raise EvaluationSuiteError("fixture cases require sourceRevision and inputIdentity")
            if case.get("expectedOutcome") not in {"PASS", "FAIL"}:
                raise EvaluationSuiteError("fixture cases require expectedOutcome PASS or FAIL")
        else:
            raise EvaluationSuiteError("case kind must be fixture or historical")
    return suite


def run_suite(
    suite: Mapping[str, Any], store: RuntimeObjectStore, *,
    historical_runner: Callable[[str], str] | None = None,
    now: Callable[[], str] | None = None,
) -> dict[str, Any]:
    """Run cases independently; persist each terminal case before continuing."""
    canonical = json.dumps(suite, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    suite_id = "evaluationsuite-" + digest[:24]
    spec = suite["spec"]
    cases = sorted(spec["cases"], key=lambda item: item["id"])
    clock = now or (lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
    started = clock()
    record: dict[str, Any] = {
        "apiVersion": "aep.dev/v1alpha1", "kind": "EvaluationSuiteRun", "id": suite_id,
        "createdAt": started, "updatedAt": started, "status": "RUNNING",
        "suite": {"name": suite["metadata"]["name"], "version": suite["metadata"]["version"], "digest": digest},
        "workflowRef": deepcopy(spec["workflowRef"]), "evaluationRefs": deepcopy(spec["evaluationRefs"]),
        "caseOrder": [case["id"] for case in cases], "caseResults": [],
        "provenance": {"actor": "evaluation-suite-runner", "sideEffects": "disabled"},
    }
    record = dict(store.create(record, deterministic_key=f"evaluation-suite:{digest}"))
    results = list(record.get("caseResults", []))
    completed_ids = {item["caseId"] for item in results}
    for case in cases:
        if case["id"] in completed_ids:
            continue
        case_started = clock()
        try:
            if case["kind"] == "fixture":
                outcome = case["expectedOutcome"]
                evidence = {"sourceRevision": case["sourceRevision"], "inputIdentity": case["inputIdentity"],
                            "evaluationSchemaVersion": case.get("evaluationSchemaVersion", "1.0.0"),
                            "resourceRefs": [deepcopy(spec["workflowRef"]), *deepcopy(spec["evaluationRefs"])],
                            "metrics": deepcopy(case.get("metrics", {})), "buildSuccess": case.get("buildSuccess"),
                            "testSuccess": case.get("testSuccess"), "modelInvocationCount": 0,
                            "tokenUsage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0}, "cost": None}
            else:
                if historical_runner is None:
                    raise EvaluationSuiteError("historical case execution requires the workflow runtime")
                execution_id = historical_runner(case["workflowExecutionId"])
                execution = store.get(execution_id)
                if execution is None:
                    raise EvaluationSuiteError("replayed execution evidence is missing")
                related = store.list_by_workflow_execution(execution_id)
                evidence = _execution_evidence(execution, related, case["workflowExecutionId"])
                outcome = "PASS" if execution.get("status") == "SUCCEEDED" else "FAIL"
                evidence["sourceRevision"] = execution.get("repositoryRevision")
                evidence["inputIdentity"] = case["workflowExecutionId"]
            failure = None
        except Exception as error:
            outcome, evidence, failure = "FAIL", {}, str(error)
        case_record = {"caseId": case["id"], "kind": case["kind"], "outcome": outcome,
                       "startedAt": case_started, "completedAt": clock(), "evidence": evidence}
        if failure:
            case_record["failure"] = failure
        results.append(case_record)
        record.update({"caseResults": deepcopy(results), "updatedAt": case_record["completedAt"]})
        record = dict(store.update_status(suite_id, "RUNNING", expected_status="RUNNING",
                                          updated_at=case_record["completedAt"], changes={"caseResults": results}))
    aggregate = aggregate_results(results, spec.get("metrics", []))
    final_status = "SUCCEEDED" if all(item["outcome"] == "PASS" for item in results) else "FAILED"
    return dict(store.update_status(suite_id, final_status, expected_status="RUNNING", updated_at=clock(),
                                    changes={"aggregate": aggregate}))


def aggregate_results(results: Sequence[Mapping[str, Any]], configured_metrics: Sequence[str] = ()) -> dict[str, Any]:
    count = len(results)
    evidence = [item.get("evidence", {}) for item in results]
    metric_names = sorted(set(configured_metrics) | {key for item in evidence for key in item.get("metrics", {})})
    totals = {"modelInvocationCount": 0, "inputTokens": 0, "outputTokens": 0, "totalTokens": 0, "cost": 0.0,
              "costAvailable": False, "latencyMilliseconds": 0}
    metrics: dict[str, list[float]] = {key: [] for key in metric_names}
    for result in results:
        item = result.get("evidence", {})
        totals["modelInvocationCount"] += item.get("modelInvocationCount", 0)
        usage = item.get("tokenUsage", {})
        for key in ("inputTokens", "outputTokens", "totalTokens"):
            totals[key] += usage.get(key, 0)
        if item.get("cost") is not None:
            totals["cost"] += item["cost"]
            totals["costAvailable"] = True
        try:
            start = datetime.fromisoformat(result["startedAt"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(result["completedAt"].replace("Z", "+00:00"))
            totals["latencyMilliseconds"] += max(0, int((end - start).total_seconds() * 1000))
        except (KeyError, ValueError, TypeError):
            pass
        for key in metric_names:
            value = item.get("metrics", {}).get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                metrics[key].append(value)
    totals["cost"] = totals["cost"] if totals["costAvailable"] else None
    totals["caseCount"] = count
    totals["passRate"] = sum(item.get("outcome") == "PASS" for item in results) / count if count else 0.0
    totals["buildSuccessRate"] = _success_rate(evidence, "buildSuccess")
    totals["testSuccessRate"] = _success_rate(evidence, "testSuccess")
    totals["metrics"] = {
        key: {"mean": sum(values) / len(values) if values else None, "count": len(values)}
        for key, values in metrics.items()
    }
    return totals


def _execution_evidence(execution: Mapping[str, Any], related: Sequence[Mapping[str, Any]], source_id: str) -> dict[str, Any]:
    models = [item for item in related if item.get("kind") == "ModelInvocation"]
    token = {key: sum((item.get("tokenUsage") or {}).get(key, 0) for item in models)
             for key in ("inputTokens", "outputTokens", "totalTokens")}
    costs = [item["cost"] for item in models if isinstance(item.get("cost"), (int, float))]
    return {"sourceExecutionId": source_id, "repositoryRevision": execution.get("repositoryRevision"),
            "resourceRefs": deepcopy(execution.get("provenance", {}).get("resourceRefs", [])),
            "generatedArtifactIds": [item.get("id") for item in related if item.get("kind") == "GeneratedArtifact"],
        "evaluationResults": [{"id": item.get("id"), "outcome": item.get("outcome"),
                                   "evaluationRef": deepcopy(item.get("evaluationRef")),
                                   "evaluationSchemaVersion": (item.get("evaluationRef") or {}).get("version"),
                                   "metrics": deepcopy(item.get("metrics", {}))}
                                  for item in related if item.get("kind") == "EvaluationResult"],
            "metrics": _evaluation_metrics(related),
            "modelInvocationCount": len(models), "tokenUsage": token,
            "cost": sum(costs) if costs else None, "buildSuccess": execution.get("buildSuccess"),
            "testSuccess": execution.get("testSuccess")}


def _success_rate(items: Sequence[Mapping[str, Any]], field: str) -> float | None:
    values = [item[field] for item in items if isinstance(item.get(field), bool)]
    return sum(values) / len(values) if values else None


def _evaluation_metrics(related: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for item in related:
        metrics = item.get("metrics")
        if isinstance(metrics, Mapping):
            for key, value in metrics.items():
                if isinstance(key, str) and isinstance(value, (int, float)) and not isinstance(value, bool):
                    totals[key] = totals.get(key, 0) + value
    return totals


def _validate_ref(ref: Any, kind: str) -> None:
    if (not isinstance(ref, Mapping) or ref.get("kind") != kind
            or not isinstance(ref.get("name"), str) or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", ref["name"])
            or not isinstance(ref.get("version"), str)
            or not re.fullmatch(r"v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?", ref["version"])):
        raise EvaluationSuiteError(f"suite requires an immutable {kind} reference")
