"""Internal context for content-free Agent history persistence tracing."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from .context import current_trace_service
from .outbound import stable_identifier_hash
from .service import NoopTraceSpan, TraceSpan


@dataclass(frozen=True)
class HistoryPersistContext:
    """Facts supplied by an internal Agent history write call site."""

    trigger_source: str
    checkpoint_present: bool = False


_history_persist_context: ContextVar[HistoryPersistContext | None] = ContextVar(
    "astrbot_trace_history_persist_context",
    default=None,
)


@contextmanager
def agent_history_persist_context(
    trigger_source: str,
    *,
    checkpoint_present: bool = False,
) -> Iterator[None]:
    """Mark one internal conversation update as Agent-history persistence."""

    token = _history_persist_context.set(
        HistoryPersistContext(
            trigger_source=trigger_source,
            checkpoint_present=checkpoint_present,
        )
    )
    try:
        yield
    finally:
        _history_persist_context.reset(token)


def start_history_persist_span(
    configured_service: Any,
    *,
    unified_msg_origin: str,
    conversation_id: str | None,
    history: list[dict] | None,
    token_usage: int | None,
) -> TraceSpan | NoopTraceSpan:
    """Start a history write span only for an explicitly marked Agent call."""

    context = _history_persist_context.get()
    if context is None or history is None:
        return NoopTraceSpan()
    service = current_trace_service() or configured_service
    if service is None:
        return NoopTraceSpan()
    roles: Counter[str] = Counter()
    checkpoint_count = 0
    for message in history:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if isinstance(role, str):
            roles[role] += 1
        if message.get("type") == "checkpoint":
            checkpoint_count += 1
    attributes = {
        "trigger_source": context.trigger_source,
        "conversation_id_hash": stable_identifier_hash(conversation_id),
        "umo_hash": stable_identifier_hash(unified_msg_origin),
        "pending_message_count": len(history),
        "role_distribution": dict(roles),
        "checkpoint_present": context.checkpoint_present or checkpoint_count > 0,
        "checkpoint_count": checkpoint_count,
        "token_usage_present": token_usage is not None,
    }
    try:
        if service.current_span().trace_id is not None:
            return service.start_span(
                "conversation.history.persist",
                kind="storage",
                attributes=attributes,
            )
        return service.start_root(
            "conversation.history.persist",
            kind="storage",
            attributes=attributes,
        )
    except Exception:
        return NoopTraceSpan()
