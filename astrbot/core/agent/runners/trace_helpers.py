"""Small async-generator helpers used by Agent Runner tracing boundaries."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager
from typing import Any

from astrbot.core.trace.service import NoopTraceSpan, TraceSpan


@contextmanager
def activate_trace_span(span: TraceSpan | NoopTraceSpan) -> Iterator[None]:
    """Make a span current only while advancing a downstream async generator."""

    if isinstance(span, NoopTraceSpan):
        yield
        return
    with span.activate():
        yield


async def close_traced_stream(
    stream: AsyncGenerator[Any, None],
    span: TraceSpan | NoopTraceSpan,
) -> None:
    """Close an async stream with its Trace context installed temporarily."""

    try:
        with activate_trace_span(span):
            await stream.aclose()
    except Exception:
        return
