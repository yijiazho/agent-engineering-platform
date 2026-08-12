from __future__ import annotations

from pathlib import Path
import logging

import pytest

from aep.execution_checkout import CheckoutFailureClass, CheckoutProvisionError
from aep.dogfood_runtime import (
    DogfoodReconciliationConsumer,
    DogfoodReconciliationError,
    DogfoodWorkflowRunner,
    _reconciliation_revision,
)
from aep.runtime_store import DurableJsonRuntimeObjectStore
from aep.webhook_dispatch import SQLiteReconciliationDispatcher


class RecordingRunner:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.requests = []

    def run(self, request):
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        return "workflowexecution-test"


def test_workflow_runner_preserves_resource_checkout_crlf_policy(
    tmp_path: Path, monkeypatch
) -> None:
    captured: dict[str, object] = {}

    class VerificationComplete(Exception):
        pass

    def verify(repository_root, revision, *, require_detached, autocrlf):
        captured.update(
            repository_root=repository_root,
            revision=revision,
            require_detached=require_detached,
            autocrlf=autocrlf,
        )
        raise VerificationComplete

    monkeypatch.setattr("aep.dogfood_runtime.verify_resource_checkout", verify)
    environment = {
        "AEP_STATE_ROOT": str(tmp_path / "state"),
        "AEP_REPOSITORY_ROOT": str(tmp_path / "resources"),
        "AEP_RESOURCE_REVISION": "a" * 40,
        "AEP_RESOURCE_SCHEMA_ROOT": str(tmp_path / "schemas"),
        "AEP_RESOURCE_GIT_AUTOCRLF": "true",
    }

    with pytest.raises(VerificationComplete):
        DogfoodWorkflowRunner(environment)

    assert captured == {
        "repository_root": Path(environment["AEP_REPOSITORY_ROOT"]),
        "revision": "a" * 40,
        "require_detached": True,
        "autocrlf": "true",
    }


def event() -> dict[str, object]:
    return {
        "id": "event-dogfood",
        "deduplicationKey": "github:delivery:dogfood",
    }


def test_consumer_retires_outbox_only_after_runner_completes(tmp_path: Path) -> None:
    dispatcher = SQLiteReconciliationDispatcher(tmp_path / "webhook.sqlite3")
    dispatcher.submit(event(), trace_id="trace-dogfood")
    runner = RecordingRunner()
    consumer = DogfoodReconciliationConsumer(dispatcher, runner)

    assert consumer.run_once() == 1

    assert len(runner.requests) == 1
    assert dispatcher.pending_requests() == ()
    assert dispatcher.failure("event-dogfood") is None


def test_configuration_failure_is_persisted_and_not_hot_retried(tmp_path: Path) -> None:
    dispatcher = SQLiteReconciliationDispatcher(tmp_path / "webhook.sqlite3")
    dispatcher.submit(event(), trace_id="trace-dogfood")
    runner = RecordingRunner(DogfoodReconciliationError("unsafe details"))
    consumer = DogfoodReconciliationConsumer(dispatcher, runner)

    assert consumer.run_once() == 0

    assert dispatcher.pending_requests() == ()
    assert dispatcher.failure("event-dogfood")["failureClass"] == "CONFIGURATION"
    assert dispatcher.failure("event-dogfood")["message"] == (
        "dogfood reconciliation failed configuration checks"
    )


