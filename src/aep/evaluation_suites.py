"""Versioned, durable evaluation suite execution and aggregation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
from threading import Event, Thread
from typing import Any, Callable
from uuid import uuid4

import yaml
from jsonschema import Draft202012Validator

from aep.runtime_store import RuntimeObjectStore


class EvaluationSuiteError(ValueError):
    """Invalid suite definition or case evidence."""


def _validate_suite(suite: Mapping[str, Any]) -> None:
    schema_path = Path(__file__).parents[2] / "schemas" / "resources" / "v1" / "evaluationsuite.schema.json"
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator(schema).validate(suite)
    except Exception as error:
        from jsonschema import ValidationError
        if isinstance(error, ValidationError):
            raise EvaluationSuiteError(f"invalid EvaluationSuite: {error.message}") from None
        raise EvaluationSuiteError("evaluation suite schema could not be loaded") from None
    spec = suite["spec"]
    _validate_ref(spec["workflowRef"], "Workflow")
    for reference in spec["evaluationRefs"]:
        _validate_ref(reference, "Evaluation")


def load_suite(path: Path | str) -> dict[str, Any]:
    try:
        value = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        raise EvaluationSuiteError("evaluation suite could not be read") from None
    if not isinstance(value, Mapping):
        raise EvaluationSuiteError("evaluation suite must be an object")
    suite = deepcopy(dict(value))
    _validate_suite(suite)
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
    historical_runner: Callable[[str, Sequence[str]], str] | None = None,
    now: Callable[[], str] | None = None,
) -> dict[str, Any]:
    """Run cases independently; persist each terminal case before continuing."""
    _validate_suite(suite)
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
    if record.get("status") in {"SUCCEEDED", "FAILED", "CANCELLED"}:
        return record
    lease_key = f"evaluation-suite-run:{suite_id}"
    owner_id = str(uuid4())
    now_dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
    expiry = (now_dt + timedelta(seconds=45)).isoformat().replace("+00:00", "Z")
    if not store.acquire_lease(lease_key, owner_id, now=started, expires_at=expiry):
        raise EvaluationSuiteError("evaluation suite is already running")
    heartbeat_stop, lease_lost = Event(), Event()

    def heartbeat() -> None:
        while not heartbeat_stop.wait(10):
            current = clock()
            current_dt = datetime.fromisoformat(current.replace("Z", "+00:00"))
            next_expiry = (current_dt + timedelta(seconds=45)).isoformat().replace("+00:00", "Z")
            if not store.renew_lease(lease_key, owner_id, now=current, expires_at=next_expiry):
                lease_lost.set()
                return

    heartbeat_thread = Thread(target=heartbeat, name=f"{suite_id}-lease", daemon=True)
    heartbeat_thread.start()
    try:
        return _run_claimed_suite(suite, store, historical_runner, clock, suite_id, record, lease_lost)
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=2)
        store.release_lease(lease_key, owner_id)


def _run_claimed_suite(suite: Mapping[str, Any], store: RuntimeObjectStore,
                       historical_runner: Callable[[str, Sequence[str]], str] | None,
                       clock: Callable[[], str], suite_id: str,
                       record: dict[str, Any], lease_lost: Event) -> dict[str, Any]:
    spec = suite["spec"]
    cases = sorted(spec["cases"], key=lambda item: item["id"])
    results = list(record.get("caseResults", []))
    completed_ids = {item["caseId"] for item in results}
    for case in cases:
        if case["id"] in completed_ids:
            continue
        if lease_lost.is_set():
            raise EvaluationSuiteError("evaluation suite execution lease was lost")
        case_started = clock()
        failure = None
        try:
            if case["kind"] == "fixture":
                outcome = case["expectedOutcome"]
                evidence = {"sourceRevision": case["sourceRevision"], "inputIdentity": case["inputIdentity"],
                            "evaluationSchemaVersions": {
                                f"{ref['name']}:{ref['version']}": ref["version"]
                                for ref in spec["evaluationRefs"]
                            },
                            "resourceRefs": [deepcopy(spec["workflowRef"]), *deepcopy(spec["evaluationRefs"])],
                            "metrics": deepcopy(case.get("metrics", {})), "buildSuccess": case.get("buildSuccess"),
                            "testSuccess": case.get("testSuccess"), "modelInvocationCount": 0,
                            "tokenUsage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0}, "cost": None}
            else:
                if historical_runner is None:
                    raise EvaluationSuiteError("historical case execution requires the workflow runtime")
                source = store.get(case["workflowExecutionId"])
                if source is None or source.get("kind") != "WorkflowExecution":
                    raise EvaluationSuiteError("historical WorkflowExecution was not found")
                overrides = _historical_overrides(source, store.list_by_workflow_execution(case["workflowExecutionId"]), spec)
                execution_id = historical_runner(case["workflowExecutionId"], overrides)
                execution = store.get(execution_id)
                if execution is None:
                    raise EvaluationSuiteError("replayed execution evidence is missing")
                related = store.list_by_workflow_execution(execution_id)
                evidence = _execution_evidence(execution, related, case["workflowExecutionId"])
                passed, evaluation_failure = _historical_case_passed(
                    execution, related, spec["evaluationRefs"]
                )
                outcome = "PASS" if passed else "FAIL"
                if not passed:
                    failure = evaluation_failure
                evidence["sourceRevision"] = execution.get("repositoryRevision")
                evidence["inputIdentity"] = case["workflowExecutionId"]
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


def _execution_evidence(
    execution: Mapping[str, Any], related: Sequence[Mapping[str, Any]], source_id: str
) -> dict[str, Any]:
    models = [item for item in related if item.get("kind") == "ModelInvocation"]
    input_tokens = sum(_usage(item).get("inputTokens", 0) for item in models)
    output_tokens = sum(_usage(item).get("outputTokens", 0) for item in models)
    token = {"inputTokens": input_tokens, "outputTokens": output_tokens,
             "totalTokens": input_tokens + output_tokens}
    costs = [item["cost"] for item in models if isinstance(item.get("cost"), (int, float))]
    evaluations = [item for item in related if item.get("kind") == "EvaluationResult"]
    build_results = [item for item in evaluations if _evaluation_type(item) == "build"]
    test_results = [item for item in evaluations if _evaluation_type(item) == "test"]
    return {"sourceExecutionId": source_id, "executionId": execution.get("id"),
            "repositoryRevision": execution.get("repositoryRevision"),
            "resourceRefs": deepcopy(execution.get("provenance", {}).get("resourceRefs", [])),
            "generatedArtifactIds": [item.get("id") for item in related if item.get("kind") == "GeneratedArtifact"],
        "evaluationResults": [{"id": item.get("id"), "outcome": item.get("outcome"),
                                   "evaluationRef": deepcopy(item.get("evaluationRef")),
                                   "evaluationSchemaVersion": (item.get("evaluationRef") or {}).get("version"),
                                   "metrics": deepcopy(item.get("metrics", {}))}
                                  for item in evaluations],
            "taskExecutions": [
                {key: deepcopy(item[key]) for key in ("id", "taskRef", "attempt", "status", "failure") if key in item}
                for item in sorted(
                    (value for value in related if value.get("kind") == "TaskExecution"),
                    key=lambda value: (str(value.get("taskRef")), int(value.get("attempt", 0)), str(value.get("id", ""))),
                )
            ],
            "metrics": _evaluation_metrics(evaluations),
            "modelInvocationCount": len(models), "tokenUsage": token,
            "cost": sum(costs) if costs else None,
            "buildSuccess": _all_pass(build_results), "testSuccess": _all_pass(test_results)}


def _usage(invocation: Mapping[str, Any]) -> dict[str, int]:
    value = invocation.get("tokenUsage")
    if not isinstance(value, Mapping):
        return {"inputTokens": 0, "outputTokens": 0}
    return {"inputTokens": value.get("inputTokens", value.get("input", 0)),
            "outputTokens": value.get("outputTokens", value.get("output", 0))}


def _evaluation_type(result: Mapping[str, Any]) -> str | None:
    evidence = result.get("evidence")
    value = evidence.get("evaluationType") if isinstance(evidence, Mapping) else None
    return value.lower() if isinstance(value, str) else None


def _all_pass(results: Sequence[Mapping[str, Any]]) -> bool | None:
    if not results:
        return None
    return all(item.get("outcome") == "PASS" for item in results)


def _historical_case_passed(
    execution: Mapping[str, Any], related: Sequence[Mapping[str, Any]],
    evaluation_refs: Sequence[Mapping[str, Any]],
) -> tuple[bool, str | None]:
    for reference in evaluation_refs:
        matching = [
            result for result in related
            if result.get("kind") == "EvaluationResult"
            and result.get("evaluationRef") == reference
        ]
        if not matching:
            return False, f"selected Evaluation {reference['name']!r} produced no result"
        if any(result.get("status") != "SUCCEEDED" or result.get("outcome") != "PASS" for result in matching):
            return False, f"selected Evaluation {reference['name']!r} did not pass"
    publication_disabled = (
        execution.get("replayProvenance", {}).get("publicationMode") == "disabled"
        if isinstance(execution.get("replayProvenance"), Mapping) else False
    )
    effective_tasks: dict[str, Mapping[str, Any]] = {}
    for task in related:
        if task.get("kind") != "TaskExecution":
            continue
        reference = task.get("taskRef")
        key = json.dumps(reference, sort_keys=True) if isinstance(reference, Mapping) else str(task.get("id"))
        previous = effective_tasks.get(key)
        if previous is None or (int(task.get("attempt", 0)), str(task.get("id", ""))) > (
            int(previous.get("attempt", 0)), str(previous.get("id", ""))
        ):
            effective_tasks[key] = task
    publication_denial_seen = False
    for task in effective_tasks.values():
        if task.get("status") not in {"FAILED", "BLOCKED"}:
            continue
        task_ref = task.get("taskRef")
        failure = task.get("failure")
        publication_denial = (
            publication_disabled and task.get("status") == "FAILED"
            and isinstance(task_ref, Mapping) and task_ref.get("name") == "create-pull-request"
            and isinstance(failure, Mapping) and failure.get("class") == "POLICY"
            and failure.get("message") == "publication is disabled for replay"
        )
        if publication_denial:
            publication_denial_seen = True
        else:
            return False, "historical replay contains a failed or blocked Task"
    if execution.get("status") != "SUCCEEDED" and not (
        execution.get("status") == "FAILED" and publication_denial_seen
    ):
        return False, "historical replay did not complete successfully"
    return True, None


def _success_rate(items: Sequence[Mapping[str, Any]], field: str) -> float | None:
    values = [item[field] for item in items if isinstance(item.get(field), bool)]
    return sum(values) / len(values) if values else None


def _evaluation_metrics(related: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for item in related:
        if item.get("kind") != "EvaluationResult":
            continue
        metrics = item.get("metrics")
        if isinstance(metrics, Mapping):
            for key, value in metrics.items():
                if isinstance(key, str) and isinstance(value, (int, float)) and not isinstance(value, bool):
                    totals[key] = totals.get(key, 0) + value
    return totals


def _historical_overrides(
    source: Mapping[str, Any], related: Sequence[Mapping[str, Any]], spec: Mapping[str, Any]
) -> list[str]:
    selected_workflow = spec["workflowRef"]
    source_workflow = source.get("workflowRef")
    if not isinstance(source_workflow, Mapping) or any(
        selected_workflow.get(key) != source_workflow.get(key)
        for key in ("kind", "name", "version")
    ):
        raise EvaluationSuiteError("suite Workflow does not match the historical execution; Workflow overrides are unsupported")
    historical_refs: list[Any] = []
    root = source.get("provenance")
    if isinstance(root, Mapping) and isinstance(root.get("resourceRefs"), list):
        historical_refs.extend(root["resourceRefs"])
    for item in related:
        provenance = item.get("provenance")
        if isinstance(provenance, Mapping) and isinstance(provenance.get("resourceRefs"), list):
            historical_refs.extend(provenance["resourceRefs"])
        for field in ("evaluationRef",):
            if item.get(field) is not None:
                historical_refs.append(item[field])
    selected = spec["evaluationRefs"]
    available = {
        (ref.get("kind"), ref.get("name")): ref
        for ref in historical_refs if isinstance(ref, Mapping)
    }
    overrides: list[str] = []
    for ref in selected:
        prior = available.get((ref["kind"], ref["name"]))
        if prior is None:
            raise EvaluationSuiteError(f"selected Evaluation {ref['name']!r} is absent from historical Resource provenance")
        if prior.get("version") != ref["version"]:
            overrides.append(f"Evaluation:{ref['name']}:{ref['version']}")
    return overrides


def _validate_ref(ref: Any, kind: str) -> None:
    if (not isinstance(ref, Mapping) or ref.get("kind") != kind
            or not isinstance(ref.get("name"), str) or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", ref["name"])
            or not isinstance(ref.get("version"), str)
            or not re.fullmatch(r"v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?", ref["version"])):
        raise EvaluationSuiteError(f"suite requires an immutable {kind} reference")
