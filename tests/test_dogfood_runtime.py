from __future__ import annotations

from pathlib import Path

from aep.dogfood_runtime import (
    DogfoodReconciliationConsumer,
    DogfoodReconciliationError,
)
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
