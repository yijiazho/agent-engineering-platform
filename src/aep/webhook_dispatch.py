"""Durable provider-neutral reconciliation dispatch for webhook Events."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class DispatchAcceptance:
    """Result of atomically accepting an Event and its reconciliation request."""

    accepted: bool
    event: Mapping[str, Any]


class ReconciliationDispatcher(Protocol):
    """Atomic persistence boundary for Event acceptance and reconciliation."""

    def submit(
        self, event: Mapping[str, Any], *, trace_id: str
    ) -> DispatchAcceptance:
        """Persist one Event and durable reconciliation request, or return its prior Event."""


class SQLiteReconciliationDispatcher:
    """SQLite-backed atomic Event inbox and reconciliation outbox.

    A unique deduplication key and its outbox record are committed in one
    transaction. SQLite's write transaction serializes controller instances
    sharing the same state volume, and committed rows survive restarts.
    """

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path.resolve()
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS github_webhook_events (
                    deduplication_key TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL UNIQUE,
                    event_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS reconciliation_outbox (
                    event_id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('PENDING', 'COMPLETED')),
                    FOREIGN KEY (event_id) REFERENCES github_webhook_events(event_id)
                )
                """
            )

    def submit(
        self, event: Mapping[str, Any], *, trace_id: str
    ) -> DispatchAcceptance:
        event_id = _required_text(event, "id")
        deduplication_key = _required_text(event, "deduplicationKey")
        if not trace_id.strip():
            raise ValueError("trace_id must not be empty")
        event_json = json.dumps(event, sort_keys=True, separators=(",", ":"))
        request_json = json.dumps(
            {"event": event, "eventId": event_id, "traceId": trace_id},
            sort_keys=True,
            separators=(",", ":"),
        )

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                "SELECT event_json FROM github_webhook_events "
                "WHERE deduplication_key = ?",
                (deduplication_key,),
            ).fetchone()
            if prior is not None:
                connection.commit()
                return DispatchAcceptance(False, json.loads(prior[0]))
            connection.execute(
                "INSERT INTO github_webhook_events "
                "(deduplication_key, event_id, event_json) VALUES (?, ?, ?)",
                (deduplication_key, event_id, event_json),
            )
            connection.execute(
                "INSERT INTO reconciliation_outbox "
                "(event_id, trace_id, request_json, status) VALUES (?, ?, ?, 'PENDING')",
                (event_id, trace_id, request_json),
            )
            connection.commit()
            return DispatchAcceptance(True, json.loads(event_json))
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def pending_requests(self) -> tuple[Mapping[str, Any], ...]:
        """Return durable pending work in deterministic order for a consumer."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT request_json FROM reconciliation_outbox "
                "WHERE status = 'PENDING' ORDER BY event_id"
            ).fetchall()
        return tuple(json.loads(row[0]) for row in rows)

    def mark_completed(self, event_id: str) -> None:
        """Mark one consumed outbox request complete without changing its identity."""
        if not event_id.strip():
            raise ValueError("event_id must not be empty")
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE reconciliation_outbox SET status = 'COMPLETED' "
                "WHERE event_id = ?",
                (event_id,),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"reconciliation request {event_id!r} was not found")

    def outbox_count(self) -> int:
        """Return the number of durable reconciliation identities."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM reconciliation_outbox"
            ).fetchone()
        return int(row[0])

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=5)
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection


def _required_text(value: Mapping[str, Any], field: str) -> str:
    candidate = value.get(field)
    if not isinstance(candidate, str) or not candidate.strip():
        raise ValueError(f"event.{field} must be a non-empty string")
    return candidate.strip()
