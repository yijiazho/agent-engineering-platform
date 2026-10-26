from copy import deepcopy

import pytest

from aep.evaluation_suites import _historical_overrides
from aep.experiments import ExperimentError, _arm_suite, _compare, _metric, load_experiment, run_experiment
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


def test_display_arm_names_do_not_become_resource_names():
    experiment, suite = _definitions()
    experiment["spec"]["candidates"][0]["name"] = "Candidate A"
    result = run_experiment(experiment, suite, InMemoryRuntimeObjectStore())
    assert result["arms"][1]["name"] == "Candidate A"
    assert result["arms"][1]["status"] == "SUCCEEDED"
    assert _arm_suite(suite, experiment["spec"]["candidates"][0], 1)["metadata"]["name"] == "smoke-arm-2"


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


def test_evaluation_override_becomes_effective_suite_scoring_reference():
    experiment, suite = _definitions()
    experiment["spec"]["candidates"][0]["resourceOverrides"] = [
        {"kind": "Evaluation", "name": "quality", "version": "2.0.0"}
    ]
    arm_suite = _arm_suite(suite, experiment["spec"]["candidates"][0])
    assert arm_suite["spec"]["evaluationRefs"] == [
        {"kind": "Evaluation", "name": "quality", "version": "2.0.0"}
    ]


def test_builtin_aggregate_metric_wins_over_empty_custom_metric_mean():
    aggregate = {"passRate": 0.75, "cost": 1.2, "metrics": {"passRate": {"mean": None}, "cost": {"mean": None}}}
    assert _metric(aggregate, "passRate") == 0.75
    assert _metric(aggregate, "cost") == 1.2


def test_historical_override_back_to_source_version_clears_prior_override():
    source = {"workflowRef": {"kind": "Workflow", "name": "flow", "version": "1.0.0"},
              "provenance": {"resourceRefs": [{"kind": "Workflow", "name": "flow", "version": "1.0.0"},
                                               {"kind": "Prompt", "name": "planner", "version": "1.0.0"}]}}
    spec = {"workflowRef": source["workflowRef"], "evaluationRefs": [{"kind": "Evaluation", "name": "quality", "version": "1.0.0"}],
            "resourceOverrides": [{"kind": "Prompt", "name": "planner", "version": "2.0.0"},
                                  {"kind": "Prompt", "name": "planner", "version": "1.0.0"}]}
    related = [{"kind": "EvaluationResult", "evaluationRef": {"kind": "Evaluation", "name": "quality", "version": "1.0.0"}}]
    assert _historical_overrides(source, related, spec) == []


def test_historical_override_can_resolve_context_element_resource_provenance():
    source = {"workflowRef": {"kind": "Workflow", "name": "flow", "version": "1.0.0"},
              "provenance": {"resourceRefs": [{"kind": "Workflow", "name": "flow", "version": "1.0.0"}]}}
    related = [{"id": "context-1", "kind": "ContextPackage", "elements": [{
        "type": "knowledge", "provenance": {"resourceRefs": [
            {"kind": "KnowledgeBase", "name": "repo-knowledge", "version": "1.0.0"}
        ]}
    }]}]
    spec = {"workflowRef": source["workflowRef"], "evaluationRefs": [],
            "resourceOverrides": [{"kind": "KnowledgeBase", "name": "repo-knowledge", "version": "2.0.0"}]}
    assert _historical_overrides(source, related, spec) == ["KnowledgeBase:repo-knowledge:2.0.0"]


def test_fixture_resource_overrides_are_rejected_as_unexecuted():
    experiment, suite = _definitions()
    experiment["spec"]["candidates"][0]["resourceOverrides"] = [
        {"kind": "Prompt", "name": "planner", "version": "2.0.0"}
    ]
    with pytest.raises(ExperimentError, match="fixture cases do not execute"):
        run_experiment(experiment, suite, InMemoryRuntimeObjectStore())


def test_suite_content_is_part_of_experiment_result_identity():
    experiment, suite = _definitions()
    store = InMemoryRuntimeObjectStore()
    first = run_experiment(experiment, suite, store)
    changed = deepcopy(suite)
    changed["spec"]["cases"][0]["sourceRevision"] = "different-revision"
    second = run_experiment(experiment, changed, store)
    assert first["id"] != second["id"]
    assert first["evaluationSuiteDigest"] != second["evaluationSuiteDigest"]


def test_concurrent_experiment_caller_cannot_finalize_active_owner(monkeypatch):
    from threading import Event, Thread
    import aep.experiments as experiments_module

    experiment, suite = _definitions()
    store = InMemoryRuntimeObjectStore()
    entered, release = Event(), Event()
    actual_run_suite = experiments_module.run_suite

    def blocked_run_suite(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return actual_run_suite(*args, **kwargs)

    monkeypatch.setattr(experiments_module, "run_suite", blocked_run_suite)
    results = []

    def first_caller():
        results.append(run_experiment(experiment, suite, store))

    worker = Thread(target=first_caller)
    worker.start()
    assert entered.wait(3)
    with pytest.raises(ExperimentError, match="already running"):
        run_experiment(experiment, suite, store)
    release.set()
    worker.join(3)
    assert not worker.is_alive()
    assert results[0]["status"] == "SUCCEEDED"
