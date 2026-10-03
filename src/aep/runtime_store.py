"""Persistence boundary for AEP runtime objects."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timezone
from contextlib import contextmanager
import errno
import json
import os
from pathlib import Path
import tempfile
from threading import RLock
from types import MappingProxyType
from typing import Any, Callable, Final


RuntimeObject = Mapping[str, Any]

TERMINAL_STATUSES: Final = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})
RUNTIME_IDENTITY_FIELDS: Final = frozenset(
    {"apiVersion", "id", "kind", "traceId", "createdAt", "provenance"}
)
KIND_IDENTITY_FIELDS: Final = {
    "TaskExecution": frozenset(
        {"workflowExecutionId", "taskRef", "attempt", "dependencyTaskExecutionIds"}
    )
}
KIND_WRITE_ONCE_FIELDS: Final = {
    "TaskExecution": frozenset({
        "contextPackageId", "resolvedAgentId", "terminalEvidence",
        "dispatchState", "dispatchOwnerId",
    }),
    "WorkflowExecution": frozenset({"resolvedTaskPlan"}),
}
STATUS_MANAGED_FIELDS: Final = frozenset({"status", "updatedAt", "completedAt"})


class RuntimeStoreError(Exception):
    """Base class for runtime store errors."""

    def __init__(
        self,
        message: str,
        *,
        diagnostic: Mapping[str, Any] | None = None,
        internal_error: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.diagnostic = MappingProxyType(dict(diagnostic or {}))
        self._internal_error = internal_error


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
        updated_at: str | None = None,
        changes: RuntimeObject | None = None,
    ) -> RuntimeObject:
        """Atomically update mutable execution status and associated evidence."""

    @abstractmethod
    def append_event(self, event: RuntimeObject) -> RuntimeObject:
        """Append an ExecutionEvent to the audit stream."""

    @abstractmethod
    def append_task_execution_id(
        self,
        workflow_execution_id: str,
        task_execution_id: str,
        *,
        updated_at: str | None = None,
    ) -> RuntimeObject:
        """Atomically attach a TaskExecution to its owning WorkflowExecution."""

    @abstractmethod
    def get(self, object_id: str) -> RuntimeObject | None:
        """Return an object by id, if present."""

    @abstractmethod
    def list_by_workflow_execution(self, workflow_execution_id: str) -> tuple[RuntimeObject, ...]:
        """List objects belonging to a WorkflowExecution in creation order."""

    @abstractmethod
    def list_by_task_execution(self, task_execution_id: str) -> tuple[RuntimeObject, ...]:
        """List objects produced by a TaskExecution in creation order."""

    supports_atomic_task_transitions: bool = False

    def commit_task_execution_transition(
        self,
        task_execution_id: str,
        status: str,
        *,
        workflow_execution_id: str,
        event: RuntimeObject,
        expected_status: str,
        updated_at: str,
        changes: RuntimeObject | None = None,
    ) -> RuntimeObject:
        """Atomically persist a task transition, workflow attachment, and event."""
        raise NotImplementedError("store does not support atomic task transitions")

    def claim_task_dispatch(
        self,
        task_execution_id: str,
        owner_id: str,
        *,
        updated_at: str,
    ) -> tuple[bool, RuntimeObject]:
        """Atomically claim one durable TaskExecution for executor dispatch."""
        raise NotImplementedError("store does not support atomic task dispatch claims")


class InMemoryRuntimeObjectStore(RuntimeObjectStore):
    """Thread-safe in-memory store intended for tests and local execution."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._objects: dict[str, dict[str, Any]] = {}
        self._deterministic_keys: dict[str, str] = {}
        self._claims: dict[str, dict[str, Any]] = {}
        self._workflow_index: dict[str, list[str]] = {}
        self._task_execution_index: dict[str, list[str]] = {}

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

    def claim_task_dispatch(
        self,
        task_execution_id: str,
        owner_id: str,
        *,
        updated_at: str,
    ) -> tuple[bool, RuntimeObject]:
        if not isinstance(owner_id, str) or not owner_id:
            raise ValueError("owner_id must be a non-empty string")
        with self._lock:
            value = self._require(task_execution_id)
            if value.get("kind") != "TaskExecution":
                raise ValueError("dispatch claims require a TaskExecution")
            if value.get("status") != "RUNNING":
                raise StatusConflictError(
                    f"TaskExecution {task_execution_id!r} is not RUNNING"
                )
            dispatch_state = value.get("dispatchState")
            if dispatch_state == "CLAIMED":
                return False, _snapshot(value)
            if dispatch_state != "PENDING":
                raise ValueError(
                    f"TaskExecution {task_execution_id!r} has no pending dispatch"
                )
            value["dispatchState"] = "CLAIMED"
            value["dispatchOwnerId"] = owner_id
            value["updatedAt"] = updated_at
            return True, _snapshot(value)

    def update_status(
        self,
        object_id: str,
        status: str,
        *,
        expected_status: str | None = None,
        updated_at: str | None = None,
        changes: RuntimeObject | None = None,
    ) -> RuntimeObject:
        if not status:
            raise ValueError("status must not be empty")
        change_values = deepcopy(dict(changes or {}))

        with self._lock:
            value = self._require(object_id)
            protected_fields = (
                RUNTIME_IDENTITY_FIELDS
                | STATUS_MANAGED_FIELDS
                | KIND_IDENTITY_FIELDS.get(value["kind"], frozenset())
            )
            protected = protected_fields.intersection(change_values)
            protected |= {
                field
                for field in KIND_WRITE_ONCE_FIELDS.get(value["kind"], frozenset())
                if field in value and field in change_values
            }
            if protected:
                raise ValueError(
                    f"changes cannot replace protected fields: {sorted(protected)!r}"
                )
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

            now = updated_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            value.update(change_values)
            value["status"] = status
            value["updatedAt"] = now
            if status in TERMINAL_STATUSES:
                value.setdefault("completedAt", now)
            return _snapshot(value)

    def append_event(self, event: RuntimeObject) -> RuntimeObject:
        value = _copy_and_validate(event)
        if value.get("kind") != "ExecutionEvent":
            raise ValueError("append_event requires an ExecutionEvent")
        return InMemoryRuntimeObjectStore.create(
            self, value, deterministic_key=f"execution-event:{value['id']}"
        )

    def commit_task_execution_transition(
        self,
        task_execution_id: str,
        status: str,
        *,
        workflow_execution_id: str,
        event: RuntimeObject,
        expected_status: str,
        updated_at: str,
        changes: RuntimeObject | None = None,
    ) -> RuntimeObject:
        """Provide the contract for test stores; durable stores override the checkpoint."""
        with self._lock:
            snapshot = self._memory_snapshot()
            try:
                updated = InMemoryRuntimeObjectStore.update_status(
                    self, task_execution_id, status,
                    expected_status=expected_status, updated_at=updated_at,
                    changes=changes,
                )
                InMemoryRuntimeObjectStore.append_task_execution_id(
                    self, workflow_execution_id, task_execution_id,
                    updated_at=updated_at,
                )
                InMemoryRuntimeObjectStore.append_event(self, event)
                return updated
            except Exception:
                self._restore_memory_snapshot(snapshot)
                raise

    def append_task_execution_id(
        self,
        workflow_execution_id: str,
        task_execution_id: str,
        *,
        updated_at: str | None = None,
    ) -> RuntimeObject:
        if not isinstance(task_execution_id, str) or not task_execution_id:
            raise ValueError("task_execution_id must be a non-empty string")
        with self._lock:
            workflow = self._require(workflow_execution_id)
            if workflow.get("kind") != "WorkflowExecution":
                raise ValueError(
                    f"runtime object {workflow_execution_id!r} is not a WorkflowExecution"
                )
            task = self._require(task_execution_id)
            if (
                task.get("kind") != "TaskExecution"
                or task.get("workflowExecutionId") != workflow_execution_id
            ):
                raise ValueError(
                    f"TaskExecution {task_execution_id!r} does not belong to "
                    f"WorkflowExecution {workflow_execution_id!r}"
                )
            task_execution_ids = workflow.setdefault("taskExecutionIds", [])
            if not isinstance(task_execution_ids, list):
                raise ValueError("WorkflowExecution.taskExecutionIds must be an array")
            if task_execution_id not in task_execution_ids:
                if workflow.get("status") in TERMINAL_STATUSES:
                    raise ImmutableRuntimeObjectError(
                        f"completed runtime object {workflow_execution_id!r} is immutable"
                    )
                task_execution_ids.append(task_execution_id)
                workflow["updatedAt"] = updated_at or datetime.now(
                    timezone.utc
                ).isoformat().replace("+00:00", "Z")
            return _snapshot(workflow)

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

    def list_by_task_execution(self, task_execution_id: str) -> tuple[RuntimeObject, ...]:
        with self._lock:
            return tuple(
                _snapshot(self._objects[object_id])
                for object_id in self._task_execution_index.get(task_execution_id, ())
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
        task_execution_id = _task_execution_id(value)
        if task_execution_id is not None:
            self._task_execution_index.setdefault(task_execution_id, []).append(value["id"])

    def _memory_snapshot(self) -> tuple[Any, ...]:
        return (
            deepcopy(self._objects), deepcopy(self._deterministic_keys),
            deepcopy(self._claims), deepcopy(self._workflow_index),
            deepcopy(self._task_execution_index),
        )

    def _restore_memory_snapshot(self, snapshot: tuple[Any, ...]) -> None:
        (
            self._objects, self._deterministic_keys, self._claims,
            self._workflow_index, self._task_execution_index,
        ) = snapshot


class DurableJsonRuntimeObjectStore(InMemoryRuntimeObjectStore):
    """Durable JSON store with a cross-process writer fence and safe checkpoints."""

    supports_atomic_task_transitions = True

    def __init__(self, path: Path | str) -> None:
        super().__init__()
        self._path = Path(path).resolve()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path = self._path.with_name(f".{self._path.name}.lock")
        with self._lock, self._writer_fence():
            self._cleanup_stale_checkpoints()
            if self._path.exists():
                self._restore()

    def create(
        self, runtime_object: RuntimeObject, *, deterministic_key: str
    ) -> RuntimeObject:
        return self._mutate(lambda: InMemoryRuntimeObjectStore.create(
            self, runtime_object, deterministic_key=deterministic_key
        ))

    def claim(
        self, deterministic_key: str, value: RuntimeObject
    ) -> tuple[bool, RuntimeObject]:
        return self._mutate(lambda: InMemoryRuntimeObjectStore.claim(self, deterministic_key, value))

    def claim_task_dispatch(
        self,
        task_execution_id: str,
        owner_id: str,
        *,
        updated_at: str,
    ) -> tuple[bool, RuntimeObject]:
        return self._mutate(lambda: InMemoryRuntimeObjectStore.claim_task_dispatch(
            self, task_execution_id, owner_id, updated_at=updated_at
        ))

    def update_status(
        self,
        object_id: str,
        status: str,
        *,
        expected_status: str | None = None,
        updated_at: str | None = None,
        changes: RuntimeObject | None = None,
    ) -> RuntimeObject:
        return self._mutate(lambda: InMemoryRuntimeObjectStore.update_status(
            self,
                object_id,
                status,
                expected_status=expected_status,
                updated_at=updated_at,
                changes=changes,
            ))

    def append_event(self, event: RuntimeObject) -> RuntimeObject:
        return self._mutate(lambda: InMemoryRuntimeObjectStore.append_event(self, event))

    def append_task_execution_id(
        self,
        workflow_execution_id: str,
        task_execution_id: str,
        *,
        updated_at: str | None = None,
    ) -> RuntimeObject:
        return self._mutate(lambda: InMemoryRuntimeObjectStore.append_task_execution_id(
            self,
                workflow_execution_id,
                task_execution_id,
                updated_at=updated_at,
            ))

    def commit_task_execution_transition(
        self,
        task_execution_id: str,
        status: str,
        *,
        workflow_execution_id: str,
        event: RuntimeObject,
        expected_status: str,
        updated_at: str,
        changes: RuntimeObject | None = None,
    ) -> RuntimeObject:
        def mutation() -> RuntimeObject:
            updated = InMemoryRuntimeObjectStore.update_status(
                self, task_execution_id, status, expected_status=expected_status,
                updated_at=updated_at, changes=changes,
            )
            InMemoryRuntimeObjectStore.append_task_execution_id(
                self, workflow_execution_id, task_execution_id, updated_at=updated_at,
            )
            InMemoryRuntimeObjectStore.append_event(self, event)
            return updated

        return self._mutate(mutation)

    def get(self, object_id: str) -> RuntimeObject | None:
        return self._read(lambda: InMemoryRuntimeObjectStore.get(self, object_id))

    def list_by_workflow_execution(self, workflow_execution_id: str) -> tuple[RuntimeObject, ...]:
        return self._read(lambda: InMemoryRuntimeObjectStore.list_by_workflow_execution(self, workflow_execution_id))

    def list_by_task_execution(self, task_execution_id: str) -> tuple[RuntimeObject, ...]:
        return self._read(lambda: InMemoryRuntimeObjectStore.list_by_task_execution(self, task_execution_id))

    def _restore(self) -> None:
        try:
            encoded = self._path.read_text(encoding="utf-8")
        except UnicodeError as error:
            raise RuntimeStoreError(
                "durable runtime checkpoint is invalid",
                diagnostic={
                    "operation": "restore", "phase": "read",
                    "category": "invalid_checkpoint",
                },
                internal_error=error,
            ) from None
        except OSError as error:
            raise RuntimeStoreError(
                "durable runtime checkpoint could not be read",
                diagnostic=_persistence_diagnostic("restore", "read", error),
                internal_error=error,
            ) from None
        try:
            payload = json.loads(encoded)
            objects = payload["objects"]
            deterministic_keys = payload["deterministicKeys"]
            claims = payload["claims"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeStoreError(
                "durable runtime checkpoint is invalid",
                diagnostic={"operation": "restore", "phase": "read", "category": "invalid_checkpoint"},
                internal_error=error,
            ) from None
        invalid_checkpoint = {
            "operation": "restore", "phase": "read",
            "category": "invalid_checkpoint",
        }
        if not all(isinstance(item, dict) for item in (objects, deterministic_keys, claims)):
            raise RuntimeStoreError(
                "durable runtime checkpoint is invalid",
                diagnostic=invalid_checkpoint,
            )
        object_order = payload.get("objectOrder", list(objects))
        if (
            not isinstance(object_order, list)
            or not all(isinstance(item, str) for item in object_order)
            or len(object_order) != len(set(object_order))
            or set(object_order) != set(objects)
        ):
            raise RuntimeStoreError(
                "durable runtime checkpoint is invalid",
                diagnostic=invalid_checkpoint,
            )
        self._objects = {
            object_id: _copy_and_validate(objects[object_id])
            for object_id in object_order
        }
        self._deterministic_keys = {
            str(key): str(value) for key, value in deterministic_keys.items()
        }
        self._claims = {
            str(key): deepcopy(value) for key, value in claims.items()
        }
        self._workflow_index.clear()
        self._task_execution_index.clear()
        for value in self._objects.values():
            self._index(value)

    def _checkpoint(self) -> None:
        payload = {
            "objects": self._objects,
            "objectOrder": list(self._objects),
            "deterministicKeys": self._deterministic_keys,
            "claims": self._claims,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        temporary: Path | None = None
        phase = "temporary_create"
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=f".{self._path.name}.", suffix=".tmp",
                dir=self._path.parent, delete=False,
            ) as stream:
                temporary = Path(stream.name)
                phase = "temporary_write"
                stream.write(encoded.encode("utf-8"))
                stream.flush()
                phase = "file_sync"
                os.fsync(stream.fileno())
            phase = "replace"
            os.replace(temporary, self._path)
            temporary = None
            phase = "directory_sync"
            self._sync_directory()
        except OSError as error:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
            raise RuntimeStoreError(
                "durable runtime checkpoint persistence failed",
                diagnostic=_persistence_diagnostic("checkpoint", phase, error),
                internal_error=error,
            ) from None

    def _mutate(self, operation: Callable[[], Any]) -> Any:
        with self._lock, self._writer_fence():
            if self._path.exists():
                self._restore()
            snapshot = self._memory_snapshot()
            try:
                result = operation()
                self._checkpoint()
                return result
            except RuntimeStoreError as error:
                if not error.diagnostic.get("phase") == "directory_sync":
                    self._restore_memory_snapshot(snapshot)
                raise
            except Exception:
                self._restore_memory_snapshot(snapshot)
                raise

    def _read(self, operation: Callable[[], Any]) -> Any:
        with self._lock, self._writer_fence():
            if self._path.exists():
                self._restore()
            return operation()

    def _cleanup_stale_checkpoints(self) -> None:
        pattern = f".{self._path.name}.*.tmp"
        try:
            for candidate in self._path.parent.glob(pattern):
                try:
                    candidate.unlink(missing_ok=True)
                except OSError as error:
                    raise RuntimeStoreError(
                        "stale runtime checkpoint could not be removed",
                        diagnostic=_persistence_diagnostic(
                            "checkpoint_cleanup", "remove", error
                        ),
                        internal_error=error,
                    ) from None
        except OSError as error:
            raise RuntimeStoreError(
                "stale runtime checkpoints could not be enumerated",
                diagnostic=_persistence_diagnostic(
                    "checkpoint_cleanup", "enumerate", error
                ),
                internal_error=error,
            ) from None

    @contextmanager
    def _writer_fence(self):
        handle = None
        descriptor: int | None = None
        phase = "open"
        try:
            descriptor = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            handle = os.fdopen(descriptor, "r+b")
            descriptor = None
            phase = "acquire"
            _lock_file(handle)
        except OSError as error:
            diagnostic_error = error
            diagnostic_phase = phase
            try:
                if handle is not None:
                    handle.close()
                elif descriptor is not None:
                    os.close(descriptor)
            except OSError as cleanup_error:
                diagnostic_error = cleanup_error
                diagnostic_phase = "cleanup"
            raise RuntimeStoreError(
                "durable runtime writer fence could not be acquired",
                diagnostic=_persistence_diagnostic(
                    "writer_fence", diagnostic_phase, diagnostic_error,
                    lock_contention=diagnostic_phase == "acquire" and os.name == "nt",
                ),
                internal_error=diagnostic_error,
            ) from None
        try:
            yield
        except BaseException:
            _release_file_lock(handle, suppress_errors=True)
            raise
        release_error = _release_file_lock(handle, suppress_errors=False)
        if release_error is not None:
            raise RuntimeStoreError(
                "durable runtime writer fence could not be released",
                diagnostic=_persistence_diagnostic(
                    "writer_fence", "release", release_error
                ),
                internal_error=release_error,
            ) from None

    def _sync_directory(self) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(self._path.parent, os.O_RDONLY)
        try:
            _sync_directory_descriptor(descriptor)
        finally:
            os.close(descriptor)


def _sync_directory_descriptor(descriptor: int) -> None:
    """Sync a directory unless its filesystem documents no such operation."""
    try:
        os.fsync(descriptor)
    except OSError as error:
        unsupported = {
            errno.EINVAL,
            getattr(errno, "ENOTSUP", errno.EINVAL),
            getattr(errno, "EOPNOTSUPP", errno.EINVAL),
        }
        if error.errno not in unsupported:
            raise


def _persistence_diagnostic(
    operation: str,
    phase: str,
    error: OSError,
    *,
    lock_contention: bool = False,
) -> dict[str, Any]:
    """Return bounded diagnostics without serializing host paths or exception text."""
    category_by_errno = {
        errno.EACCES: "permission_denied",
        errno.EPERM: "permission_denied",
        errno.ENOSPC: "no_space",
        errno.EROFS: "read_only",
        errno.EBUSY: "busy",
        errno.EEXIST: "collision",
        errno.EIO: "io_error",
    }
    number = getattr(error, "errno", None)
    category = (
        "busy"
        if lock_contention and number in {errno.EACCES, errno.EAGAIN}
        else category_by_errno.get(number, "os_error")
    )
    return {
        "operation": operation,
        "phase": phase,
        "category": category,
        "errno": number if isinstance(number, int) and number >= 0 else None,
    }


def _lock_file(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return
    import fcntl
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock_file(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _release_file_lock(handle: Any, *, suppress_errors: bool) -> OSError | None:
    error: OSError | None = None
    try:
        _unlock_file(handle)
    except OSError as candidate:
        error = candidate
    try:
        handle.close()
    except OSError as candidate:
        if error is None:
            error = candidate
    return None if suppress_errors else error


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


def _task_execution_id(value: RuntimeObject) -> str | None:
    candidate = value.get("taskExecutionId")
    if candidate is None:
        provenance = value.get("provenance")
        if isinstance(provenance, Mapping):
            candidate = provenance.get("taskExecutionId")
    return candidate if isinstance(candidate, str) else None


def _snapshot(value: dict[str, Any]) -> RuntimeObject:
    # A deep copy prevents callers from mutating stored evidence through aliases.
    return MappingProxyType(deepcopy(value))
