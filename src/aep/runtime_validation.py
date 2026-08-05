"""Shared validation primitives for AEP runtime objects."""

from __future__ import annotations

from datetime import datetime
import re


RFC3339_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:(?P<second>\d{2})"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


def is_rfc3339_timestamp(value: object) -> bool:
    """Return whether ``value`` is a timezone-qualified RFC3339 timestamp."""
    if not isinstance(value, str):
        return True
    match = RFC3339_TIMESTAMP.fullmatch(value)
    if match is None:
        return False
    parseable = value
    if match.group("second") == "60":
        start, end = match.span("second")
        parseable = f"{value[:start]}59{value[end:]}"
    try:
        parsed = datetime.fromisoformat(parseable.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.utcoffset() is not None
