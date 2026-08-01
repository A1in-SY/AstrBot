"""Type-preserving automatic tracing for Core-managed Provider instances."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import inspect
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any

from .context import current_trace_service, trace_suppressed
from .serialization import provider_request_manifest, provider_response_manifest
from .service import NoopTraceSpan, TraceService, TraceSpan

_provider_families: contextvars.ContextVar[frozenset[str]] = contextvars.ContextVar(
    "astrbot_trace_provider_families",
    default=frozenset(),
)

_METHODS: tuple[tuple[str, str, str], ...] = (
    ("text_chat", "chat", "model.call"),
    ("text_chat_stream", "chat", "model.call"),
    ("get_text", "stt", "stt.call"),
    ("get_audio", "tts", "tts.call"),
    ("get_audio_stream", "tts", "tts.call"),
    ("get_embedding", "embedding", "embedding.call"),
    ("get_embeddings", "embedding", "embedding.call"),
    ("get_embeddings_batch", "embedding", "embedding.call"),
    ("rerank", "rerank", "rerank.call"),
)


def instrument_provider(provider: Any, trace_service: TraceService | None) -> None:
    """Wrap a managed Provider instance without replacing its concrete type.

    Args:
        provider: A Core-managed Provider instance.
        trace_service: Core tracing service that owns direct-provider root traces.
    """

    if trace_service is None:
        return
    try:
        wrapped_methods = getattr(provider, "_astrbot_trace_wrapped_methods", None)
    except Exception:
        return
    if wrapped_methods is None:
        wrapped_methods = set()
        try:
            setattr(provider, "_astrbot_trace_wrapped_methods", wrapped_methods)
        except Exception:
            return
    for method_name, family, operation in _METHODS:
        if method_name in wrapped_methods:
            continue
        try:
            original = getattr(provider, method_name, None)
        except Exception:
            continue
        if original is None or not callable(original):
            continue
        if inspect.isasyncgenfunction(original):
            wrapper = _wrap_stream(
                provider,
                original,
                family,
                operation,
                trace_service,
            )
        elif inspect.iscoroutinefunction(original):
            wrapper = _wrap_coroutine(
                provider,
                original,
                family,
                operation,
                trace_service,
            )
        else:
            continue
        try:
            setattr(provider, method_name, wrapper)
            wrapped_methods.add(method_name)
        except Exception:
            continue
    if "test" not in wrapped_methods:
        try:
            original_test = getattr(provider, "test", None)
        except Exception:
            return
        if original_test is not None and inspect.iscoroutinefunction(original_test):
            try:
                setattr(provider, "test", _wrap_probe(original_test, trace_service))
                wrapped_methods.add("test")
            except Exception:
                return


def _wrap_coroutine(
    provider: Any,
    original: Callable[..., Awaitable[Any]],
    family: str,
    operation: str,
    configured_service: TraceService,
) -> Callable[..., Awaitable[Any]]:
    @functools.wraps(original)
    async def traced(*args: Any, **kwargs: Any) -> Any:
        if family in _provider_families.get() or trace_suppressed():
            return await original(*args, **kwargs)
        token = _provider_families.set(_provider_families.get() | {family})
        try:
            span, owns_span = _safe_start_provider_span(
                provider,
                operation,
                original.__name__,
                configured_service,
            )
            with _span_scope(span, owns_span):
                _record_request(span, original.__name__, args, kwargs)
                result = await original(*args, **kwargs)
                if not isinstance(span, NoopTraceSpan):
                    span.set_attributes(result_type=type(result).__name__)
                    _record_response(span, result)
                return result
        except BaseException as exc:
            if "span" in locals() and not isinstance(span, NoopTraceSpan):
                span.add_event("provider.error", exception_type=type(exc).__name__)
            raise
        finally:
            _provider_families.reset(token)

    return traced


def _wrap_stream(
    provider: Any,
    original: Callable[..., AsyncGenerator[Any, None]],
    family: str,
    operation: str,
    configured_service: TraceService,
) -> Callable[..., AsyncGenerator[Any, None]]:
    @functools.wraps(original)
    async def traced(*args: Any, **kwargs: Any) -> AsyncGenerator[Any, None]:
        if family in _provider_families.get() or trace_suppressed():
            async for item in original(*args, **kwargs):
                yield item
            return
        span, owns_span = _safe_start_provider_span(
            provider,
            operation,
            original.__name__,
            configured_service,
        )
        _record_request(span, original.__name__, args, kwargs)
        original_generator = original(*args, **kwargs)
        accumulator = _StreamingSemanticAccumulator()
        chunk_count = 0
        original_closed = False
        terminal_recorded = False
        try:
            while True:
                token = _provider_families.set(_provider_families.get() | {family})
                try:
                    with _activate_span(span):
                        item = await anext(original_generator)
                except StopAsyncIteration:
                    original_closed = True
                    _record_stream_terminal(span, accumulator, chunk_count)
                    terminal_recorded = True
                    if owns_span:
                        span.finish()
                    break
                finally:
                    _provider_families.reset(token)
                chunk_count += 1
                try:
                    accumulator.observe(item)
                except Exception:
                    span.mark_degraded("provider_response_serialization_failed")
                yield item
        except GeneratorExit:
            await _close_stream_generator(original_generator, span)
            original_closed = True
            _record_stream_terminal(span, accumulator, chunk_count)
            terminal_recorded = True
            if owns_span:
                span.finish(status="cancelled", outcome="generator_closed")
            raise
        except asyncio.CancelledError:
            await _close_stream_generator(original_generator, span)
            original_closed = True
            _record_stream_terminal(span, accumulator, chunk_count)
            terminal_recorded = True
            if owns_span:
                span.finish(status="cancelled", outcome="cancelled")
            raise
        except BaseException as exc:
            if not isinstance(span, NoopTraceSpan):
                span.add_event("provider.error", exception_type=type(exc).__name__)
            await _close_stream_generator(original_generator, span)
            original_closed = True
            _record_stream_terminal(span, accumulator, chunk_count)
            terminal_recorded = True
            if owns_span:
                span.finish(status="error", outcome="exception")
            raise
        finally:
            if not original_closed:
                await _close_stream_generator(original_generator, span)
            if not terminal_recorded:
                _record_stream_terminal(span, accumulator, chunk_count)

    return traced


def _wrap_probe(
    original: Callable[..., Awaitable[Any]],
    trace_service: TraceService,
) -> Callable[..., Awaitable[Any]]:
    @functools.wraps(original)
    async def traced(*args: Any, **kwargs: Any) -> Any:
        with trace_service.suppress():
            return await original(*args, **kwargs)

    return traced


def _start_provider_span(
    provider: Any,
    operation: str,
    method: str,
    configured_service: TraceService,
) -> tuple[TraceSpan | NoopTraceSpan, bool]:
    service = current_trace_service() or configured_service
    active_span = service.current_span()
    if (
        not isinstance(active_span, NoopTraceSpan)
        and active_span._span_state.operation == operation
    ):
        active_span.set_attributes(
            provider_method=method,
            **_provider_attributes(provider),
        )
        return active_span, False
    attributes = {"provider_method": method, **_provider_attributes(provider)}
    if active_span.trace_id is not None:
        return service.start_span(
            operation, kind="provider", attributes=attributes
        ), True
    return service.start_root(operation, kind="provider", attributes=attributes), True


def _safe_start_provider_span(
    provider: Any,
    operation: str,
    method: str,
    configured_service: TraceService,
) -> tuple[TraceSpan | NoopTraceSpan, bool]:
    """Start a trace span without allowing tracing failures to affect Provider I/O."""

    try:
        return _start_provider_span(provider, operation, method, configured_service)
    except Exception:
        return NoopTraceSpan(), False


def _record_request(
    span: TraceSpan | NoopTraceSpan,
    method: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> None:
    if isinstance(span, NoopTraceSpan):
        return
    try:
        span.record_json(
            "provider.request", provider_request_manifest(method, args, kwargs)
        )
    except Exception:
        return


def _record_stream_terminal(
    span: TraceSpan | NoopTraceSpan,
    accumulator: _StreamingSemanticAccumulator,
    chunk_count: int,
) -> None:
    """Record one final or best-effort partial stream snapshot."""

    if isinstance(span, NoopTraceSpan):
        return
    try:
        span.set_attributes(
            chunk_count=chunk_count,
            stream_completed=accumulator.completed,
        )
        span.record_json(
            "provider.response",
            accumulator.manifest(),
            metadata={"partial": not accumulator.completed},
        )
    except Exception:
        return


def _record_response(span: TraceSpan | NoopTraceSpan, result: Any) -> None:
    """Capture a Provider response without allowing serializer failures to escape."""

    if isinstance(span, NoopTraceSpan):
        return
    try:
        span.record_json("provider.response", provider_response_manifest(result))
    except Exception:
        try:
            span.mark_degraded("provider_response_serialization_failed")
        except Exception:
            return


@contextlib.contextmanager
def _activate_span(span: TraceSpan | NoopTraceSpan):
    """Install a span only while a downstream async generator is advanced."""

    if isinstance(span, NoopTraceSpan):
        yield
        return
    with span.activate():
        yield


async def _close_stream_generator(
    generator: AsyncGenerator[Any, None],
    span: TraceSpan | NoopTraceSpan,
) -> None:
    """Close a stream without leaking its Trace context to its consumer."""

    try:
        with _activate_span(span):
            await generator.aclose()
    except Exception:
        return


class _StreamingSemanticAccumulator:
    """Freeze known semantic stream fields while each item is observed."""

    def __init__(self) -> None:
        self._final: dict[str, Any] | None = None
        self._last: dict[str, Any] | None = None
        self._text_parts: list[Any] = []
        self._reasoning_parts: list[Any] = []
        self._last_usage: Any = None
        self._last_response_id: Any = None

    @property
    def completed(self) -> bool:
        return self._final is not None

    def observe(self, item: Any) -> None:
        manifest = provider_response_manifest(item)
        self._last = manifest
        if manifest.get("response_type") != "LLMResponse":
            self._final = manifest
            return
        if not manifest.get("is_chunk"):
            self._final = manifest
            return
        completion_text = manifest.get("completion_text")
        if completion_text:
            self._text_parts.append(completion_text)
        reasoning_content = manifest.get("reasoning_content")
        if reasoning_content:
            self._reasoning_parts.append(reasoning_content)
        if manifest.get("usage") is not None:
            self._last_usage = manifest["usage"]
        if manifest.get("response_id") is not None:
            self._last_response_id = manifest["response_id"]

    def manifest(self) -> dict[str, Any]:
        if self._final is not None:
            return self._final
        if self._last is None:
            return {"response_type": "stream", "partial": True}
        if self._last.get("response_type") != "LLMResponse":
            return {**self._last, "partial": True}
        return {
            "response_type": "LLMResponse",
            "partial": True,
            "role": self._last.get("role"),
            "completion_text": "".join(
                part for part in self._text_parts if isinstance(part, str)
            ),
            "reasoning_content": "".join(
                part for part in self._reasoning_parts if isinstance(part, str)
            ),
            "tools_call_name": self._last.get("tools_call_name"),
            "tools_call_args": self._last.get("tools_call_args"),
            "tools_call_ids": self._last.get("tools_call_ids"),
            "tools_call_extra_content": self._last.get("tools_call_extra_content"),
            "response_id": self._last_response_id,
            "usage": self._last_usage,
        }


def _provider_attributes(provider: Any) -> dict[str, Any]:
    config = getattr(provider, "provider_config", {})
    if not isinstance(config, dict):
        config = {}
    model_getter = getattr(provider, "get_model", None)
    try:
        model = (
            model_getter()
            if callable(model_getter)
            else getattr(provider, "model_name", "")
        )
    except Exception:
        model = None
    return {
        "provider_id": config.get("id"),
        "provider_type": config.get("type"),
        "model": model,
    }


def _span_scope(
    span: TraceSpan | NoopTraceSpan,
    owns_span: bool,
) -> contextlib.AbstractContextManager[TraceSpan | NoopTraceSpan]:
    if owns_span:
        return span
    return contextlib.nullcontext(span)
