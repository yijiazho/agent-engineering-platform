from __future__ import annotations

from pathlib import Path

from aep.execution_checkout import CheckoutFailureClass, CheckoutProvisionError
from aep.dogfood_runtime import (
    DogfoodReconciliationConsumer,
    DogfoodReconciliationError,
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
