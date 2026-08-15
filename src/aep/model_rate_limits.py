"""Thread-safe, process-local admission control for Model provider requests."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    """One deterministic reservation against a provider quota scope."""

    admitted: bool
    delay_ms: int
    eligible_at: float
    estimated_input_tokens: int
    reserved_tokens: int


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
    ) -> None:
        if requests_per_minute < 1 or tokens_per_minute < 1:
            raise ValueError("rate-limit capacities must be positive")
        self.requests_per_minute = requests_per_minute
        self.tokens_per_minute = tokens_per_minute
        self._lock = Lock()
        self._next_request_at = 0.0
        self._next_token_at = 0.0
        self._blocked_until = 0.0

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
            eligible_at = max(
                now,
                self._next_request_at,
                self._next_token_at,
                self._blocked_until,
            )
            delay_ms = max(0, round((eligible_at - now) * 1000))
            admitted = eligible_at < deadline
            if admitted:
                self._next_request_at = eligible_at + 60 / self.requests_per_minute
                self._next_token_at = (
                    eligible_at + demand * 60 / self.tokens_per_minute
                )
        return AdmissionDecision(
            admitted=admitted,
            delay_ms=delay_ms,
            eligible_at=eligible_at,
            estimated_input_tokens=estimated_input_tokens,
            reserved_tokens=demand,
        )

    def observe_success(
        self, *, reserved_tokens: int, actual_input_tokens: int, actual_output_tokens: int
    ) -> None:
        """Reconcile conservative reservation demand with successful usage."""

        actual = max(1, actual_input_tokens + actual_output_tokens)
        credit = max(0, reserved_tokens - actual) * 60 / self.tokens_per_minute
        if not credit:
            return
        with self._lock:
            self._next_token_at = max(0.0, self._next_token_at - credit)

    def observe_throttle(self, *, eligible_at: float) -> None:
        """Apply a provider retry/reset hint to every request in this scope."""

        with self._lock:
            self._blocked_until = max(self._blocked_until, eligible_at)
