"""Stable identity for a ContextPackage's reproducible input payload."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from typing import Any


_IDENTITY_FIELDS = (
    "taskRef", "repositoryRevision", "elements", "tokenBudget", "selection",
    "tokenCount", "tokenEstimate", "truncation",
)


def context_identity(context: Mapping[str, Any]) -> str:
    """Hash context inputs while excluding execution-specific ownership fields."""
    identity = {
        field: _stable_context_value(context[field], (field,))
        for field in _IDENTITY_FIELDS if field in context
    }
    encoded = json.dumps(
        identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    )
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _stable_context_value(value: Any, path: tuple[str, ...]) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if path[-1:] == ("retrieval",) and key == "snapshotCreatedAt":
                continue
            if path[-1:] == ("provenance",) and key in {
                "traceId", "workflowExecutionId", "taskExecutionId",
            }:
                continue
            if path[-2:] == ("content", "metadata") and key in {
                "id", "createdAt", "updatedAt",
            }:
                continue
            if path[-1:] == ("inputArtifactRefs",) and key == "generatedArtifactId":
                continue
            result[key] = _stable_context_value(item, (*path, key))
        return result
    if isinstance(value, (list, tuple)):
        return [_stable_context_value(item, path) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"context identity contains non-JSON value {type(value).__name__}")
