"""Persistence boundary for AEP runtime objects."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timezone
from threading import RLock
from types import MappingProxyType
from typing import Any, Final


RuntimeObject = Mapping[str, Any]

TERMINAL_STATUSES: Final = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})


class RuntimeStoreError(Exception):
    """Base class for runtime store errors."""


class RuntimeObjectNotFoundError(RuntimeStoreError):
    """Raised when a requested runtime object does not exist."""


class RuntimeObjectAlreadyExistsError(RuntimeStoreError):
    """Raised when an object id is reused with a different deterministic key."""


class ImmutableRuntimeObjectError(RuntimeStoreError):
    """Raised when completed runtime evidence would be changed."""


class StatusConflictError(RuntimeStoreError):
    """Raised when an optimistic status update loses a race."""


class RuntimeObjectStore(ABC):
    """Storage contract for runtime state, separate from Git Resources."""

    @abstractmethod
    def create(self, runtime_object: RuntimeObject, *, deterministic_key: str) -> RuntimeObject:
        """Create an object, or return the prior object for the same key."""

    @abstractmethod
    def claim(self, deterministic_key: str, value: RuntimeObject) -> tuple[bool, RuntimeObject]:
        """Atomically store a value for a key, or return the prior value."""

    @abstractmethod
    def update_status(
        self,
        object_id: str,
        status: str,
        *,
        expected_status: str | None = None,
    ) -> RuntimeObject:
        """Atomically update mutable execution status."""

    @abstractmethod
    def append_event(self, event: RuntimeObject) -> RuntimeObject:
        """Append an ExecutionEvent to the audit stream."""

    @abstractmethod
    def get(self, object_id: str) -> RuntimeObject | None:
        """Return an object by id, if present."""

    @abstractmethod
    def list_by_workflow_execution(self, workflow_execution_id: str) -> tuple[RuntimeObject, ...]:
        """List objects belonging to a WorkflowExecution in creation order."""


class InMemoryRuntimeObjectStore(RuntimeObjectStore):
    """Thread-safe in-memory store intended for tests and local execution."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._objects: dict[str, dict[str, Any]] = {}
        self._deterministic_keys: dict[str, str] = {}
        self._claims: dict[str, dict[str, Any]] = {}
        self._workflow_index: dict[str, list[str]] = {}

    def create(self, runtime_object: RuntimeObject, *, deterministic_key: str) -> RuntimeObject:
        value = _copy_and_validate(runtime_object)
        if not deterministic_key:
            raise ValueError("deterministic_key must not be empty")

        object_id = value["id"]
        with self._lock:
            existing_id = self._deterministic_keys.get(deterministic_key)
            if existing_id is not None:
                return _snapshot(self._objects[existing_id])
            if object_id in self._objects:
                raise RuntimeObjectAlreadyExistsError(
                    f"runtime object {object_id!r} already exists"
                )

            self._objects[object_id] = value
            self._deterministic_keys[deterministic_key] = object_id
            self._index(value)
            return _snapshot(value)

    def claim(self, deterministic_key: str, value: RuntimeObject) -> tuple[bool, RuntimeObject]:
        if not deterministic_key:
            raise ValueError("deterministic_key must not be empty")
        if not isinstance(value, Mapping):
            raise TypeError("value must be a mapping")

        with self._lock:
            existing = self._claims.get(deterministic_key)
            if existing is not None:
                return False, _snapshot(existing)
            claimed = deepcopy(dict(value))
            self._claims[deterministic_key] = claimed
            return True, _snapshot(claimed)

    def update_status(
        self,
        object_id: str,
        status: str,
        *,
        expected_status: str | None = None,
    ) -> RuntimeObject:
        if not status:
            raise ValueError("status must not be empty")

        with self._lock:
            value = self._require(object_id)
            current = value.get("status")
            if current is None:
                raise ValueError(f"runtime object {object_id!r} has no status")
            if expected_status is not None and current != expected_status:
                raise StatusConflictError(
                    f"expected status {expected_status!r} for {object_id!r}, found {current!r}"
                )
            if current in TERMINAL_STATUSES:
                if current == status:
                    return _snapshot(value)
                raise ImmutableRuntimeObjectError(
                    f"completed runtime object {object_id!r} is immutable"
                )

            now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            value["status"] = status
            value["updatedAt"] = now
            if status in TERMINAL_STATUSES:
                value.setdefault("completedAt", now)
            return _snapshot(value)

    def append_event(self, event: RuntimeObject) -> RuntimeObject:
        value = _copy_and_validate(event)
        if value.get("kind") != "ExecutionEvent":
            raise ValueError("append_event requires an ExecutionEvent")
        return self.create(value, deterministic_key=f"execution-event:{value['id']}")

    def get(self, object_id: str) -> RuntimeObject | None:
        with self._lock:
            value = self._objects.get(object_id)
            return _snapshot(value) if value is not None else None

    def list_by_workflow_execution(self, workflow_execution_id: str) -> tuple[RuntimeObject, ...]:
        with self._lock:
            return tuple(
                _snapshot(self._objects[object_id])
                for object_id in self._workflow_index.get(workflow_execution_id, ())
            )

    def _require(self, object_id: str) -> dict[str, Any]:
        try:
            return self._objects[object_id]
        except KeyError as error:
            raise RuntimeObjectNotFoundError(
                f"runtime object {object_id!r} was not found"
            ) from error

    def _index(self, value: dict[str, Any]) -> None:
        workflow_execution_id = _workflow_execution_id(value)
        if workflow_execution_id is not None:
            self._workflow_index.setdefault(workflow_execution_id, []).append(value["id"])


def _copy_and_validate(runtime_object: RuntimeObject) -> dict[str, Any]:
    if not isinstance(runtime_object, Mapping):
        raise TypeError("runtime_object must be a mapping")
    value = deepcopy(dict(runtime_object))
    object_id = value.get("id")
    kind = value.get("kind")
    if not isinstance(object_id, str) or not object_id:
        raise ValueError("runtime_object.id must be a non-empty string")
    if not isinstance(kind, str) or not kind:
        raise ValueError("runtime_object.kind must be a non-empty string")
    return value


def _workflow_execution_id(value: RuntimeObject) -> str | None:
    if value.get("kind") == "WorkflowExecution":
        candidate = value.get("id")
    else:
        candidate = value.get("workflowExecutionId")
        if candidate is None:
            provenance = value.get("provenance")
            if isinstance(provenance, Mapping):
                candidate = provenance.get("workflowExecutionId")
    return candidate if isinstance(candidate, str) else None


def _snapshot(value: dict[str, Any]) -> RuntimeObject:
    # A deep copy prevents callers from mutating stored evidence through aliases.
    return MappingProxyType(deepcopy(value))
