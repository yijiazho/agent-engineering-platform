from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import sqlite3
from pathlib import Path

import pytest

from aep.webhook_dispatch import SQLiteReconciliationDispatcher


def event() -> dict[str, object]:
    return {
        "id": "event-durable-038",
        "deduplicationKey": "github:delivery:durable-038",
        "source": "github",
        "type": "github.issue.created",
    }


def test_replay_after_dispatcher_restart_returns_prior_event_and_one_outbox(
    tmp_path: Path,
) -> None:
    database = tmp_path / "shared/github-webhook.sqlite3"
    first_process = SQLiteReconciliationDispatcher(database)

    accepted = first_process.submit(event(), trace_id="trace-durable-038")
    restarted_process = SQLiteReconciliationDispatcher(database)
    replay = restarted_process.submit(event(), trace_id="different-retry-trace")

    assert accepted.accepted is True
    assert replay.accepted is False
    assert replay.event == accepted.event
    assert restarted_process.pending_requests() == (
        {
            "event": event(),
            "eventId": "event-durable-038",
            "traceId": "trace-durable-038",
        },
    )


def test_two_controller_instances_atomically_accept_and_enqueue_once(
    tmp_path: Path,
) -> None:
    database = tmp_path / "shared/github-webhook.sqlite3"
    first = SQLiteReconciliationDispatcher(database)
    second = SQLiteReconciliationDispatcher(database)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda dispatcher: dispatcher.submit(
                    event(), trace_id="trace-durable-038"
                ),
                (first, second),
            )
        )

    assert sum(result.accepted for result in results) == 1
    assert {result.event["id"] for result in results} == {"event-durable-038"}
    assert len(first.pending_requests()) == 1


def test_completed_dispatch_is_not_recreated_by_replay(tmp_path: Path) -> None:
    database = tmp_path / "shared/github-webhook.sqlite3"
    dispatcher = SQLiteReconciliationDispatcher(database)
    accepted = dispatcher.submit(event(), trace_id="trace-durable-038")
    dispatcher.mark_completed(str(accepted.event["id"]))

    replay = SQLiteReconciliationDispatcher(database).submit(
        event(), trace_id="trace-replay-038"
    )

    assert replay.accepted is False
    assert dispatcher.pending_requests() == ()
    assert dispatcher.outbox_count() == 1


def test_event_insert_rolls_back_when_atomic_outbox_persistence_fails(
    tmp_path: Path,
) -> None:
    database = tmp_path / "shared/github-webhook.sqlite3"
    dispatcher = SQLiteReconciliationDispatcher(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_outbox BEFORE INSERT ON reconciliation_outbox
            BEGIN
                SELECT RAISE(ABORT, 'forced outbox failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="forced outbox failure"):
        dispatcher.submit(event(), trace_id="trace-durable-038")

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM github_webhook_events"
        ).fetchone() == (0,)
        connection.execute("DROP TRIGGER fail_outbox")

    retry = dispatcher.submit(event(), trace_id="trace-durable-038")

    assert retry.accepted is True
    assert len(dispatcher.pending_requests()) == 1
