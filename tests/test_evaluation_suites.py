import pytest

from aep.evaluation_suites import EvaluationSuiteError, aggregate_results, load_suite, run_suite
from aep.runtime_store import InMemoryRuntimeObjectStore


def _suite():
    return {
        "apiVersion": "aep.dev/v1alpha1", "kind": "EvaluationSuite",
        "metadata": {"name": "regression", "version": "1.0.0"},
        "spec": {"workflowRef": {"kind": "Workflow", "name": "demo", "version": "1.0.0"},
                 "evaluationRefs": [{"kind": "Evaluation", "name": "acceptance", "version": "2.0.0"}],
                 "metrics": ["score"], "cases": [
                     {"id": "b", "kind": "fixture", "sourceRevision": "a" * 40,
                      "inputIdentity": "sha256:two", "expectedOutcome": "FAIL", "metrics": {"score": 0.0}},
                     {"id": "a", "kind": "fixture", "sourceRevision": "b" * 40,
                      "inputIdentity": "sha256:one", "expectedOutcome": "PASS", "metrics": {"score": 1.0},
                      "buildSuccess": True, "testSuccess": True},
                 ]}}


def test_mixed_cases_are_deterministic_durable_and_aggregated():
    store = InMemoryRuntimeObjectStore()
    suite = _suite()
    result = run_suite(suite, store, now=iter([f"2026-01-01T00:00:{i:02d}Z" for i in range(20)]).__next__)
    assert result["status"] == "FAILED"
    assert result["caseOrder"] == ["a", "b"]
    assert result["aggregate"]["passRate"] == 0.5
    assert result["aggregate"]["metrics"]["score"] == {"mean": 0.5, "count": 2}
    assert result["aggregate"]["buildSuccessRate"] == 1.0
    assert len(result["caseResults"]) == 2
    assert store.get(result["id"])["caseResults"] == result["caseResults"]


def test_failed_historical_case_does_not_erase_fixture_evidence():
    suite = _suite()
    suite["spec"]["cases"].append({"id": "historical", "kind": "historical", "workflowExecutionId": "source-1"})
    store = InMemoryRuntimeObjectStore()
    result = run_suite(suite, store, historical_runner=lambda _: (_ for _ in ()).throw(RuntimeError("replay unavailable")))
    assert [case["caseId"] for case in result["caseResults"]] == ["a", "b", "historical"]
    assert result["caseResults"][0]["outcome"] == "PASS"
    assert result["caseResults"][-1]["outcome"] == "FAIL"


def test_interrupted_suite_keeps_completed_case_and_resumes_remaining_case():
    class StopRun(BaseException):
        pass

    store = InMemoryRuntimeObjectStore()
    calls = iter(["2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z", "2026-01-01T00:00:02Z"])

    def interrupted_clock():
        try:
            return next(calls)
        except StopIteration:
            raise StopRun()

    with pytest.raises(StopRun):
        run_suite(_suite(), store, now=interrupted_clock)
    suite_id = next(key for key in store._objects if key.startswith("evaluationsuite-"))
    assert len(store.get(suite_id)["caseResults"]) == 1
    resumed = run_suite(_suite(), store)
    assert resumed["status"] == "FAILED"
    assert len(resumed["caseResults"]) == 2


def test_historical_case_records_replay_artifacts_usage_and_resource_provenance():
    suite = _suite()
    suite["spec"]["cases"] = [{"id": "history", "kind": "historical", "workflowExecutionId": "source"}]
    store = InMemoryRuntimeObjectStore()
    store.create({"id": "replay", "kind": "WorkflowExecution", "status": "SUCCEEDED", "repositoryRevision": "c" * 40,
                  "provenance": {"resourceRefs": []}}, deterministic_key="replay")
    store.create({"id": "model", "kind": "ModelInvocation", "workflowExecutionId": "replay",
                  "tokenUsage": {"inputTokens": 3, "outputTokens": 4, "totalTokens": 7}, "cost": 0.2}, deterministic_key="model")
    store.create({"id": "artifact", "kind": "GeneratedArtifact", "workflowExecutionId": "replay"}, deterministic_key="artifact")
    store.create({"id": "eval", "kind": "EvaluationResult", "workflowExecutionId": "replay",
                  "evaluationRef": {"kind": "Evaluation", "name": "acceptance", "version": "1.0.0"}}, deterministic_key="eval")
    result = run_suite(suite, store, historical_runner=lambda _: "replay")
    evidence = result["caseResults"][0]["evidence"]
    assert evidence["sourceRevision"] == "c" * 40
    assert evidence["modelInvocationCount"] == 1
    assert evidence["tokenUsage"]["totalTokens"] == 7
    assert evidence["cost"] == 0.2
    assert evidence["generatedArtifactIds"] == ["artifact"]
    assert len(evidence["evaluationResults"]) == 1


def test_suite_requires_immutable_versioned_references():
    suite = _suite()
    suite["spec"]["workflowRef"]["version"] = "latest"
    # Loader applies the same contract as suite files.
    from aep.evaluation_suites import _validate_ref
    with pytest.raises(EvaluationSuiteError):
        _validate_ref(suite["spec"]["workflowRef"], "Workflow")


def test_aggregate_latency_and_cost_availability():
    result = aggregate_results([{"outcome": "PASS", "startedAt": "2026-01-01T00:00:00Z", "completedAt": "2026-01-01T00:00:02Z",
                                 "evidence": {"cost": 1.5, "metrics": {"quality": 0.8}}}])
    assert result["latencyMilliseconds"] == 2000
    assert result["cost"] == 1.5 and result["costAvailable"]
    assert result["modelInvocationCount"] == 0
