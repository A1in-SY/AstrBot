"""Shared models and limits for AstrBot execution tracing.

This module deliberately has no dependency on the Core lifecycle, providers, or
plugins.  Keeping the value objects here makes the tracing runtime safe to use
from low-level execution paths without creating import cycles.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from typing import Any

TRACE_STATUS_RUNNING = "running"
TRACE_STATUS_SUCCESS = "success"
TRACE_STATUS_SKIPPED = "skipped"
TRACE_STATUS_ERROR = "error"
TRACE_STATUS_CANCELLED = "cancelled"
TRACE_STATUS_INCOMPLETE = "incomplete"

TRACE_STATUSES = frozenset(
    {
        TRACE_STATUS_RUNNING,
        TRACE_STATUS_SUCCESS,
        TRACE_STATUS_SKIPPED,
        TRACE_STATUS_ERROR,
        TRACE_STATUS_CANCELLED,
        TRACE_STATUS_INCOMPLETE,
    }
)
TERMINAL_TRACE_STATUSES = TRACE_STATUSES - {TRACE_STATUS_RUNNING}

MAX_ARTIFACT_LOGICAL_BYTES = 4 * 1024 * 1024
MAX_TRACE_CAPTURED_BYTES = 16 * 1024 * 1024
INLINE_ARTIFACT_BYTES = 1024
MAX_SPANS_PER_TRACE = 1024
MAX_EVENTS_PER_SPAN = 128
MAX_EVENTS_PER_TRACE = 4096
MAX_ATTRIBUTES_PER_SPAN = 128
MAX_ATTRIBUTES_BYTES_PER_SPAN = 64 * 1024
MAX_LINKS_PER_TRACE = 128


def new_trace_id() -> str:
    """Create a W3C-shaped 128-bit trace identifier."""

    return secrets.token_hex(16)


def new_span_id() -> str:
    """Create a W3C-shaped 64-bit span identifier."""

    return secrets.token_hex(8)


def canonical_json(value: Any) -> bytes:
    """Encode JSON deterministically without invoking arbitrary object methods.

    Args:
        value: JSON-compatible data to encode.

    Returns:
        Stable UTF-8 encoded JSON bytes.

    Raises:
        TypeError: If the value contains a non-JSON-compatible object.
    """

    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def content_hash(content: bytes) -> str:
    """Return the SHA-256 digest for canonical, uncompressed content."""

    return hashlib.sha256(content).hexdigest()
