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
        field: _json_value(context[field])
        for field in _IDENTITY_FIELDS if field in context
    }
    encoded = json.dumps(
        identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    )
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"context identity contains non-JSON value {type(value).__name__}")
