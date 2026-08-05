"""Immutable GeneratedArtifact metadata and content-addressed storage."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
from functools import cache
from hashlib import sha256
import json
from pathlib import Path
import re
from threading import RLock
from types import MappingProxyType
from typing import Any, TypeAlias

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource as SchemaResource
from referencing.jsonschema import DRAFT202012

from aep.observability import CorrelationContext, ObservabilityContractError
from aep.runtime_store import InMemoryRuntimeObjectStore, RuntimeObjectStore


ArtifactContent: TypeAlias = (
    bytes | bytearray | memoryview | str | Mapping[str, Any] | list[Any]
)
ArtifactMetadata: TypeAlias = Mapping[str, Any]

RFC3339_DATETIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


class GeneratedArtifactStoreError(Exception):
    """Base class for GeneratedArtifact storage errors."""


class GeneratedArtifactNotFoundError(GeneratedArtifactStoreError):
    """Raised when artifact metadata or its content cannot be found."""


class ImmutableGeneratedArtifactError(GeneratedArtifactStoreError):
    """Raised when published GeneratedArtifact evidence would be changed."""


class GeneratedArtifactValidationError(GeneratedArtifactStoreError):
    """Raised when GeneratedArtifact metadata violates its runtime schema."""


class ContentIntegrityError(GeneratedArtifactStoreError):
    """Raised when content does not match its content address."""


class ContentAddressedStore(ABC):
    """Provider-neutral boundary for immutable content-addressed bytes."""

    @abstractmethod
    def put(self, content: bytes, *, expected_address: str | None = None) -> str:
        """Store content once and return its digest-based address."""

    @abstractmethod
    def get(self, content_address: str) -> bytes | None:
        """Return content after verifying its digest, if present."""


class InMemoryContentAddressedStore(ContentAddressedStore):
    """Thread-safe content store for tests and local execution."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._content: dict[str, bytes] = {}

    def put(self, content: bytes, *, expected_address: str | None = None) -> str:
        if not isinstance(content, bytes):
            raise TypeError("content must be bytes")
        content_address = _content_address(content)
        if expected_address is not None and expected_address != content_address:
            raise ContentIntegrityError(
                f"content digest {content_address!r} does not match "
                f"expected address {expected_address!r}"
            )

        with self._lock:
            self._content.setdefault(content_address, content)
        return content_address

    def get(self, content_address: str) -> bytes | None:
        with self._lock:
            content = self._content.get(content_address)
        if content is None:
            return None
        actual_address = _content_address(content)
        if actual_address != content_address:
            raise ContentIntegrityError(
                f"stored content digest {actual_address!r} does not match "
                f"address {content_address!r}"
            )
        return content

    @property
    def object_count(self) -> int:
        """Return the number of unique content objects, for diagnostics."""
        with self._lock:
            return len(self._content)


class GeneratedArtifactStore(ABC):
    """Persistence boundary separating artifact metadata from content."""

    @abstractmethod
    def publish(
        self, metadata: ArtifactMetadata, content: ArtifactContent
    ) -> ArtifactMetadata:
        """Publish immutable artifact metadata and content."""

    @abstractmethod
    def get(self, artifact_id: str) -> ArtifactMetadata | None:
        """Return artifact metadata by id, if present."""

    @abstractmethod
    def get_content(self, artifact_id: str) -> bytes:
        """Return verified content for an artifact."""

    @abstractmethod
    def list_by_task_execution(
        self, task_execution_id: str
    ) -> tuple[ArtifactMetadata, ...]:
        """Return artifacts produced by a TaskExecution in publication order."""


