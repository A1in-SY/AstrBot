"""Context-local state for AstrBot execution tracing."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SpanState:
    """Mutable in-memory state for one span before it reaches terminal state."""

    span_id: str
    parent_span_id: str | None
    operation: str
    kind: str
    source: str
    plugin_id: str | None
    started_at: float
    attributes: dict[str, Any] = field(default_factory=dict)
    status: str = "running"
    outcome: str | None = None
    degraded: bool = False
    degradation_reasons: list[str] = field(default_factory=list)
    ended_at: float | None = None
    event_count: int = 0
    artifact_ref_count: int = 0
    terminal: bool = False


@dataclass
class TraceState:
    """Mutable in-memory state for a trace and its lazy persistence envelope."""

    trace_id: str
    service: Any | None
    root_span_id: str
    operation: str
    kind: str
    source: str
    plugin_id: str | None
    started_at: float
    attributes: dict[str, Any] = field(default_factory=dict)
    status: str = "running"
    outcome: str | None = None
    degraded: bool = False
    degradation_reasons: list[str] = field(default_factory=list)
    ended_at: float | None = None
    revision: int = 0
    lazy: bool = False
    materialized: bool = False
    terminal: bool = False
    spans: dict[str, SpanState] = field(default_factory=dict)
    pending_commands: list[Any] = field(default_factory=list)
    unique_artifact_hashes: set[str] = field(default_factory=set)
    captured_artifact_bytes: int = 0
    event_count: int = 0
    link_count: int = 0
    dropped: dict[str, int] = field(default_factory=dict)


_current_trace_state: ContextVar[TraceState | None] = ContextVar(
    "astrbot_trace_state",
    default=None,
)
_current_span_id: ContextVar[str | None] = ContextVar(
    "astrbot_trace_span_id",
    default=None,
)
_trace_suppressed: ContextVar[bool] = ContextVar(
    "astrbot_trace_suppressed",
    default=False,
)


def current_trace_state() -> TraceState | None:
    """Return the trace state visible in the current task context."""

    return _current_trace_state.get()


def current_span_id() -> str | None:
    """Return the active span identifier visible in the current task context."""

    return _current_span_id.get()


def current_trace_service() -> Any | None:
    """Return the tracing service associated with the active trace context."""

    state = current_trace_state()
    return state.service if state is not None else None


def trace_suppressed() -> bool:
    """Return whether new automatic tracing is suppressed in this context."""

    return _trace_suppressed.get()


@contextmanager
def detached_trace_context() -> Iterator[None]:
    """Temporarily clear only Trace ContextVars for a known detached task.

    ``asyncio.create_task`` copies the caller's context.  Scheduling a known
    background business operation inside this scope gives the new task a clean
    Trace context while preserving every unrelated application ContextVar.
    """

    trace_token = _current_trace_state.set(None)
    span_token = _current_span_id.set(None)
    suppression_token = _trace_suppressed.set(False)
    try:
        yield
    finally:
        _trace_suppressed.reset(suppression_token)
        _current_span_id.reset(span_token)
        _current_trace_state.reset(trace_token)
