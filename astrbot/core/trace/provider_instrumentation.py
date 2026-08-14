"""Type-preserving automatic tracing for Core-managed Provider instances."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import inspect
import math
import wave
from collections.abc import AsyncGenerator, Awaitable, Callable
from pathlib import Path
from typing import Any

from .context import current_trace_service, trace_suppressed
from .outbound import (
    OutboundCallRecorder,
    OutboundRequestSnapshot,
    record_active_outbound_failure,
    record_outbound_first_chunk,
    record_outbound_response_summary,
)
from .serialization import provider_request_manifest, provider_response_manifest
from .service import NoopTraceSpan, TraceService, TraceSpan

_provider_families: contextvars.ContextVar[frozenset[str]] = contextvars.ContextVar(
    "astrbot_trace_provider_families",
    default=frozenset(),
)
_embedding_batch_dispatch: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "astrbot_trace_embedding_batch_dispatch",
    default=False,
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
        # The batch coordinator does not itself perform I/O. Keep nested batch
        # requests visible so every real ``get_embeddings`` dispatch (and its
        # retry) is recorded on the same logical ``embedding.call`` span.
        exposes_nested_dispatches = original.__name__ == "get_embeddings_batch"
        batch_token = (
            _embedding_batch_dispatch.set(True) if exposes_nested_dispatches else None
        )
        token = (
            None
            if exposes_nested_dispatches
            else _provider_families.set(_provider_families.get() | {family})
        )
        try:
            span, owns_span = _safe_start_provider_span(
                provider,
                operation,
                original.__name__,
                configured_service,
            )
            with _span_scope(span, owns_span):
                _record_request(span, original.__name__, args, kwargs)
                fallback = _start_fallback_outbound(
                    span,
                    provider,
                    original.__name__,
                    args,
                    kwargs,
                )
                try:
                    result = await original(*args, **kwargs)
                except BaseException as exc:
                    batch_coordinator = original.__name__ == "get_embeddings_batch"
                    batch_coordinator_owns_terminal = (
                        _embedding_batch_dispatch.get()
                        and original.__name__ in {"get_embedding", "get_embeddings"}
                    )
                    if not batch_coordinator and not batch_coordinator_owns_terminal:
                        if fallback is not None:
                            fallback[0].record_failed(
                                exc,
                                attempt_number=fallback[1],
                            )
                        else:
                            record_active_outbound_failure(
                                exc,
                                span=span if isinstance(span, TraceSpan) else None,
                            )
                    _record_provider_error(span, exc)
                    raise
                if fallback is not None:
                    fallback[0].record_completed(result, attempt_number=fallback[1])
                if not isinstance(span, NoopTraceSpan):
                    span.set_attributes(result_type=type(result).__name__)
                    _record_provider_domain_summary(
                        span,
                        original.__name__,
                        args,
                        kwargs,
                        result,
                    )
                    _record_response(span, result)
                return result
        finally:
            if token is not None:
                _provider_families.reset(token)
            if batch_token is not None:
                _embedding_batch_dispatch.reset(batch_token)

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
        fallback = _start_fallback_outbound(
            span,
            provider,
            original.__name__,
            args,
            kwargs,
        )
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
                    if fallback is not None:
                        fallback[0].record_completed(
                            original_generator,
                            attempt_number=fallback[1],
                        )
                    _record_stream_terminal(span, accumulator, chunk_count)
                    terminal_recorded = True
                    if owns_span:
                        span.finish()
                    break
                finally:
                    _provider_families.reset(token)
                chunk_count += 1
                if _is_semantic_stream_item(item):
                    record_outbound_first_chunk(
                        item,
                        span=span if isinstance(span, TraceSpan) else None,
                    )
                try:
                    accumulator.observe(item)
                except Exception:
                    span.mark_degraded("provider_response_serialization_failed")
                yield item
        except GeneratorExit as exc:
            if fallback is not None:
                fallback[0].record_failed(exc, attempt_number=fallback[1])
            else:
                record_active_outbound_failure(
                    exc,
                    span=span if isinstance(span, TraceSpan) else None,
                )
            await _close_stream_generator(original_generator, span)
            original_closed = True
            _record_stream_terminal(span, accumulator, chunk_count)
            terminal_recorded = True
            if owns_span:
                span.finish(status="cancelled", outcome="generator_closed")
            raise
        except asyncio.CancelledError as exc:
            if fallback is not None:
                fallback[0].record_failed(exc, attempt_number=fallback[1])
            else:
                record_active_outbound_failure(
                    exc,
                    span=span if isinstance(span, TraceSpan) else None,
                )
            await _close_stream_generator(original_generator, span)
            original_closed = True
            _record_stream_terminal(span, accumulator, chunk_count)
            terminal_recorded = True
            if owns_span:
                span.finish(status="cancelled", outcome="cancelled")
            raise
        except BaseException as exc:
            if fallback is not None:
                fallback[0].record_failed(exc, attempt_number=fallback[1])
            else:
                record_active_outbound_failure(
                    exc,
                    span=span if isinstance(span, TraceSpan) else None,
                )
            _record_provider_error(span, exc)
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
        requested_model = kwargs.get("model")
        if isinstance(requested_model, str) and requested_model:
            span.set_attributes(requested_model=requested_model)
        span.record_json(
            "provider.request", provider_request_manifest(method, args, kwargs)
        )
    except Exception:
        return


def _record_provider_error(
    span: TraceSpan | NoopTraceSpan,
    error: BaseException,
) -> None:
    """Record a Provider failure without affecting the original exception."""

    if isinstance(span, NoopTraceSpan):
        return
    try:
        span.add_event("provider.error", exception_type=type(error).__name__)
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
            response_chunk_count=chunk_count,
            stream_completed=accumulator.completed,
        )
        manifest = accumulator.manifest()
        span.record_json(
            "provider.response",
            manifest,
            metadata={"partial": not accumulator.completed},
        )
        record_outbound_response_summary(
            usage=manifest.get("usage"),
            response_id=manifest.get("response_id"),
            span=span,
        )
    except Exception:
        return


def _record_response(span: TraceSpan | NoopTraceSpan, result: Any) -> None:
    """Capture a Provider response without allowing serializer failures to escape."""

    if isinstance(span, NoopTraceSpan):
        return
    try:
        manifest = provider_response_manifest(result)
        span.record_json("provider.response", manifest)
        record_outbound_response_summary(
            usage=manifest.get("usage"),
            response_id=manifest.get("response_id"),
            span=span,
        )
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
        "effective_model": model,
    }


_BUILTIN_PROVIDER_ROUTES: dict[str, tuple[str, str, str]] = {
    "azure_tts": ("azure.speech.synthesis", "client.post", "/cognitiveservices/v1"),
    "bailian_rerank": (
        "bailian.rerank",
        "client.post",
        "/api/v1/services/rerank/text-rerank/text-rerank",
    ),
    "dashscope_embedding": (
        "dashscope.embedding",
        "Embedding.call",
        "/api/v1/services/embeddings/{task}/{task}",
    ),
    "dashscope_tts": (
        "dashscope.tts",
        "SpeechSynthesizer.call",
        "/api/v1/services/aigc/multimodal-generation/generation",
    ),
    "edge_tts": (
        "edge.tts",
        "edge_tts.Communicate",
        "/consumer/speech/synthesize/readaloud/edge/v1",
    ),
    "elevenlabs_tts_api": (
        "elevenlabs.text_to_speech",
        "client.post",
        "/v1/text-to-speech/{voice_id}",
    ),
    "fishaudio_tts_api": ("fishaudio.tts", "client.tts.convert", "/v1/tts"),
    "gemini_embedding": (
        "gemini.models.embed_content",
        "client.models.embed_content",
        "/models/{model}:embedContent",
    ),
    "gemini_tts": (
        "gemini.models.generate_content",
        "client.models.generate_content",
        "/models/{model}:generateContent",
    ),
    "genie_tts": ("genie.tts", "client.call", "/tts"),
    "gsv_tts_selfhost": ("gsv.local", "model.infer", "local"),
    "gsvi_tts_api": ("gsv.infer_single", "session.post", "/infer_single"),
    "mimo_stt_api": ("mimo.chat.completions", "client.post", "/chat/completions"),
    "mimo_tts_api": ("mimo.chat.completions", "client.post", "/chat/completions"),
    "minimax_tts_api": ("minimax.text_to_audio", "session.post", "/v1/t2a_v2"),
    "nvidia_embedding": ("nvidia.embeddings", "client.post", "/v1/embeddings"),
    "nvidia_rerank": (
        "nvidia.rerank",
        "client.post",
        "/v1/retrieval/{model}/reranking",
    ),
    "ollama_embedding": ("ollama.embed", "client.post", "/api/embed"),
    "openai_embedding": (
        "openai.embeddings",
        "client.embeddings.create",
        "/embeddings",
    ),
    "openai_tts_api": (
        "openai.audio.speech",
        "client.audio.speech.create",
        "/audio/speech",
    ),
    "openai_whisper_api": (
        "openai.audio.transcriptions",
        "client.audio.transcriptions.create",
        "/audio/transcriptions",
    ),
    "openai_whisper_selfhost": ("whisper.local", "model.transcribe", "local"),
    "sensevoice_stt_selfhost": ("sensevoice.local", "model.generate", "local"),
    "tei_rerank": ("tei.rerank", "client.post", "/rerank"),
    "vllm_rerank": ("vllm.rerank", "client.post", "/v1/rerank"),
    "volcengine_tts": ("volcengine.tts", "session.post", "/api/v1/tts"),
    "xinference_rerank": ("xinference.rerank", "model.rerank", "/v1/rerank"),
    "xinference_stt": (
        "xinference.audio.transcriptions",
        "session.post",
        "/v1/audio/transcriptions",
    ),
}


def _start_fallback_outbound(
    span: TraceSpan | NoopTraceSpan,
    provider: Any,
    method: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> tuple[OutboundCallRecorder, int] | None:
    """Provide route diagnostics for simple built-in adapters without deep hooks."""

    if not isinstance(span, TraceSpan) or method == "get_embeddings_batch":
        return None
    config = getattr(provider, "provider_config", {})
    if not isinstance(config, dict):
        return None
    provider_type = config.get("type")
    if provider_type in {"azure_tts", "gsv_tts_selfhost"} or bool(
        getattr(provider, "_astrbot_deep_outbound", False)
    ):
        return None
    route = _BUILTIN_PROVIDER_ROUTES.get(str(provider_type))
    if route is None:
        return None
    api_family, sdk_operation, resource_path = route
    base_url = next(
        (
            config.get(key)
            for key in (
                "embedding_api_base",
                "rerank_api_base",
                "gemini_tts_api_base",
                "api_base",
            )
            if config.get(key)
        ),
        None,
    )
    if base_url is None:
        base_url = getattr(provider, "base_url", None) or getattr(
            provider, "api_base", None
        )
    if base_url is None:
        client = getattr(provider, "client", None)
        base_url = getattr(client, "base_url", None)
    if base_url is not None:
        base_url = str(base_url)
    values = dict(kwargs)
    if len(args) == 1:
        values["input"] = args[0]
    elif args:
        values["input"] = list(args)
    values.update(_provider_effective_controls(provider, method))
    recorder = OutboundCallRecorder(
        OutboundRequestSnapshot(
            api_family=api_family,
            sdk_operation=sdk_operation,
            base_url=base_url,
            resource_path=resource_path,
            route_resolution=(
                "unavailable"
                if resource_path == "local"
                else (
                    "sdk_declared"
                    if not any(
                        marker in sdk_operation
                        for marker in ("client.post", "session.post")
                    )
                    else "constructed"
                )
            ),
            streaming=method.endswith("stream"),
            timeout_seconds=config.get("timeout") or config.get("gemini_tts_timeout"),
            proxy_configured=bool(config.get("proxy")),
            parameters=values,
        )
    )
    attempt_number = recorder.record_attempt()
    return recorder, attempt_number


def _record_provider_domain_summary(
    span: TraceSpan,
    method: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    result: Any,
) -> None:
    """Attach content-free STT/TTS/embedding/rerank request and result metrics."""

    attributes: dict[str, Any] = {}
    if method in {"get_embedding", "get_embeddings", "get_embeddings_batch"}:
        inputs = args[0] if args else kwargs.get("text", kwargs.get("texts"))
        texts = inputs if isinstance(inputs, list) else [inputs]
        attributes["input_count"] = len(texts)
        attributes["input_chars"] = sum(
            len(item) for item in texts if isinstance(item, str)
        )
        vectors = result if isinstance(result, list) else []
        if vectors and isinstance(vectors[0], (int, float)):
            attributes["vector_count"] = 1
            attributes["embedding_dimensions"] = len(vectors)
        elif vectors and isinstance(vectors[0], list):
            attributes["vector_count"] = len(vectors)
            attributes["embedding_dimensions"] = len(vectors[0])
        for key in ("batch_size", "tasks_limit", "max_retries"):
            if isinstance(kwargs.get(key), int):
                attributes[key] = kwargs[key]
        if method == "get_embeddings_batch":
            batch_size = kwargs.get("batch_size", args[1] if len(args) > 1 else 16)
            tasks_limit = kwargs.get("tasks_limit", args[2] if len(args) > 2 else 3)
            if isinstance(batch_size, int) and batch_size > 0:
                attributes["batch_size"] = batch_size
                attributes["batch_count"] = math.ceil(len(texts) / batch_size)
            if isinstance(tasks_limit, int):
                attributes["concurrency"] = tasks_limit
    elif method == "rerank":
        query = args[0] if args else kwargs.get("query")
        documents = args[1] if len(args) > 1 else kwargs.get("documents")
        attributes["query_chars"] = len(query) if isinstance(query, str) else 0
        attributes["document_count"] = (
            len(documents) if isinstance(documents, list) else 0
        )
        attributes["result_count"] = len(result) if isinstance(result, list) else 0
        scores = [
            score
            for item in (result if isinstance(result, list) else [])
            if isinstance(
                score := (
                    item.get("relevance_score")
                    if isinstance(item, dict)
                    else getattr(item, "relevance_score", None)
                ),
                (int, float),
            )
        ]
        if scores:
            attributes["score_min"] = min(scores)
            attributes["score_max"] = max(scores)
        top_n = args[2] if len(args) > 2 else kwargs.get("top_n")
        if isinstance(top_n, int):
            attributes["top_n"] = top_n
    elif method == "get_text":
        attributes["audio_source_type"] = "media_reference"
        audio_reference = args[0] if args else kwargs.get("audio_url")
        audio_size = _local_file_size(audio_reference)
        if audio_size is not None:
            attributes["audio_bytes"] = audio_size
        audio_duration = _local_audio_duration_seconds(audio_reference)
        if audio_duration is not None:
            attributes["audio_duration_seconds"] = audio_duration
        attributes["result_chars"] = len(result) if isinstance(result, str) else 0
    elif method in {"get_audio", "get_audio_stream"}:
        text = args[0] if args else kwargs.get("text")
        attributes["input_chars"] = len(text) if isinstance(text, str) else 0
        if isinstance(result, (bytes, bytearray, memoryview)):
            attributes["audio_bytes"] = len(result)
        else:
            audio_size = _local_file_size(result)
            if audio_size is not None:
                attributes["audio_bytes"] = audio_size
            audio_duration = _local_audio_duration_seconds(result)
            if audio_duration is not None:
                attributes["audio_duration_seconds"] = audio_duration
    if attributes:
        span.set_attributes(**attributes)


def _provider_effective_controls(provider: Any, method: str) -> dict[str, Any]:
    """Read allowlisted adapter fields that are finalized during construction."""

    controls: dict[str, Any] = {}
    try:
        model = (
            provider.get_model()
            if callable(getattr(provider, "get_model", None))
            else None
        )
    except Exception:
        model = None
    if isinstance(model, str) and model:
        controls["model"] = model
    candidates = {
        "voice": ("voice", "voice_name", "voice_id", "voice_type"),
        "response_format": ("audio_format", "output_format", "response_format"),
        "language": ("language",),
        "sample_rate": ("sample_rate",),
        "speed": ("speed",),
    }
    for target, names in candidates.items():
        for name in names:
            value = getattr(provider, name, None)
            if value is None or isinstance(value, (bool, int, float, str)):
                if value not in (None, ""):
                    controls[target] = value
                    break
    config = getattr(provider, "provider_config", {})
    if isinstance(config, dict):
        for source, target in (
            ("embedding_dimensions", "dimensions"),
            ("encoding_format", "encoding_format"),
            ("response_format", "response_format"),
            ("language", "language"),
            ("sample_rate", "sample_rate"),
        ):
            value = config.get(source)
            if value is None or isinstance(value, (bool, int, float, str)):
                if value not in (None, ""):
                    controls.setdefault(target, value)
    if method == "rerank":
        controls["return_documents"] = False
    return controls


def _local_file_size(value: Any) -> int | None:
    """Return a local output/input size without retaining its filesystem path."""

    if not isinstance(value, (str, Path)):
        return None
    try:
        path = Path(value)
        return path.stat().st_size if path.is_file() else None
    except (OSError, ValueError, TypeError):
        return None


def _local_audio_duration_seconds(value: Any) -> float | None:
    """Return WAV duration when a Provider exposes a readable local file."""

    if not isinstance(value, (str, Path)):
        return None
    try:
        with wave.open(str(Path(value)), "rb") as audio_file:
            frame_rate = audio_file.getframerate()
            if frame_rate <= 0:
                return None
            return round(audio_file.getnframes() / frame_rate, 3)
    except (OSError, ValueError, TypeError, wave.Error, EOFError):
        return None


def _is_semantic_stream_item(item: Any) -> bool:
    """Return whether a yielded Provider item contains Core-consumable output."""

    try:
        if any(
            getattr(item, field, None)
            for field in (
                "completion_text",
                "reasoning_content",
                "result_chain",
                "tools_call_name",
            )
        ):
            return True
        if hasattr(item, "is_chunk"):
            return not bool(getattr(item, "is_chunk", False))
        return item is not None
    except Exception:
        return False


def _span_scope(
    span: TraceSpan | NoopTraceSpan,
    owns_span: bool,
) -> contextlib.AbstractContextManager[TraceSpan | NoopTraceSpan]:
    if owns_span:
        return span
    return contextlib.nullcontext(span)
