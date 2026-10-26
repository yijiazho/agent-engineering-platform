"""Versioned AI configuration experiment execution and comparison."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, ValidationError

from aep.evaluation_suites import EvaluationSuiteError, run_suite
from aep.execution_replay import ReplayError, _collect_resource_refs
from aep.runtime_store import RuntimeObjectStore


class ExperimentError(ValueError):
    """Experiment definition or arm evidence is invalid."""


def load_experiment(path: str | Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        schema_path = Path(__file__).parents[2] / "schemas/resources/v1/experiment.schema.json"
        if not schema_path.is_file():
            schema_path = Path(__file__).parent / "schemas/resources/v1/experiment.schema.json"
        Draft202012Validator(json.loads(schema_path.read_text(encoding="utf-8"))).validate(value)
    except ValidationError as error:
        raise ExperimentError(f"invalid Experiment: {error.message}") from None
    except Exception as error:
        raise ExperimentError("experiment definition or schema could not be read") from error
    return deepcopy(dict(value))


def run_experiment(
    experiment: Mapping[str, Any], suite: Mapping[str, Any], store: RuntimeObjectStore, *,
    historical_runner: Callable[[str, Sequence[str]], str] | None = None,
    now: Callable[[], str] | None = None,
) -> dict[str, Any]:
    """Run all arms against the same suite definition and persist comparisons."""
    try:
        schema_path = Path(__file__).parents[2] / "schemas/resources/v1/experiment.schema.json"
        if not schema_path.is_file():
            schema_path = Path(__file__).parent / "schemas/resources/v1/experiment.schema.json"
        Draft202012Validator(json.loads(schema_path.read_text(encoding="utf-8"))).validate(experiment)
    except Exception as error:
        raise ExperimentError("invalid Experiment definition") from error
    spec = experiment["spec"]
    suite_ref = spec["evaluationSuiteRef"]
    if (suite_ref["name"], suite_ref["version"]) != (suite["metadata"]["name"], suite["metadata"]["version"]):
        raise ExperimentError("EvaluationSuite identity does not match the declared immutable reference")
    arms = [spec["baseline"], *spec["candidates"]]
    names = [arm["name"] for arm in arms]
    if len(names) != len(set(names)):
        raise ExperimentError("baseline and candidate arm names must be unique")
    baseline_refs = _baseline_refs(suite)
    for arm in arms:
        _validate_overrides(arm["resourceOverrides"], baseline_refs)
        for case in suite["spec"]["cases"]:
            if case["kind"] != "historical":
                continue
            source = store.get(case["workflowExecutionId"])
            if source is None or source.get("kind") != "WorkflowExecution":
                raise ExperimentError(f"historical case {case['id']!r} has no source WorkflowExecution")
            try:
                refs = _collect_resource_refs(source, store.list_by_workflow_execution(case["workflowExecutionId"]))
            except ReplayError as error:
                raise ExperimentError(str(error)) from None
            available = {(ref["kind"], ref["name"]) for ref in refs}
            for override in arm["resourceOverrides"]:
                if (override["kind"], override["name"]) not in available:
                    raise ExperimentError(f"incompatible override {override['kind']}:{override['name']} for case {case['id']}")

    canonical = json.dumps(experiment, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    result_id = "experimentresult-" + digest[:24]
    existing = store.get(result_id)
    if existing and existing.get("status") in {"SUCCEEDED", "FAILED"}:
        return existing
    stamp = (now or _timestamp)()
    record: dict[str, Any] = {
        "apiVersion": "aep.dev/v1alpha1", "kind": "ExperimentResult", "id": result_id,
        "createdAt": stamp, "updatedAt": stamp, "status": "RUNNING",
        "experiment": {"name": experiment["metadata"]["name"], "version": experiment["metadata"]["version"], "digest": digest},
        "evaluationSuiteRef": deepcopy(suite_ref), "caseOrder": sorted(case["id"] for case in suite["spec"]["cases"]),
        "arms": [], "comparisons": {}, "provenance": {"sideEffects": "disabled", "publication": False},
    }
    record = dict(store.create(record, deterministic_key=f"experiment:{digest}"))
    if record.get("status") in {"SUCCEEDED", "FAILED"}:
        return record
    arm_results = []
    for arm in arms:
        arm_suite = deepcopy(dict(suite))
        arm_suite["metadata"]["name"] = f"{suite['metadata']['name']}-{arm['name']}"
        refs = arm_suite["spec"].setdefault("resourceOverrides", [])
        refs.extend(deepcopy(arm["resourceOverrides"]))
        try:
            suite_result = run_suite(arm_suite, store, historical_runner=historical_runner, now=now)
            arm_results.append({"name": arm["name"], "resourceOverrides": deepcopy(arm["resourceOverrides"]), "status": suite_result["status"],
                                "evaluationSuiteRunId": suite_result["id"], "aggregate": deepcopy(suite_result.get("aggregate", {})),
                                "caseResults": deepcopy(suite_result.get("caseResults", [])),
                                "failure": deepcopy(suite_result.get("failure"))})
        except Exception as error:
            arm_results.append({"name": arm["name"], "resourceOverrides": deepcopy(arm["resourceOverrides"]), "status": "FAILED", "evaluationSuiteRunId": None,
                                "aggregate": {}, "caseResults": [], "failure": {"message": str(error)}})
    baseline = arm_results[0]
    comparisons = {arm["name"]: _compare(baseline, arm, spec["metrics"]) for arm in arm_results[1:]}
    status = "SUCCEEDED" if all(arm["status"] == "SUCCEEDED" for arm in arm_results) else "FAILED"
    record.update({"arms": arm_results, "comparisons": comparisons, "status": status, "updatedAt": (now or _timestamp)()})
    return dict(store.update_status(result_id, status, expected_status="RUNNING", updated_at=record["updatedAt"],
                                   changes={"arms": arm_results, "comparisons": comparisons}))


def _baseline_refs(suite: Mapping[str, Any]) -> set[tuple[str, str]]:
    refs = [suite["spec"]["workflowRef"], *suite["spec"]["evaluationRefs"]]
    for case in suite["spec"]["cases"]:
        if case["kind"] == "historical":
            refs.append({"kind": "WorkflowExecution", "name": case["workflowExecutionId"], "version": "1.0.0"})
    return {(ref["kind"], ref["name"]) for ref in refs}


def _validate_overrides(overrides: Sequence[Mapping[str, Any]], baseline: set[tuple[str, str]]) -> None:
    seen = set()
    for ref in overrides:
        identity = (ref["kind"], ref["name"])
        if identity in seen:
            raise ExperimentError("an arm may override each Resource kind and name only once")
        seen.add(identity)
        if identity not in baseline:
            # Historical replay can only replace Resources captured by its provenance.
            # Fixture suite cases have no Resource graph beyond suite-level references.
            if ref["kind"] not in {"Agent", "Prompt", "Model", "KnowledgeBase", "Evaluation", "Policy"}:
                raise ExperimentError(f"incompatible override {ref['kind']}:{ref['name']}")


def _compare(baseline: Mapping[str, Any], candidate: Mapping[str, Any], metrics: Sequence[str]) -> dict[str, Any]:
    names = list(dict.fromkeys([*metrics, "passRate", "tokenUsage.totalTokens", "cost", "latencyMilliseconds", "modelInvocationCount"]))
    result = {}
    for name in names:
        left, right = _metric(baseline.get("aggregate", {}), name), _metric(candidate.get("aggregate", {}), name)
        delta = right - left if left is not None and right is not None else None
        result[name] = {"baseline": left, "candidate": right, "absoluteDifference": delta,
                        "relativeDifference": delta / abs(left) if delta is not None and left != 0 else None}
    result["armStatus"] = {"baseline": baseline.get("status"), "candidate": candidate.get("status")}
    return result


def _metric(aggregate: Mapping[str, Any], name: str) -> float | None:
    aliases = {"tokenUsage.totalTokens": "totalTokens", "tokenUsage.inputTokens": "inputTokens",
               "tokenUsage.outputTokens": "outputTokens"}
    name = aliases.get(name, name)
    value: Any = aggregate
    for part in name.split("."):
        value = value.get(part) if isinstance(value, Mapping) else None
    if name in aggregate.get("metrics", {}):
        metric = aggregate["metrics"][name]
        value = metric.get("mean") if isinstance(metric, Mapping) else metric
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _timestamp() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
