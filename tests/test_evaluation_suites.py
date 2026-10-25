import pytest
from threading import Event, Lock, Thread

from aep.evaluation_suites import EvaluationSuiteError, aggregate_results, load_suite, run_suite
from aep.runtime_store import DurableJsonRuntimeObjectStore, InMemoryRuntimeObjectStore


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
    store.create({"id": "source", "kind": "WorkflowExecution", "status": "SUCCEEDED", "repositoryRevision": "c" * 40,
                  "workflowRef": suite["spec"]["workflowRef"],
                  "provenance": {"resourceRefs": [suite["spec"]["workflowRef"],
                                                    {"kind": "Evaluation", "name": "acceptance", "version": "1.0.0"}]}},
                 deterministic_key="source")
    store.create({"id": "replay", "kind": "WorkflowExecution", "status": "SUCCEEDED", "repositoryRevision": "c" * 40,
                  "provenance": {"resourceRefs": []}}, deterministic_key="replay")
    store.create({"id": "model", "kind": "ModelInvocation", "workflowExecutionId": "replay",
                  "tokenUsage": {"input": 3, "output": 4}, "cost": 0.2}, deterministic_key="model")
    store.create({"id": "artifact", "kind": "GeneratedArtifact", "workflowExecutionId": "replay"}, deterministic_key="artifact")
    store.create({"id": "eval", "kind": "EvaluationResult", "workflowExecutionId": "replay",
                  "evaluationRef": {"kind": "Evaluation", "name": "acceptance", "version": "1.0.0"}}, deterministic_key="eval")
    received = []
    result = run_suite(suite, store, historical_runner=lambda _, overrides: (received.extend(overrides) or "replay"))
    evidence = result["caseResults"][0]["evidence"]
    assert evidence["sourceRevision"] == "c" * 40
    assert evidence["modelInvocationCount"] == 1
    assert evidence["tokenUsage"]["totalTokens"] == 7
    assert received == ["Evaluation:acceptance:2.0.0"]
    assert evidence["cost"] == 0.2
    assert evidence["generatedArtifactIds"] == ["artifact"]
    assert len(evidence["evaluationResults"]) == 1


def test_historical_workflow_mismatch_is_rejected_before_replay():
    suite = _suite()
    suite["spec"]["cases"] = [{"id": "history", "kind": "historical", "workflowExecutionId": "source"}]
    store = InMemoryRuntimeObjectStore()
    store.create({"id": "source", "kind": "WorkflowExecution", "workflowRef": {"kind": "Workflow", "name": "other", "version": "1.0.0"},
                  "provenance": {"resourceRefs": []}}, deterministic_key="source")
    called = False

    def runner(_, __):
        nonlocal called
        called = True
        return "replay"

    result = run_suite(suite, store, historical_runner=runner)
    assert result["caseResults"][0]["outcome"] == "FAIL"
    assert "Workflow does not match" in result["caseResults"][0]["failure"]
    assert not called


def test_publication_suppression_does_not_fail_a_passing_regression_case():
    suite = _suite()
    suite["spec"]["cases"] = [{"id": "history", "kind": "historical", "workflowExecutionId": "source"}]
    store = InMemoryRuntimeObjectStore()
    workflow, evaluation = suite["spec"]["workflowRef"], suite["spec"]["evaluationRefs"][0]
    store.create({"id": "source", "kind": "WorkflowExecution", "workflowRef": workflow,
                  "provenance": {"resourceRefs": [workflow, evaluation]}}, deterministic_key="source")

    def runner(_, __):
        store.create({"id": "replay", "kind": "WorkflowExecution", "status": "FAILED",
                      "repositoryRevision": "f" * 40,
                      "replayProvenance": {"publicationMode": "disabled"},
                      "provenance": {"resourceRefs": [workflow, evaluation]}}, deterministic_key="replay")
        store.create({"id": "evaluation-result", "kind": "EvaluationResult",
                      "workflowExecutionId": "replay", "status": "SUCCEEDED", "outcome": "PASS",
                      "evaluationRef": evaluation, "metrics": {"passed": 1}}, deterministic_key="evaluation-result")
        store.create({"id": "analysis-attempt-1", "kind": "TaskExecution", "workflowExecutionId": "replay",
                      "taskRef": {"kind": "Task", "name": "analyze-issue", "version": "1.0.0"},
                      "attempt": 1, "status": "FAILED", "failure": {"class": "RECOVERABLE", "message": "retry"}},
                     deterministic_key="analysis-attempt-1")
        store.create({"id": "analysis-attempt-2", "kind": "TaskExecution", "workflowExecutionId": "replay",
                      "taskRef": {"kind": "Task", "name": "analyze-issue", "version": "1.0.0"},
                      "attempt": 2, "status": "SUCCEEDED"}, deterministic_key="analysis-attempt-2")
        store.create({"id": "publication-task", "kind": "TaskExecution", "workflowExecutionId": "replay",
                      "taskRef": {"kind": "Task", "name": "create-pull-request", "version": "1.0.0"},
                      "status": "FAILED", "failure": {"class": "POLICY", "message": "publication is disabled for replay"}},
                     deterministic_key="publication-task")
        return "replay"

    result = run_suite(suite, store, historical_runner=runner)
    assert result["status"] == "SUCCEEDED"
    assert result["caseResults"][0]["outcome"] == "PASS"
    attempts = result["caseResults"][0]["evidence"]["taskExecutions"]
    assert [item["id"] for item in attempts if item["id"].startswith("analysis-")] == [
        "analysis-attempt-1", "analysis-attempt-2"
    ]