def test_recoverable_failure_logs_only_safe_diagnostic_fields(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    dispatcher = SQLiteReconciliationDispatcher(tmp_path / "webhook.sqlite3")
    dispatcher.submit(event(), trace_id="trace-dogfood")
    runner = RecordingRunner(RuntimeError("provider-secret-must-not-appear"))
    consumer = DogfoodReconciliationConsumer(dispatcher, runner)

    with caplog.at_level(logging.WARNING, logger="aep.dogfood_runtime"):
        assert consumer.run_once() == 0

    assert len(caplog.messages) == 1
    assert "event_id=event-dogfood" in caplog.messages[0]
    assert "failure_class=RECOVERABLE" in caplog.messages[0]
    assert "failure_code=reconciliation_failed" in caplog.messages[0]
    assert "exception_type=RuntimeError" in caplog.messages[0]
    assert "provider-secret-must-not-appear" not in caplog.text


def test_recoverable_failure_logs_only_first_occurrence_and_transitions(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    dispatcher = SQLiteReconciliationDispatcher(tmp_path / "webhook.sqlite3")
    dispatcher.submit(event(), trace_id="trace-dogfood")
    runner = RecordingRunner(RuntimeError("first unsafe provider detail"))
    consumer = DogfoodReconciliationConsumer(dispatcher, runner)

    with caplog.at_level(logging.WARNING, logger="aep.dogfood_runtime"):
        assert consumer.run_once() == 0
        assert consumer.run_once() == 0
        runner.failure = OSError("second unsafe provider detail")
        assert consumer.run_once() == 0
        assert consumer.run_once() == 0

    assert len(caplog.messages) == 2
    assert "exception_type=RuntimeError" in caplog.messages[0]
    assert "exception_type=OSError" in caplog.messages[1]
    assert "unsafe provider detail" not in caplog.text
    assert len(runner.requests) == 4


def test_reported_failure_state_is_cleared_when_event_leaves_pending_outbox(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    dispatcher = SQLiteReconciliationDispatcher(tmp_path / "webhook.sqlite3")
    dispatcher.submit(event(), trace_id="trace-dogfood")
    runner = RecordingRunner(RuntimeError("unsafe details"))
    consumer = DogfoodReconciliationConsumer(dispatcher, runner)

    with caplog.at_level(logging.WARNING, logger="aep.dogfood_runtime"):
        assert consumer.run_once() == 0
        runner.failure = None
        assert consumer.run_once() == 1
        assert consumer.run_once() == 0

    assert len(caplog.messages) == 1
    assert consumer._reported_failures == {}


def test_reconciliation_reuses_recorded_revision_and_skips_terminal_execution(
    tmp_path: Path,
) -> None:
    store = DurableJsonRuntimeObjectStore(tmp_path / "runtime.json")
    execution_id = "workflowexecution-existing"
    store.create(
        {
            "id": execution_id,
            "kind": "WorkflowExecution",
            "status": "RUNNING",
            "repositoryRevision": "a" * 40,
        },
        deterministic_key="execution-key",
    )
    resolver_called = False

    def resolver() -> str:
        nonlocal resolver_called
        resolver_called = True
        return "b" * 40

    assert _reconciliation_revision(store, execution_id, resolver) == "a" * 40
    assert resolver_called is False
    store.update_status(execution_id, "SUCCEEDED")
    assert _reconciliation_revision(store, execution_id, resolver) is None
    assert resolver_called is False


def test_checkout_configuration_failure_is_terminal(tmp_path: Path) -> None:
    dispatcher = SQLiteReconciliationDispatcher(tmp_path / "webhook.sqlite3")
    dispatcher.submit(event(), trace_id="trace-dogfood")
    runner = RecordingRunner(
        CheckoutProvisionError(
            CheckoutFailureClass.CONFIGURATION,
            "stale_revision",
            "provider details must not be persisted",
        )
    )
    consumer = DogfoodReconciliationConsumer(dispatcher, runner)

    assert consumer.run_once() == 0
    assert dispatcher.pending_requests() == ()
    assert dispatcher.failure("event-dogfood")["failureClass"] == "CONFIGURATION"
    assert dispatcher.failure("event-dogfood")["message"] == (
        "dogfood checkout provisioning failed configuration checks"
    )


def test_polling_failure_does_not_kill_consumer_and_is_visible(tmp_path: Path) -> None:
    dispatcher = SQLiteReconciliationDispatcher(tmp_path / "webhook.sqlite3")
    consumer = DogfoodReconciliationConsumer(dispatcher, RecordingRunner(), poll_seconds=0)
    calls = 0

    def failing_poll() -> None:
        nonlocal calls
        calls += 1
        consumer._stop.set()
        raise OSError("database is locked")

    consumer.run_once = failing_poll  # type: ignore[method-assign]
    consumer._run()

    assert calls == 1
    assert consumer.liveness()["status"] == "degraded"
    assert "database is locked" in consumer.liveness()["lastPollError"]