class InMemoryGeneratedArtifactStore(GeneratedArtifactStore):
    """Thread-safe GeneratedArtifact store for tests and local execution."""

    def __init__(
        self,
        *,
        runtime_store: RuntimeObjectStore | None = None,
        content_store: ContentAddressedStore | None = None,
    ) -> None:
        self._runtime_store = runtime_store or InMemoryRuntimeObjectStore()
        self._content_store = content_store or InMemoryContentAddressedStore()
        self._lock = RLock()

    def publish(
        self, metadata: ArtifactMetadata, content: ArtifactContent
    ) -> ArtifactMetadata:
        value = _copy_metadata(metadata)
        encoded_content = _encode_content(content)
        expected_address = value.get("contentAddress")
        if expected_address is not None and not isinstance(expected_address, str):
            raise GeneratedArtifactValidationError(
                "invalid GeneratedArtifact at $.contentAddress: must be a string"
            )
        content_address = _content_address(encoded_content)
        if expected_address is not None and expected_address != content_address:
            raise ContentIntegrityError(
                f"content digest {content_address!r} does not match "
                f"expected address {expected_address!r}"
            )
        value["contentAddress"] = content_address
        created_at = value.get("createdAt")
        if "publishedAt" not in value and isinstance(created_at, str):
            value["publishedAt"] = created_at
        try:
            CorrelationContext.from_runtime_object(value)
        except ObservabilityContractError as error:
            raise GeneratedArtifactValidationError(
                f"invalid GeneratedArtifact correlation: {error}"
            ) from error
        _validate_metadata(value)

        artifact_id = value["id"]
        deterministic_key = f"generated-artifact:{artifact_id}"
        publication_key = f"generated-artifact-publication:{artifact_id}"
        with self._lock:
            existing = self._runtime_store.get(artifact_id)
            if existing is not None:
                if dict(existing) != value:
                    raise ImmutableGeneratedArtifactError(
                        f"published GeneratedArtifact {artifact_id!r} is immutable"
                    )
                return _snapshot(existing)

            _, claimed = self._runtime_store.claim(publication_key, value)
            if dict(claimed) != value:
                raise ImmutableGeneratedArtifactError(
                    f"published GeneratedArtifact {artifact_id!r} is immutable"
                )

            existing = self._runtime_store.get(artifact_id)
            if existing is not None:
                if dict(existing) != value:
                    raise ImmutableGeneratedArtifactError(
                        f"published GeneratedArtifact {artifact_id!r} is immutable"
                    )
                return _snapshot(existing)

            self._content_store.put(
                encoded_content, expected_address=content_address
            )
            created = self._runtime_store.create(
                value, deterministic_key=deterministic_key
            )
            if dict(created) != value:
                raise ImmutableGeneratedArtifactError(
                    f"published GeneratedArtifact {artifact_id!r} is immutable"
                )
            return _snapshot(created)

    def get(self, artifact_id: str) -> ArtifactMetadata | None:
        value = self._runtime_store.get(artifact_id)
        if value is None:
            return None
        if value.get("kind") != "GeneratedArtifact":
            return None
        return _snapshot(value)

    def get_content(self, artifact_id: str) -> bytes:
        metadata = self.get(artifact_id)
        if metadata is None:
            raise GeneratedArtifactNotFoundError(
                f"GeneratedArtifact {artifact_id!r} was not found"
            )
        content_address = metadata["contentAddress"]
        content = self._content_store.get(content_address)
        if content is None:
            raise GeneratedArtifactNotFoundError(
                f"content {content_address!r} for GeneratedArtifact "
                f"{artifact_id!r} was not found"
            )
        return content

    def list_by_task_execution(
        self, task_execution_id: str
    ) -> tuple[ArtifactMetadata, ...]:
        return tuple(
            _snapshot(value)
            for value in self._runtime_store.list_by_task_execution(task_execution_id)
            if value.get("kind") == "GeneratedArtifact"
        )


def _copy_metadata(metadata: ArtifactMetadata) -> dict[str, Any]:
    if not isinstance(metadata, Mapping):
        raise TypeError("metadata must be a mapping")
    return deepcopy(dict(metadata))


def _validate_metadata(metadata: dict[str, Any]) -> None:
    errors = sorted(
        _generated_artifact_validator().iter_errors(metadata),
        key=lambda error: (list(error.absolute_path), error.message),
    )
    if errors:
        error = errors[0]
        path = "$" + "".join(
            f"[{part}]" if isinstance(part, int) else f".{part}"
            for part in error.absolute_path
        )
        raise GeneratedArtifactValidationError(
            f"invalid GeneratedArtifact at {path}: {error.message}"
        )


@cache
def _generated_artifact_validator() -> Draft202012Validator:
    schema_root = Path(__file__).parents[2] / "schemas"
    schema_paths = (
        schema_root / "resources" / "v1" / "resource-definitions.schema.json",
        schema_root / "runtime" / "v1" / "runtime-definitions.schema.json",
        schema_root / "runtime" / "v1" / "generatedartifact.schema.json",
    )
    schemas = [json.loads(path.read_text(encoding="utf-8")) for path in schema_paths]
    registry = Registry()
    for schema in schemas:
        registry = registry.with_resource(
            schema["$id"],
            SchemaResource.from_contents(schema, default_specification=DRAFT202012),
        )
    format_checker = FormatChecker()
    format_checker.checks("date-time")(_is_rfc3339_datetime)
    return Draft202012Validator(
        schemas[-1],
        registry=registry,
        format_checker=format_checker,
    )


def _is_rfc3339_datetime(value: object) -> bool:
    if not isinstance(value, str) or RFC3339_DATETIME.fullmatch(value) is None:
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _encode_content(content: ArtifactContent) -> bytes:
    if isinstance(content, bytes):
        return content
    if isinstance(content, (bytearray, memoryview)):
        return bytes(content)
    if isinstance(content, str):
        return content.encode("utf-8")
    if isinstance(content, (Mapping, list)):
        try:
            canonical = json.dumps(
                content,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError) as error:
            raise ValueError("structured artifact content must be valid JSON") from error
        return canonical.encode("utf-8")
    raise TypeError("content must be bytes, text, or structured JSON")


def _content_address(content: bytes) -> str:
    return f"sha256:{sha256(content).hexdigest()}"


def _snapshot(value: ArtifactMetadata) -> ArtifactMetadata:
    return MappingProxyType(deepcopy(dict(value)))