def test_superseded_evaluation_failure_is_excluded_from_scoring_and_success_rates():
    from aep.evaluation_suites import _execution_evidence, _historical_case_passed
    evaluation = {"kind": "Evaluation", "name": "acceptance", "version": "2.0.0"}
    related = [
        {"id": "run-1", "kind": "TaskExecution", "taskRef": {"name": "run-validation"}, "attempt": 1},
        {"id": "run-2", "kind": "TaskExecution", "taskRef": {"name": "run-validation"}, "attempt": 2},
        {"id": "failed-test", "kind": "EvaluationResult", "taskExecutionId": "run-1",
         "evaluationRef": evaluation, "status": "FAILED", "outcome": "FAIL",
         "evidence": {"evaluationType": "test"}, "metrics": {"durationMs": 100}},
        {"id": "passed-test", "kind": "EvaluationResult", "taskExecutionId": "run-2",
         "evaluationRef": evaluation, "status": "SUCCEEDED", "outcome": "PASS",
         "evidence": {"evaluationType": "test"}, "metrics": {"durationMs": 20}},
    ]
    assert _historical_case_passed({"status": "SUCCEEDED"}, related, [evaluation]) == (True, None)
    evidence = _execution_evidence({"status": "SUCCEEDED"}, related, "source")
    assert evidence["testSuccess"] is True
    assert evidence["metrics"] == {"durationMs": 20}
    assert [item["id"] for item in evidence["evaluationResults"]] == ["failed-test", "passed-test"]


def test_direct_suite_runner_rejects_floating_resource_versions_before_persisting():
    suite, store = _suite(), InMemoryRuntimeObjectStore()
    suite["spec"]["workflowRef"]["version"] = "latest"
    with pytest.raises(EvaluationSuiteError, match="immutable Workflow"):
        run_suite(suite, store)
    assert not store._objects


def test_direct_suite_runner_rejects_duplicate_case_ids_before_persisting():
    suite, store = _suite(), InMemoryRuntimeObjectStore()
    suite["spec"]["cases"][1]["id"] = suite["spec"]["cases"][0]["id"]
    with pytest.raises(EvaluationSuiteError, match="unique"):
        run_suite(suite, store)
    assert not store._objects


def test_completed_suite_rerun_returns_existing_terminal_evidence():
    store, suite = InMemoryRuntimeObjectStore(), _suite()
    first = run_suite(suite, store)
    again = run_suite(suite, store)
    assert again == first


def test_concurrent_suite_invocation_does_not_dispatch_duplicate_case(tmp_path):
    suite = _suite()
    suite["spec"]["cases"] = [{"id": "history", "kind": "historical", "workflowExecutionId": "source"}]
    checkpoint = tmp_path / "runtime.json"
    store = DurableJsonRuntimeObjectStore(checkpoint)
    workflow = suite["spec"]["workflowRef"]
    evaluations = suite["spec"]["evaluationRefs"]
    store.create({"id": "source", "kind": "WorkflowExecution", "workflowRef": workflow,
                  "provenance": {"resourceRefs": [workflow, *evaluations]}}, deterministic_key="source")
    entered, release = Event(), Event()
    calls = []

    def runner(_, __):
        calls.append(1)
        entered.set()
        assert release.wait(3)
        store.create({"id": "replay", "kind": "WorkflowExecution", "status": "SUCCEEDED", "repositoryRevision": "d" * 40,
                      "provenance": {"resourceRefs": []}}, deterministic_key="replay")
        return "replay"

    failure = []
    worker = Thread(target=lambda: run_suite(suite, store, historical_runner=runner))
    worker.start()
    assert entered.wait(3)
    with pytest.raises(EvaluationSuiteError, match="already running"):
        run_suite(suite, DurableJsonRuntimeObjectStore(checkpoint), historical_runner=runner)
    release.set()
    worker.join(3)
    assert not worker.is_alive()
    assert len(calls) == 1


