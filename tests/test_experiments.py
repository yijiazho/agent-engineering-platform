from copy import deepcopy

import pytest

from aep.experiments import ExperimentError, _compare, load_experiment, run_experiment
from aep.runtime_store import InMemoryRuntimeObjectStore


def _definitions():
    suite = {
        "apiVersion": "aep.dev/v1alpha1", "kind": "EvaluationSuite",
        "metadata": {"name": "smoke", "version": "1.0.0"},
        "spec": {"workflowRef": {"kind": "Workflow", "name": "flow", "version": "1.0.0"},
                 "evaluationRefs": [{"kind": "Evaluation", "name": "quality", "version": "1.0.0"}],
                 "metrics": ["quality"], "cases": [
                     {"id": "case-b", "kind": "fixture", "sourceRevision": "abc", "inputIdentity": "b", "expectedOutcome": "PASS", "metrics": {"quality": 0.8}},
                     {"id": "case-a", "kind": "fixture", "sourceRevision": "abc", "inputIdentity": "a", "expectedOutcome": "PASS", "metrics": {"quality": 0.6}},
                 ]}}
    experiment = {"apiVersion": "aep.dev/v1alpha1", "kind": "Experiment",
                  "metadata": {"name": "prompt-trial", "version": "1.0.0"},
                  "spec": {"evaluationSuiteRef": {"kind": "EvaluationSuite", "name": "smoke", "version": "1.0.0"},
                           "baseline": {"name": "baseline", "resourceOverrides": []},
                           "candidates": [{"name": "candidate-a", "resourceOverrides": []},
                                          {"name": "candidate-b", "resourceOverrides": []}],
                           "metrics": ["quality"]}}
    return experiment, suite


def test_experiment_runs_baseline_and_multiple_candidates_deterministically():
    experiment, suite = _definitions()
    store = InMemoryRuntimeObjectStore()
    result = run_experiment(experiment, suite, store, now=lambda: "2026-01-01T00:00:00Z")
    assert result["status"] == "SUCCEEDED"
    assert result["caseOrder"] == ["case-a", "case-b"]
    assert [arm["name"] for arm in result["arms"]] == ["baseline", "candidate-a", "candidate-b"]
    assert result["comparisons"]["candidate-a"]["quality"]["absoluteDifference"] == 0
    assert result["comparisons"]["candidate-a"]["quality"]["relativeDifference"] == 0
    assert result["comparisons"]["candidate-a"]["passRate"]["absoluteDifference"] == 0
    assert result["provenance"]["publication"] is False


def test_experiment_keeps_partial_failed_arm_and_does_not_drop_it():
    experiment, suite = _definitions()
    suite["spec"]["cases"][0]["expectedOutcome"] = "FAIL"
    result = run_experiment(experiment, suite, InMemoryRuntimeObjectStore())
    assert result["status"] == "FAILED"
    assert all(len(arm["caseResults"]) == 2 for arm in result["arms"])
    assert result["arms"][0]["aggregate"]["passRate"] == 0.5


def test_experiment_rejects_suite_identity_and_duplicate_override():
    experiment, suite = _definitions()
    experiment["spec"]["evaluationSuiteRef"]["version"] = "2.0.0"
    with pytest.raises(ExperimentError, match="identity"):
        run_experiment(experiment, suite, InMemoryRuntimeObjectStore())
    experiment, suite = _definitions()
    experiment["spec"]["baseline"]["resourceOverrides"] = [
        {"kind": "Model", "name": "model", "version": "2.0.0"},
        {"kind": "Model", "name": "model", "version": "3.0.0"},
    ]
    with pytest.raises(ExperimentError, match="once"):
        run_experiment(experiment, suite, InMemoryRuntimeObjectStore())


def test_comparison_reports_regression_and_improvement_with_signed_deltas():
    baseline = {"status": "SUCCEEDED", "aggregate": {"passRate": 0.8, "metrics": {"quality": {"mean": 0.8}}}}
    candidate = {"status": "SUCCEEDED", "aggregate": {"passRate": 0.9, "metrics": {"quality": {"mean": 0.6}}}}
    comparison = _compare(baseline, candidate, ["quality"])
    assert comparison["quality"]["absoluteDifference"] == pytest.approx(-0.2)
    assert comparison["quality"]["relativeDifference"] == pytest.approx(-0.25)
    assert comparison["passRate"]["absoluteDifference"] == pytest.approx(0.1)
    assert comparison["passRate"]["relativeDifference"] == pytest.approx(0.125)


def test_example_experiment_definition_validates():
    from pathlib import Path
    path = Path(__file__).parents[1] / "fixtures/resources/experiment-issue-to-pr.yaml"
    assert load_experiment(path)["kind"] == "Experiment"
