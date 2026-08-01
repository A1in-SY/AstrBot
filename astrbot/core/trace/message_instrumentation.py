"""Trace instrumentation for concrete platform message-delivery methods."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import inspect
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any

from astrbot.core.message.message_event_result import MessageChain

from .context import current_trace_service
from .serialization import message_chain_manifest
from .service import NoopTraceSpan, TraceSpan

_STREAM_TEXT_LIMIT = 1024 * 1024
_inside_stream_delivery: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "astrbot_trace_inside_stream_delivery",
    default=False,
)


def instrument_message_event(event: Any) -> None:
    """Wrap one concrete event's delivery methods without changing its type.

    A platform adapter can override ``send`` and ``send_streaming`` freely. The
    instance wrappers sit outside those implementations, so every normal
    business send becomes observable without requiring individual adapters to
    know about the Trace service. Streaming keeps one semantic
    ``response.deliver`` Span and deliberately suppresses per-delta child
    ``message.send`` Spans.

    Args:
        event: A fully initialized concrete ``AstrMessageEvent`` instance.
    """

    try:
        if getattr(event, "_astrbot_trace_message_instrumented", False):
            return
        original_send = getattr(event, "send", None)
        original_send_streaming = getattr(event, "send_streaming", None)
    except Exception:
        return
    if not inspect.iscoroutinefunction(original_send):
        return

    @functools.wraps(original_send)
    async def traced_send(
        message: MessageChain | None, *args: Any, **kwargs: Any
    ) -> Any:
        if _inside_stream_delivery.get():
            return await original_send(message, *args, **kwargs)
        span = _start_delivery_span(event, "message.send", streaming=False)
        if isinstance(span, NoopTraceSpan):
            return await original_send(message, *args, **kwargs)
        with span:
            try:
                span.record_json(
                    "message.outgoing",
                    message_chain_manifest(message),
                    metadata={"direction": "outgoing", "streaming": False},
                )
            except Exception:
                span.mark_degraded("message_serialization_failed")
            return await original_send(message, *args, **kwargs)

    traced_send_streaming_fn: Callable[..., Awaitable[Any]] | None = None
    if inspect.iscoroutinefunction(original_send_streaming):

        @functools.wraps(original_send_streaming)
        async def traced_send_streaming(
            generator: AsyncGenerator[MessageChain, None],
            use_fallback: bool = False,
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            service = current_trace_service()
            if service is None:
                return await original_send_streaming(
                    generator,
                    use_fallback,
                    *args,
                    **kwargs,
                )
            parent_span = service.current_span()
            if isinstance(parent_span, NoopTraceSpan):
                return await original_send_streaming(
                    generator,
                    use_fallback,
                    *args,
                    **kwargs,
                )
            try:
                span = service.start_span(
                    "response.deliver",
                    kind="delivery",
                    attributes=_delivery_attributes(event, streaming=True),
                )
            except Exception:
                return await original_send_streaming(
                    generator,
                    use_fallback,
                    *args,
                    **kwargs,
                )
            if isinstance(span, NoopTraceSpan):
                return await original_send_streaming(
                    generator,
                    use_fallback,
                    *args,
                    **kwargs,
                )

            accumulator = _StreamingDeliveryAccumulator()
            proxy = _stream_proxy(generator, parent_span, accumulator)
            stream_token = _inside_stream_delivery.set(True)
            try:
                with span:
                    try:
                        return await original_send_streaming(
                            proxy,
                            use_fallback,
                            *args,
                            **kwargs,
                        )
                    finally:
                        _record_stream_delivery(span, accumulator)
            finally:
                _inside_stream_delivery.reset(stream_token)

        traced_send_streaming_fn = traced_send_streaming

    try:
        event.send = traced_send
        if traced_send_streaming_fn is not None:
            event.send_streaming = traced_send_streaming_fn
        event._astrbot_trace_message_instrumented = True
    except Exception:
        try:
            event.send = original_send
            if traced_send_streaming_fn is not None:
                event.send_streaming = original_send_streaming
        except Exception:
            pass


def _start_delivery_span(
    event: Any,
    operation: str,
    *,
    streaming: bool,
) -> TraceSpan | NoopTraceSpan:
    """Start a delivery child only when a live Trace context already exists."""

    service = current_trace_service()
    if service is None or isinstance(service.current_span(), NoopTraceSpan):
        return NoopTraceSpan()
    try:
        return service.start_span(
            operation,
            kind="delivery",
            attributes=_delivery_attributes(event, streaming=streaming),
        )
    except Exception:
        return NoopTraceSpan()


def _delivery_attributes(event: Any, *, streaming: bool) -> dict[str, Any]:
    """Build adapter-neutral metadata without touching platform SDK payloads."""

    return {
        "platform_id": getattr(getattr(event, "platform_meta", None), "id", None),
        "platform_name": getattr(getattr(event, "platform_meta", None), "name", None),
        "umo": getattr(event, "unified_msg_origin", None),
        "streaming": streaming,
    }


async def _stream_proxy(
    source: AsyncGenerator[MessageChain, None],
    parent_span: TraceSpan,
    accumulator: _StreamingDeliveryAccumulator,
) -> AsyncGenerator[MessageChain, None]:
    """Advance Agent output under its original parent while delivery is active.

    Args:
        source: The original response generator owned by the Agent workflow.
        parent_span: The pre-delivery parent span captured at call time.
        accumulator: Final-state-only delivery summary collector.
    """

    source_closed = False
    try:
        while True:
            try:
                with parent_span.activate():
                    chain = await anext(source)
            except StopAsyncIteration:
                source_closed = True
                return
            accumulator.observe(chain)
            yield chain
    except (GeneratorExit, asyncio.CancelledError):
        if not source_closed:
            with contextlib.suppress(Exception):
                with parent_span.activate():
                    await source.aclose()
        raise
    finally:
        if not source_closed:
            with contextlib.suppress(Exception):
                with parent_span.activate():
                    await source.aclose()


class _StreamingDeliveryAccumulator:
    """Collect a bounded final semantic summary without persisting stream deltas."""

    def __init__(self) -> None:
        self.chain_count = 0
        self.audio_chunk_count = 0
        self.reasoning_parts: list[str] = []
        self.text_parts: list[str] = []
        self.component_types: set[str] = set()
        self.text_truncated = False

    def observe(self, chain: MessageChain) -> None:
        """Incorporate one yielded response chain into the final summary.

        Args:
            chain: One response chain yielded by the Agent workflow.
        """

        self.chain_count += 1
        if chain.type == "audio_chunk":
            self.audio_chunk_count += 1
            return
        self.component_types.update(
            str(getattr(component, "type", type(component).__name__))
            for component in chain.chain
        )
        text = chain.get_plain_text()
        if not text:
            return
        target = self.reasoning_parts if chain.type == "reasoning" else self.text_parts
        current_size = sum(len(part) for part in self.text_parts) + sum(
            len(part) for part in self.reasoning_parts
        )
        remaining = _STREAM_TEXT_LIMIT - current_size
        if remaining <= 0:
            self.text_truncated = True
            return
        target.append(text[:remaining])
        if len(text) > remaining:
            self.text_truncated = True

    def manifest(self) -> dict[str, Any]:
        """Return one final-state-only artifact payload for the delivery Span."""

        return {
            "streaming": True,
            "chain_count": self.chain_count,
            "audio_chunk_count": self.audio_chunk_count,
            "text": "".join(self.text_parts),
            "reasoning": "".join(self.reasoning_parts),
            "component_types": sorted(self.component_types),
            "text_truncated": self.text_truncated,
        }


def _record_stream_delivery(
    span: TraceSpan,
    accumulator: _StreamingDeliveryAccumulator,
) -> None:
    """Persist one terminal semantic record even when delivery raises or cancels."""

    try:
        manifest = accumulator.manifest()
        span.set_attributes(
            chain_count=manifest["chain_count"],
            audio_chunk_count=manifest["audio_chunk_count"],
            text_truncated=manifest["text_truncated"],
        )
        span.record_json(
            "response.delivery",
            manifest,
            metadata={"direction": "outgoing", "streaming": True},
        )
    except Exception:
        span.mark_degraded("stream_delivery_serialization_failed")