def test_suite_reloads_checkpoint_after_waiting_to_acquire_lease(tmp_path):
    suite = _suite()
    suite["spec"]["cases"] = [{"id": "history", "kind": "historical", "workflowExecutionId": "source"}]
    store = DurableJsonRuntimeObjectStore(tmp_path / "runtime.json")
    workflow, evaluations = suite["spec"]["workflowRef"], suite["spec"]["evaluationRefs"]
    store.create({"id": "source", "kind": "WorkflowExecution", "workflowRef": workflow,
                  "provenance": {"resourceRefs": [workflow, *evaluations]}}, deterministic_key="source")
    replay_entered, finish_first = Event(), Event()
    second_waiting, allow_second = Event(), Event()
    original_acquire = store.acquire_lease
    acquire_count = 0
    acquire_lock = Lock()

    def delayed_second_acquire(key, owner_id, *, now, expires_at):
        nonlocal acquire_count
        with acquire_lock:
            acquire_count += 1
            call_number = acquire_count
        if call_number == 2:
            second_waiting.set()
            assert allow_second.wait(3)
        return original_acquire(key, owner_id, now=now, expires_at=expires_at)

    store.acquire_lease = delayed_second_acquire
    calls = []

    def runner(_, __):
        calls.append(1)
        replay_entered.set()
        if len(calls) == 1:
            assert finish_first.wait(3)
        store.create({"id": "replay", "kind": "WorkflowExecution", "status": "SUCCEEDED",
                      "repositoryRevision": "e" * 40, "provenance": {"resourceRefs": []}},
                     deterministic_key="replay")
        store.create({"id": "replay-evaluation", "kind": "EvaluationResult", "workflowExecutionId": "replay",
                      "status": "SUCCEEDED", "outcome": "PASS", "evaluationRef": evaluations[0]},
                     deterministic_key="replay-evaluation")
        return "replay"

    outcomes = []
    failures = []

    def invoke():
        try:
            outcomes.append(run_suite(suite, store, historical_runner=runner))
        except Exception as error:
            failures.append(error)

    first = Thread(target=invoke)
    first.start()
    assert replay_entered.wait(3)
    second = Thread(target=invoke)
    second.start()
    assert second_waiting.wait(3)
    finish_first.set()
    first.join(3)
    assert not first.is_alive()
    allow_second.set()
    second.join(3)
    assert not second.is_alive()
    assert not failures
    assert len(outcomes) == 2
    assert all(result["status"] == "SUCCEEDED" for result in outcomes)
    assert len(calls) == 1


def test_suite_optional_fields_are_validated_before_runtime_creation(tmp_path):
    suite = _suite()
    suite["spec"]["cases"][0]["metrics"] = None
    path = tmp_path / "invalid-suite.yaml"
    import yaml
    path.write_text(yaml.safe_dump(suite), encoding="utf-8")
    with pytest.raises(EvaluationSuiteError):
        load_suite(path)


def test_build_test_and_evaluation_metrics_use_evaluation_results_only():
    from aep.evaluation_suites import _evaluation_metrics, _execution_evidence
    records = [
        {"kind": "EvaluationResult", "outcome": "PASS", "metrics": {"durationMs": 20},
         "evidence": {"evaluationType": "build"}},
        {"kind": "EvaluationResult", "outcome": "FAIL", "metrics": {"durationMs": 30},
         "evidence": {"evaluationType": "test"}},
        {"kind": "ToolInvocation", "metrics": {"durationMs": 999}},
    ]
    assert _evaluation_metrics(records) == {"durationMs": 50}
    evidence = _execution_evidence({"repositoryRevision": "x", "provenance": {}}, records, "source")
    assert evidence["buildSuccess"] is True
    assert evidence["testSuccess"] is False
    assert evidence["metrics"] == {"durationMs": 50}


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
