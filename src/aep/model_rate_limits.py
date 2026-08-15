"""Thread-safe, process-local admission control for Model provider requests."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
import os
from pathlib import Path
from threading import Lock
import time


class CoordinatorStateError(RuntimeError):
    """Raised when durable quota state cannot be restored or checkpointed."""


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    """One deterministic reservation against a provider quota scope."""

    admitted: bool
    delay_ms: int
    eligible_at: float
    estimated_input_tokens: int
    reserved_tokens: int
    reservation_id: int | None = None


class ProcessLocalModelAdmissionCoordinator:
    """Pace requests and token demand within one explicitly single-worker process.

    Reservations are serialized by a lock. Each request advances both a request
    clock and a token-demand clock, which avoids dispatching concurrently-ready
    work as a burst. The coordinator deliberately contains no credential identity
    in evidence; callers share an instance only inside the same trusted scope.
    """

    def __init__(
        self,
        *,
        requests_per_minute: int,
        tokens_per_minute: int,
        state_path: Path | None = None,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        if requests_per_minute < 1 or tokens_per_minute < 1:
            raise ValueError("rate-limit capacities must be positive")
        self.requests_per_minute = requests_per_minute
        self.tokens_per_minute = tokens_per_minute
        self._state_path = state_path
        self._wall_clock = wall_clock
        self._lock = Lock()
        self._next_request_at = 0.0
        self._next_token_at = 0.0
        self._blocked_until = 0.0
        self._restored = False
        self._latest_reservation_id = 0

    def admit(
        self,
        *,
        now: float,
        deadline: float,
        estimated_input_tokens: int,
        output_token_allowance: int,
    ) -> AdmissionDecision:
        if estimated_input_tokens < 0 or output_token_allowance < 0:
            raise ValueError("token estimates must not be negative")
        demand = max(1, estimated_input_tokens + output_token_allowance)
        with self._lock:
            self._restore(now)
            eligible_at = max(
                now,
                self._next_request_at,
                self._next_token_at,
                self._blocked_until,
            )
            delay_ms = max(0, round((eligible_at - now) * 1000))
            admitted = eligible_at < deadline
            if admitted:
                self._latest_reservation_id += 1
                reservation_id = self._latest_reservation_id
                self._next_request_at = eligible_at + 60 / self.requests_per_minute
                self._next_token_at = (
                    eligible_at + demand * 60 / self.tokens_per_minute
                )
                self._persist(now)
            else:
                reservation_id = None
        return AdmissionDecision(
            admitted=admitted,
            delay_ms=delay_ms,
            eligible_at=eligible_at,
            estimated_input_tokens=estimated_input_tokens,
            reserved_tokens=demand,
            reservation_id=reservation_id,
        )

    def revalidate(self, *, now: float, deadline: float) -> AdmissionDecision:
        """Recheck a reserved dispatch against newer provider throttle state."""

        with self._lock:
            self._restore(now)
            eligible_at = max(now, self._blocked_until)
            delay_ms = max(0, round((eligible_at - now) * 1000))
        return AdmissionDecision(
            admitted=eligible_at < deadline,
            delay_ms=delay_ms,
            eligible_at=eligible_at,
            estimated_input_tokens=0,
            reserved_tokens=0,
        )

    def observe_success(
        self,
        *,
        now: float,
        reserved_tokens: int,
        actual_input_tokens: int,
        actual_output_tokens: int,
        reservation_id: int | None,
    ) -> None:
        """Reconcile conservative reservation demand with successful usage."""

        actual = max(1, actual_input_tokens + actual_output_tokens)
        credit = max(0, reserved_tokens - actual) * 60 / self.tokens_per_minute
        if not credit or reservation_id is None:
            return
        with self._lock:
            self._restore(now)
            # A later reservation has already fixed the shared token tail. Moving
            # that tail backwards would let a subsequent admission overlap it.
            if reservation_id != self._latest_reservation_id:
                return
            self._next_token_at = max(0.0, self._next_token_at - credit)
            self._persist(now)

    def observe_throttle(self, *, now: float, eligible_at: float) -> None:
        """Apply a provider retry/reset hint to every request in this scope."""

        with self._lock:
            self._restore(now)
            self._blocked_until = max(self._blocked_until, eligible_at)
            self._persist(now)

    def _restore(self, now: float) -> None:
        if self._restored:
            return
        if self._state_path is None or not self._state_path.exists():
            self._restored = True
            return
        try:
            state = json.loads(self._state_path.read_text(encoding="utf-8"))
            if not isinstance(state, dict) or state.get("version") != 1:
                raise ValueError("unsupported coordinator state")
            wall_now = self._wall_clock()
            deadlines = {
                key: float(state[key])
                for key in ("nextRequestAt", "nextTokenAt", "blockedUntil")
            }
            if any(value < 0 for value in deadlines.values()):
                raise ValueError("negative coordinator deadline")
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise CoordinatorStateError(
                "Model rate-limit coordinator state is unavailable"
            ) from error
        self._next_request_at = now + max(0.0, deadlines["nextRequestAt"] - wall_now)
        self._next_token_at = now + max(0.0, deadlines["nextTokenAt"] - wall_now)
        self._blocked_until = now + max(0.0, deadlines["blockedUntil"] - wall_now)
        self._restored = True

    def _persist(self, now: float) -> None:
        if self._state_path is None:
            return
        wall_now = self._wall_clock()
        state = {
            "version": 1,
            "nextRequestAt": wall_now + max(0.0, self._next_request_at - now),
            "nextTokenAt": wall_now + max(0.0, self._next_token_at - now),
            "blockedUntil": wall_now + max(0.0, self._blocked_until - now),
        }
        temporary = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(state, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(temporary, self._state_path)
        except OSError as error:
            raise CoordinatorStateError(
                "Model rate-limit coordinator state cannot be checkpointed"
            ) from error
