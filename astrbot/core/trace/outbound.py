"""Fail-open diagnostics for Core-owned outbound calls."""

from __future__ import annotations

import contextvars
import hashlib
import time
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .context import current_trace_service
from .service import TraceSpan

_active_outbound_attempt: contextvars.ContextVar[tuple[Any, int] | None] = (
    contextvars.ContextVar("astrbot_trace_active_outbound_attempt", default=None)
)

_CONTENT_FIELDS = frozenset(
    {
        "additional_messages",
        "audio",
        "contents",
        "documents",
        "files",
        "image",
        "images",
        "input",
        "inputs",
        "instructions",
        "messages",
        "prompt",
        "query",
        "text",
    }
)
_SENSITIVE_NAMES = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth_token",
        "authorization",
        "bearer_token",
        "client_secret",
        "cookie",
        "credential",
        "headers",
        "id_token",
        "password",
        "proxy",
        "proxy_url",
        "refresh_token",
        "secret",
        "set_cookie",
    }
)
_SAFE_SCALAR_FIELDS = frozenset(
    {
        "batch_size",
        "auto_save_history",
        "audio_format",
        "dimensions",
        "dimension",
        "encoding_format",
        "frequency_penalty",
        "language",
        "input_type",
        "max_completion_tokens",
        "max_concurrent_subagents",
        "max_output_tokens",
        "max_tokens",
        "model",
        "parallel_tool_calls",
        "plan_mode",
        "presence_penalty",
        "response_mime_type",
        "output_dimensionality",
        "output_format",
        "response_mode",
        "reasoning_effort",
        "return_documents",
        "recursion_limit",
        "sample_rate",
        "seed",
        "speed",
        "store",
        "stream",
        "stream_mode",
        "truncate",
        "truncation_direction",
        "raw_scores",
        "return_text",
        "service_tier",
        "temperature",
        "thinking_budget",
        "thinking_level",
        "task_type",
        "timeout",
        "timeout_seconds",
        "tool_choice",
        "tool_name",
        "top_k",
        "top_n",
        "top_p",
        "volume",
        "voice",
    }
)
_SAFE_STRUCTURED_FIELDS = frozenset(
    {
        "modalities",
        "output_config",
        "reasoning",
        "response_modalities",
        "safety_settings",
        "thinking",
        "thinking_config",
    }
)
_SAFE_INPUT_SUMMARY_FIELDS = frozenset(
    {
        "audio_duration_seconds",
        "audio_bytes",
        "audio_source_type",
        "conversation_id_hash",
        "credential_rotation_count",
        "document_count",
        "input_chars",
        "input_count",
        "mcp_server_name",
        "mcp_transport",
        "message_count",
        "prompt_cache_enabled",
        "query_chars",
        "remote_resource_id_hash",
        "request_batch_count",
        "search_engine",
        "thread_id_hash",
        "tool_count",
    }
)


@dataclass(frozen=True)
class OutboundRequestSnapshot:
    """Describe one effective request at a Core-owned call boundary.

    Args:
        api_family: Stable family name such as ``openai.responses``.
        sdk_operation: Concrete SDK or client method invoked by Core.
        http_method: HTTP method when the boundary uses HTTP semantics.
        base_url: Configured base URL. Credentials, query, and fragment are removed.
        resource_path: Static or identifier-templated resource path.
        route_resolution: ``constructed``, ``sdk_declared``, or ``unavailable``.
        streaming: Whether the response is consumed as a stream.
        timeout_seconds: Effective request timeout when known.
        proxy_configured: Whether a proxy is configured, never the proxy value.
        parameters: Final parameters passed to the SDK/client.
        input_summary: Additional adapter-owned, content-free input facts.
        transformations: Parameter conversions applied before dispatch.
        ignored_fields: Parameters deliberately removed before dispatch.
    """

    api_family: str
    sdk_operation: str
    http_method: str = "POST"
    base_url: str | None = None
    resource_path: str | None = None
    route_resolution: str = "unavailable"
    streaming: bool = False
    timeout_seconds: float | int | None = None
    proxy_configured: bool = False
    parameters: Mapping[str, Any] = field(default_factory=dict)
    input_summary: Mapping[str, Any] = field(default_factory=dict)
    transformations: tuple[str, ...] = ()
    ignored_fields: tuple[str, ...] = ()


class OutboundCallRecorder:
    """Record a request variant and its transport attempts on the active span."""

    def __init__(
        self,
        snapshot: OutboundRequestSnapshot,
        *,
        span: TraceSpan | None = None,
    ) -> None:
        """Prepare one request variant without affecting the business call.

        Args:
            snapshot: Effective request metadata captured immediately before I/O.
        """

        self.snapshot = snapshot
        self.span = span or _active_span()
        self.variant_index = 0
        self.last_attempt_number = 0
        if self.span is None:
            return
        try:
            state = _outbound_state(self.span)
            state["active_recorder"] = self
            manifest = _snapshot_manifest(snapshot)
            fingerprint = _request_fingerprint(snapshot)
            known_variants = state["variants"]
            if fingerprint in known_variants:
                self.variant_index = known_variants[fingerprint]
                return
            self.variant_index = len(known_variants) + 1
            known_variants[fingerprint] = self.variant_index
            self.span.set_attributes(
                api_family=snapshot.api_family,
                sdk_operation=snapshot.sdk_operation,
                http_method=snapshot.http_method,
                base_url=sanitize_base_url(snapshot.base_url),
                resource_path=sanitize_resource_path(snapshot.resource_path),
                route_resolution=snapshot.route_resolution,
                streaming=snapshot.streaming,
                timeout_seconds=snapshot.timeout_seconds,
                proxy_configured=snapshot.proxy_configured,
                request_variant_count=self.variant_index,
                attempt_count=state["attempt_count"],
                retry_count=state["retry_count"],
                recovery_count=state["recovery_count"],
                transport_metadata_available=state["transport_metadata_available"],
                parameter_transformation_count=len(snapshot.transformations),
                ignored_parameter_count=len(snapshot.ignored_fields),
            )
            controls = manifest["control_parameters"]
            self.span.set_attributes(
                **_summary_attributes(controls, manifest["input_summary"])
            )
            self.span.add_event(
                "outbound.request.prepared",
                variant_index=self.variant_index,
                transformation_count=len(snapshot.transformations),
                ignored_field_count=len(snapshot.ignored_fields),
                unknown_parameter_count=len(manifest["unknown_parameters"]),
                redacted_field_count=len(manifest["redacted_fields"]),
            )
            self.span.record_json(
                "outbound.effective_request",
                manifest,
                metadata={
                    "schema_version": 1,
                    "variant_index": self.variant_index,
                    "sanitized": True,
                    "transformation_count": len(snapshot.transformations),
                    "ignored_field_count": len(snapshot.ignored_fields),
                    "redacted_field_count": len(manifest["redacted_fields"]),
                },
            )
        except Exception:
            self.span = None

    def record_attempt(self) -> int:
        """Record a real SDK/HTTP attempt and return its global attempt number.

        Returns:
            One-based attempt number, or zero when Trace is unavailable.
        """

        if self.span is None:
            return 0
        try:
            state = _outbound_state(self.span)
            state["attempt_count"] += 1
            attempt_number = state["attempt_count"]
            self.last_attempt_number = attempt_number
            started_at = time.monotonic()
            state["attempt_started"][attempt_number] = started_at
            state["active_recorder"] = self
            state["active_attempt_number"] = attempt_number
            _active_outbound_attempt.set((self, attempt_number))
            if state["first_request_started"] is None:
                state["first_request_started"] = started_at
            self.span.set_attributes(attempt_count=attempt_number)
            self.span.add_event(
                "outbound.request.attempt",
                attempt_number=attempt_number,
                variant_index=self.variant_index,
            )
            return attempt_number
        except Exception:
            return 0

    def record_retry(
        self,
        error: BaseException,
        *,
        attempt_number: int,
        next_attempt_number: int | None,
        backoff_seconds: float | None,
    ) -> None:
        """Record a retry decision made by the request policy.

        Args:
            error: Failure that caused the retry.
            attempt_number: Failed attempt number.
            next_attempt_number: Attempt scheduled after the backoff.
            backoff_seconds: Planned delay before the next attempt.
        """

        if self.span is None:
            return
        try:
            state = _outbound_state(self.span)
            if attempt_number in state["closed_attempts"]:
                return
            state["closed_attempts"].add(attempt_number)
            state["retry_count"] += 1
            status_code = extract_status_code(error)
            started = state["attempt_started"].pop(attempt_number, None)
            if state.get("active_attempt_number") == attempt_number:
                state["active_attempt_number"] = None
            _clear_active_attempt(self, attempt_number)
            duration_ms = (
                round((time.monotonic() - started) * 1000, 3)
                if started is not None
                else None
            )
            self.span.set_attributes(retry_count=state["retry_count"])
            self.span.add_event(
                "outbound.request.retry",
                attempt_number=attempt_number,
                next_attempt_number=next_attempt_number,
                backoff_seconds=backoff_seconds,
                duration_ms=duration_ms,
                exception_type=type(error).__name__,
                error_category=_error_category(error, status_code),
                status_code=status_code,
                retryable=True,
                variant_index=self.variant_index,
            )
        except Exception:
            return

    def record_retry_response(
        self,
        *,
        attempt_number: int,
        next_attempt_number: int,
        status_code: int,
        backoff_seconds: float | None = None,
    ) -> None:
        """Record a retry selected from an explicit HTTP response status.

        Args:
            attempt_number: Completed attempt that triggered the retry.
            next_attempt_number: Attempt scheduled next.
            status_code: Explicit response status observed by Core.
            backoff_seconds: Planned delay, if any.
        """

        if self.span is None:
            return
        try:
            state = _outbound_state(self.span)
            if attempt_number in state["closed_attempts"]:
                return
            state["closed_attempts"].add(attempt_number)
            state["retry_count"] += 1
            state["transport_metadata_available"] = True
            started = state["attempt_started"].pop(attempt_number, None)
            if state.get("active_attempt_number") == attempt_number:
                state["active_attempt_number"] = None
            _clear_active_attempt(self, attempt_number)
            duration_ms = (
                round((time.monotonic() - started) * 1000, 3)
                if started is not None
                else None
            )
            self.span.set_attributes(
                retry_count=state["retry_count"],
                transport_metadata_available=True,
            )
            self.span.add_event(
                "outbound.request.retry",
                attempt_number=attempt_number,
                next_attempt_number=next_attempt_number,
                backoff_seconds=backoff_seconds,
                duration_ms=duration_ms,
                error_category=_error_category(Exception(), status_code),
                status_code=status_code,
                retryable=True,
                variant_index=self.variant_index,
            )
        except Exception:
            return

    def record_completed(self, response: Any, *, attempt_number: int) -> None:
        """Record successful completion of an SDK/HTTP dispatch.

        Args:
            response: SDK or direct HTTP response object.
            attempt_number: Successful attempt number.
        """

        if self.span is None:
            return
        try:
            status_code, request_id = extract_response_metadata(response)
            state = _outbound_state(self.span)
            if attempt_number in state["closed_attempts"]:
                return
            state["closed_attempts"].add(attempt_number)
            started = state["attempt_started"].pop(attempt_number, None)
            if state.get("active_attempt_number") == attempt_number:
                state["active_attempt_number"] = None
                state["active_recorder"] = None
            _clear_active_attempt(self, attempt_number)
            duration_ms = (
                round((time.monotonic() - started) * 1000, 3)
                if started is not None
                else None
            )
            metadata_available = status_code is not None or request_id is not None
            if metadata_available:
                state["transport_metadata_available"] = True
            attributes: dict[str, Any] = {
                "transport_metadata_available": state["transport_metadata_available"],
            }
            if status_code is not None:
                attributes["status_code"] = status_code
            if request_id is not None:
                attributes["remote_request_id"] = request_id
            self.span.set_attributes(**attributes)
            self.span.add_event(
                "outbound.request.completed",
                attempt_number=attempt_number,
                variant_index=self.variant_index,
                duration_ms=duration_ms,
                status_code=status_code,
                remote_request_id=request_id,
                transport_metadata_available=metadata_available,
            )
        except Exception:
            return

    def record_failed(
        self,
        error: BaseException,
        *,
        attempt_number: int,
        terminal: bool = True,
        status_code: int | None = None,
    ) -> None:
        """Record one request failure without storing exception text.

        Args:
            error: Terminal exception.
            attempt_number: Last attempted request number.
            terminal: Whether the retry policy will stop after this failure.
        """

        if self.span is None:
            return
        try:
            status_code = status_code or extract_status_code(error)
            state = _outbound_state(self.span)
            if attempt_number in state["closed_attempts"]:
                return
            state["closed_attempts"].add(attempt_number)
            started = state["attempt_started"].pop(attempt_number, None)
            if state.get("active_attempt_number") == attempt_number:
                state["active_attempt_number"] = None
                state["active_recorder"] = None
            _clear_active_attempt(self, attempt_number)
            duration_ms = (
                round((time.monotonic() - started) * 1000, 3)
                if started is not None
                else None
            )
            if status_code is not None:
                state["transport_metadata_available"] = True
                self.span.set_attributes(
                    status_code=status_code,
                    transport_metadata_available=True,
                )
            self.span.add_event(
                "outbound.request.failed",
                attempt_number=attempt_number,
                variant_index=self.variant_index,
                duration_ms=duration_ms,
                exception_type=type(error).__name__,
                error_category=_error_category(error, status_code),
                status_code=status_code,
                terminal=terminal,
            )
        except Exception:
            return


def sanitize_base_url(value: str | None) -> str | None:
    """Remove credentials, query, and fragment from an endpoint URL.

    Args:
        value: Configured endpoint URL.

    Returns:
        Sanitized base URL, or ``None`` when unavailable.
    """

    if not value:
        return None
    try:
        parsed = urlsplit(str(value))
        if not parsed.scheme or not parsed.hostname:
            return None
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        path = parsed.path.rstrip("/")
        return urlunsplit((parsed.scheme, host, path, "", ""))
    except (TypeError, ValueError):
        return None


def split_configured_endpoint(
    value: Any,
    *,
    dynamic_path_template: str,
    static_paths: tuple[str, ...] = (),
) -> tuple[str | None, str | None]:
    """Return a sanitized endpoint origin and non-identifying resource path.

    Configured endpoints often include their route rather than a pure base URL.
    Unknown paths may contain tenant/session identifiers, so only explicitly
    allowlisted static paths are kept; other paths use the supplied template.

    Args:
        value: Configured absolute HTTP(S) URL.
        dynamic_path_template: Placeholder for unknown or dynamic paths.
        static_paths: Exact path values that are safe to expose.

    Returns:
        Sanitized origin and safe path, or ``(None, None)`` when unavailable.
    """

    try:
        parsed = urlsplit(str(value))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None, None
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        origin = urlunsplit((parsed.scheme, host, "", "", ""))
        path = parsed.path.rstrip("/") or "/"
        return origin, path if path in static_paths else dynamic_path_template
    except (TypeError, ValueError):
        return None, None


def sanitize_resource_path(value: Any) -> str | None:
    """Keep a route path while stripping accidental query/fragment values."""

    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = urlsplit(value)
        path = parsed.path or value.split("?", 1)[0].split("#", 1)[0]
        return path or None
    except (TypeError, ValueError):
        return None


def stable_identifier_hash(value: Any) -> str | None:
    """Return a short stable hash for a dynamic remote identifier.

    Args:
        value: Identifier to hash.

    Returns:
        A prefixed SHA-256 fragment, or ``None`` for empty values.
    """

    if value is None or value == "":
        return None
    return "sha256:" + hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


def extract_status_code(error: BaseException) -> int | None:
    """Extract a numeric status code without parsing exception text.

    Args:
        error: SDK or HTTP exception.

    Returns:
        Status code when exposed by the exception.
    """

    for attr in ("status_code", "status"):
        value = getattr(error, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(error, "response", None)
    if response is not None:
        for attr in ("status_code", "status"):
            value = getattr(response, attr, None)
            if isinstance(value, int):
                return value
    return None


def extract_response_metadata(response: Any) -> tuple[int | None, str | None]:
    """Extract explicitly exposed status and request identifiers.

    Args:
        response: SDK or direct HTTP response object.

    Returns:
        Tuple of status code and request ID. Missing values remain ``None``.
    """

    status_code = None
    for attr in ("status_code", "status"):
        value = getattr(response, attr, None)
        if isinstance(value, int):
            status_code = value
            break
    request_id = None
    for attr in ("_request_id", "request_id", "requestId"):
        value = getattr(response, attr, None)
        if isinstance(value, str) and value:
            request_id = value
            break
    headers = getattr(response, "headers", None)
    if request_id is None and headers is not None and hasattr(headers, "get"):
        for key in ("x-request-id", "request-id", "x-amzn-requestid"):
            value = headers.get(key)
            if isinstance(value, str) and value:
                request_id = value
                break
    return status_code, request_id


def record_outbound_first_chunk(
    response: Any = None,
    *,
    span: TraceSpan | None = None,
) -> None:
    """Record the first Core-consumable streaming item on the active span.

    Args:
        response: Optional SDK/Core response carrying request metadata.
        span: Explicit logical call span when the caller has already left its context.
    """

    span = span or _active_span()
    if span is None:
        return
    try:
        state = _outbound_state(span)
        state["response_chunk_count"] += 1
        if not state["first_chunk_recorded"]:
            state["first_chunk_recorded"] = True
            first_request_started = state["first_request_started"]
            elapsed_ms = (
                round((time.monotonic() - first_request_started) * 1000, 3)
                if isinstance(first_request_started, (int, float))
                else None
            )
            span.set_attributes(time_to_first_chunk_ms=elapsed_ms)
            span.add_event(
                "outbound.response.first_chunk",
                time_to_first_chunk_ms=elapsed_ms,
            )
        status_code, request_id = extract_response_metadata(response)
        attributes: dict[str, Any] = {
            "response_chunk_count": state["response_chunk_count"]
        }
        if status_code is not None:
            attributes["status_code"] = status_code
            attributes["transport_metadata_available"] = True
        if request_id is not None:
            attributes["remote_request_id"] = request_id
            attributes["transport_metadata_available"] = True
        span.set_attributes(**attributes)
    except Exception:
        return


def record_outbound_recovery(
    action: str,
    *,
    span: TraceSpan | None = None,
    attempt_number: int | None = None,
    close_attempt: bool = True,
    **attributes: Any,
) -> None:
    """Record a semantic recovery without storing sensitive values.

    Args:
        action: Stable recovery action name.
        close_attempt: Whether the recovery terminates the current transport
            attempt. Reconnect-before-retry flows leave it open so the retry
            policy can still record the attempt's terminal retry event.
        **attributes: Safe scalar facts describing the recovery.
    """

    span = span or _active_span()
    if span is None:
        return
    try:
        state = _outbound_state(span)
        state["recovery_count"] += 1
        if attempt_number is None:
            active_attempt_number = state.get("active_attempt_number")
            if isinstance(active_attempt_number, int) and active_attempt_number > 0:
                attempt_number = active_attempt_number
        started = (
            state["attempt_started"].get(attempt_number)
            if attempt_number is not None
            else None
        )
        if close_attempt:
            if attempt_number is not None:
                state["attempt_started"].pop(attempt_number, None)
            if state.get("active_attempt_number") == attempt_number:
                state["active_attempt_number"] = None
                state["active_recorder"] = None
            if isinstance(attempt_number, int) and attempt_number > 0:
                state["closed_attempts"].add(attempt_number)
                active_recorder = _active_outbound_attempt.get()
                if active_recorder is not None:
                    _clear_active_attempt(active_recorder[0], attempt_number)
        duration_ms = (
            round((time.monotonic() - started) * 1000, 3)
            if started is not None
            else None
        )
        safe_attributes = {
            key: value
            for key, value in attributes.items()
            if not _is_sensitive_key(key)
            and (value is None or isinstance(value, (bool, int, float, str)))
        }
        span.set_attributes(recovery_count=state["recovery_count"])
        span.add_event(
            "outbound.request.recovered",
            action=action,
            recovery_number=state["recovery_count"],
            attempt_number=attempt_number,
            duration_ms=duration_ms,
            **safe_attributes,
        )
    except Exception:
        return


def record_active_outbound_failure(
    error: BaseException,
    *,
    span: TraceSpan | None = None,
) -> None:
    """Close an unhandled active attempt at a surrounding Core boundary.

    Direct HTTP adapters cannot always wrap every failure raised while entering
    an async response context. The surrounding Provider or Tool boundary calls
    this helper as a final safety net. A recorder that already completed or
    failed its attempt is ignored, so the fallback never creates duplicate
    terminal events.

    Args:
        error: Original business exception, re-raised unchanged by the caller.
        span: Explicit logical span, or the active span when omitted.
    """

    span = span or _active_span()
    if span is None:
        return
    try:
        recorder = None
        attempt_number = None
        task_attempt = _active_outbound_attempt.get()
        if task_attempt is not None and task_attempt[0].span is span:
            recorder, attempt_number = task_attempt
        if recorder is None:
            state = _outbound_state(span)
            recorder = state.get("active_recorder")
            attempt_number = state.get("active_attempt_number")
        if not isinstance(recorder, OutboundCallRecorder):
            return
        if not isinstance(attempt_number, int) or attempt_number <= 0:
            return
        recorder.record_failed(error, attempt_number=attempt_number)
    except Exception:
        return


def record_active_outbound_retry(
    error: BaseException,
    *,
    next_attempt_number: int | None,
    backoff_seconds: float | None,
    span: TraceSpan | None = None,
) -> None:
    """Close the active attempt as a retry from a surrounding Core loop.

    Business-level batch coordinators own the retry decision while the nested
    Provider method owns the actual request recorder. Missing or already closed
    attempts are ignored so this helper remains fail-open and idempotent.

    Args:
        error: Original request error.
        next_attempt_number: Global attempt number expected after backoff.
        backoff_seconds: Planned retry delay.
        span: Explicit logical call span, or the active span when omitted.
    """

    span = span or _active_span()
    if span is None:
        return
    try:
        recorder = None
        attempt_number = None
        task_attempt = _active_outbound_attempt.get()
        if task_attempt is not None and task_attempt[0].span is span:
            recorder, attempt_number = task_attempt
        if recorder is None:
            state = _outbound_state(span)
            recorder = state.get("active_recorder")
            attempt_number = state.get("active_attempt_number")
        if not isinstance(recorder, OutboundCallRecorder):
            return
        if not isinstance(attempt_number, int) or attempt_number <= 0:
            return
        recorder.record_retry(
            error,
            attempt_number=attempt_number,
            next_attempt_number=next_attempt_number,
            backoff_seconds=backoff_seconds,
        )
    except Exception:
        return


def record_outbound_response_summary(
    *,
    finish_reason: Any = None,
    usage: Any = None,
    response_id: Any = None,
    span: TraceSpan | None = None,
) -> None:
    """Attach stable terminal model response facts to the active span.

    Args:
        finish_reason: Provider finish/stop reason.
        usage: Core or SDK token usage object.
        response_id: Semantic provider response identifier.
        span: Explicit logical call span when the caller has already left its context.
    """

    span = span or _active_span()
    if span is None:
        return
    try:
        attributes: dict[str, Any] = {}
        if finish_reason is not None:
            attributes["finish_reason"] = str(finish_reason)
        if response_id:
            attributes["response_id_hash"] = stable_identifier_hash(response_id)
        if usage is not None:
            for source, target in (
                ("input", "usage_input_tokens"),
                ("input_other", "usage_input_other_tokens"),
                ("input_cached", "usage_input_cached_tokens"),
                ("output", "usage_output_tokens"),
                ("total", "usage_total_tokens"),
                ("input_tokens", "usage_input_tokens"),
                ("output_tokens", "usage_output_tokens"),
                ("cache_read_input_tokens", "usage_input_cached_tokens"),
                ("prompt_tokens", "usage_input_tokens"),
                ("completion_tokens", "usage_output_tokens"),
                ("total_tokens", "usage_total_tokens"),
                ("prompt_token_count", "usage_input_tokens"),
                ("candidates_token_count", "usage_output_tokens"),
                ("total_token_count", "usage_total_tokens"),
                ("prompt_eval_count", "usage_input_tokens"),
            ):
                value = (
                    usage.get(source)
                    if isinstance(usage, Mapping)
                    else getattr(usage, source, None)
                )
                if isinstance(value, int):
                    attributes[target] = value
        span.set_attributes(**attributes)
    except Exception:
        return


def record_outbound_result_attributes(
    *,
    span: TraceSpan | None = None,
    **attributes: Any,
) -> None:
    """Attach content-free result facts to an outbound logical span.

    Args:
        span: Explicit logical call span, or the active span when omitted.
        **attributes: Safe scalar or short scalar-list result metadata.
    """

    span = span or _active_span()
    if span is None:
        return
    try:
        safe_attributes = {
            key: value
            for key, value in attributes.items()
            if not _is_sensitive_key(key)
            and (
                value is None
                or isinstance(value, (bool, int, float, str))
                or (
                    isinstance(value, (list, tuple))
                    and all(
                        item is None or isinstance(item, (bool, int, float, str))
                        for item in value
                    )
                )
            )
        }
        span.set_attributes(**safe_attributes)
    except Exception:
        return


def has_outbound_observation(span: TraceSpan | None = None) -> bool:
    """Return whether an outbound recorder has observed the logical span.

    Args:
        span: Explicit span or the active span when omitted.

    Returns:
        ``True`` when at least one request snapshot exists.
    """

    span = span or _active_span()
    if span is None:
        return False
    try:
        state = getattr(span._span_state, "_astrbot_outbound_state", None)
        return bool(state and state.get("variants"))
    except Exception:
        return False


def find_trace_span(operation: str) -> TraceSpan | None:
    """Return the active span or its nearest ancestor for an operation.

    This lets external Agent HTTP clients attach their single logical outbound
    boundary to ``agent.run`` even while ``agent.step`` is temporarily active.

    Args:
        operation: Core operation name to locate.

    Returns:
        A running span handle, or ``None`` when no matching ancestor is active.
    """

    span = _active_span()
    if span is None:
        return None
    try:
        state = span._span_state
        while state is not None:
            if state.operation == operation and not state.terminal:
                return TraceSpan(span._service, span._trace_state, state)
            parent_span_id = state.parent_span_id
            state = (
                span._trace_state.spans.get(parent_span_id)
                if parent_span_id is not None
                else None
            )
    except Exception:
        return None
    return None


def _active_span() -> TraceSpan | None:
    service = current_trace_service()
    if service is None:
        return None
    span = service.current_span()
    return span if isinstance(span, TraceSpan) else None


def _clear_active_attempt(
    recorder: OutboundCallRecorder,
    attempt_number: int,
) -> None:
    """Clear the task-local attempt pointer when it still names this attempt."""

    try:
        active = _active_outbound_attempt.get()
        if active is not None and active == (recorder, attempt_number):
            _active_outbound_attempt.set(None)
    except Exception:
        return


def _outbound_state(span: TraceSpan) -> dict[str, Any]:
    state = getattr(span._span_state, "_astrbot_outbound_state", None)
    if state is None:
        state = {
            "variants": {},
            "attempt_count": 0,
            "retry_count": 0,
            "recovery_count": 0,
            "attempt_started": {},
            "first_request_started": None,
            "first_chunk_recorded": False,
            "response_chunk_count": 0,
            "transport_metadata_available": False,
            "active_recorder": None,
            "active_attempt_number": None,
            "closed_attempts": set(),
        }
        setattr(span._span_state, "_astrbot_outbound_state", state)
    return state


def _snapshot_manifest(snapshot: OutboundRequestSnapshot) -> dict[str, Any]:
    controls: dict[str, Any] = {}
    input_summary: dict[str, Any] = {}
    unknown: list[dict[str, str]] = []
    redacted: list[str] = []
    for key, value in snapshot.input_summary.items():
        key_text = str(key)
        if _is_sensitive_key(key_text):
            redacted.append(f"input_summary.{key_text}")
        elif _is_safe_input_summary_key(key_text):
            input_summary[key_text] = _safe_input_summary_value(key_text, value)
        else:
            unknown.append(
                {
                    "path": f"input_summary.{key_text}",
                    "type": type(value).__name__,
                }
            )
    for key, value in snapshot.parameters.items():
        key_text = str(key)
        key_lower = key_text.lower()
        if _is_sensitive_key(key_lower):
            redacted.append(key_text)
        elif key_lower in _CONTENT_FIELDS:
            input_summary[key_text] = _content_summary(value)
        elif key_lower == "tools":
            input_summary["tools"] = _tool_summary(value)
        elif key_lower == "response_format":
            controls["response_format"] = _response_format_summary(value)
        elif key_lower == "tool_choice":
            controls["tool_choice"] = _tool_choice_summary(value)
        elif key_lower in {"stop_sequences", "stop"}:
            controls["stop_count"] = (
                len(value)
                if isinstance(value, (list, tuple))
                else int(value is not None)
            )
        elif key_lower in _SAFE_SCALAR_FIELDS:
            controls[key_text] = _safe_scalar(value)
        elif key_lower == "output_config":
            controls[key_text] = _output_config_summary(value)
        elif key_lower == "safety_settings":
            controls[key_text] = _safety_settings_summary(value)
        elif key_lower in _SAFE_STRUCTURED_FIELDS:
            controls[key_text] = _control_structure_summary(
                value,
                key_lower,
                redacted,
            )
        elif key_lower in {"extra_body", "generation_config"} and isinstance(
            value, Mapping
        ):
            nested = OutboundRequestSnapshot(
                api_family=snapshot.api_family,
                sdk_operation=snapshot.sdk_operation,
                parameters=value,
            )
            nested_manifest = _snapshot_manifest(nested)
            controls.update(nested_manifest["control_parameters"])
            input_summary.update(nested_manifest["input_summary"])
            unknown.extend(
                {
                    "path": f"{key_text}.{item['path']}",
                    "type": item["type"],
                }
                for item in nested_manifest["unknown_parameters"]
            )
            redacted.extend(
                f"{key_text}.{item}" for item in nested_manifest["redacted_fields"]
            )
        else:
            unknown.append({"path": key_text, "type": type(value).__name__})
    return {
        "schema_version": 1,
        "route": {
            "api_family": snapshot.api_family,
            "sdk_operation": snapshot.sdk_operation,
            "http_method": snapshot.http_method,
            "base_url": sanitize_base_url(snapshot.base_url),
            "resource_path": sanitize_resource_path(snapshot.resource_path),
            "route_resolution": snapshot.route_resolution,
            "streaming": snapshot.streaming,
            "timeout_seconds": snapshot.timeout_seconds,
            "proxy_configured": snapshot.proxy_configured,
        },
        "control_parameters": controls,
        "input_summary": input_summary,
        "transformations": list(snapshot.transformations),
        "ignored_fields": list(snapshot.ignored_fields),
        "unknown_parameters": unknown,
        "redacted_fields": sorted(set(redacted)),
    }


def _request_fingerprint(snapshot: OutboundRequestSnapshot) -> str:
    """Hash the actual request variant without persisting its content.

    The effective-request Artifact deliberately stores only summaries. Its JSON
    therefore cannot distinguish equal-length prompts or messages. This private
    digest includes known container values so semantic recovery variants remain
    distinct while no request body is written to Trace storage.
    """

    digest = hashlib.sha256()

    def _update(value: Any, depth: int = 0) -> None:
        if depth >= 32:
            digest.update(b"depth-limit")
            return
        if value is None:
            digest.update(b"none")
        elif isinstance(value, bool):
            digest.update(b"bool:1" if value else b"bool:0")
        elif isinstance(value, int):
            digest.update(f"int:{value}".encode())
        elif isinstance(value, float):
            digest.update(f"float:{value!r}".encode())
        elif isinstance(value, str):
            encoded = value.encode("utf-8", errors="replace")
            digest.update(f"str:{len(encoded)}:".encode())
            digest.update(encoded)
        elif isinstance(value, (bytes, bytearray, memoryview)):
            encoded = bytes(value)
            digest.update(f"bytes:{len(encoded)}:".encode())
            digest.update(encoded)
        elif isinstance(value, Mapping):
            digest.update(b"mapping{")
            for key in sorted(value, key=lambda item: str(item)):
                _update(str(key), depth + 1)
                _update(value[key], depth + 1)
            digest.update(b"}")
        elif isinstance(value, (list, tuple)):
            digest.update(f"sequence:{len(value)}[".encode())
            for item in value:
                _update(item, depth + 1)
            digest.update(b"]")
        else:
            mapping = _as_mapping(value)
            if mapping is None:
                digest.update(f"opaque:{type(value).__name__}".encode())
            else:
                _update(mapping, depth + 1)

    _update(
        {
            "api_family": snapshot.api_family,
            "sdk_operation": snapshot.sdk_operation,
            "http_method": snapshot.http_method,
            "base_url": sanitize_base_url(snapshot.base_url),
            "resource_path": sanitize_resource_path(snapshot.resource_path),
            "streaming": snapshot.streaming,
            "timeout_seconds": snapshot.timeout_seconds,
            "proxy_configured": snapshot.proxy_configured,
            "parameters": snapshot.parameters,
            "transformations": snapshot.transformations,
            "ignored_fields": snapshot.ignored_fields,
        }
    )
    return digest.hexdigest()


def _summary_attributes(
    controls: Mapping[str, Any],
    input_summary: Mapping[str, Any],
) -> dict[str, Any]:
    attributes: dict[str, Any] = {}
    for key in (
        "model",
        "batch_size",
        "temperature",
        "top_p",
        "top_k",
        "seed",
        "presence_penalty",
        "frequency_penalty",
        "parallel_tool_calls",
        "store",
        "stop_count",
        "language",
        "voice",
        "speed",
        "sample_rate",
        "dimensions",
        "dimension",
        "encoding_format",
        "input_type",
        "output_dimensionality",
        "output_format",
        "top_n",
        "return_documents",
        "reasoning_effort",
        "service_tier",
        "truncate",
        "truncation_direction",
        "raw_scores",
        "return_text",
        "response_mime_type",
        "audio_format",
        "volume",
    ):
        value = controls.get(key)
        if value is None or isinstance(value, (bool, int, float, str)):
            if key in controls:
                attributes[key] = value
    for key in ("max_output_tokens", "max_completion_tokens", "max_tokens"):
        value = controls.get(key)
        if isinstance(value, int):
            attributes["token_limit_field"] = key
            attributes["token_limit_value"] = value
            break
    reasoning = controls.get("reasoning")
    if isinstance(reasoning, Mapping) and reasoning.get("effort") is not None:
        attributes["reasoning_effort"] = reasoning["effort"]
    thinking = controls.get("thinking")
    if isinstance(thinking, Mapping):
        if thinking.get("type") is not None:
            attributes["thinking_type"] = thinking["type"]
        if thinking.get("budget_tokens") is not None:
            attributes["thinking_budget_tokens"] = thinking["budget_tokens"]
    thinking_config = controls.get("thinking_config")
    if isinstance(thinking_config, Mapping):
        for source, target in (
            ("thinking_budget", "thinking_budget"),
            ("thinking_level", "thinking_level"),
            ("include_thoughts", "include_thoughts"),
        ):
            if thinking_config.get(source) is not None:
                attributes[target] = thinking_config[source]
    output_config = controls.get("output_config")
    if isinstance(output_config, Mapping) and output_config.get("effort") is not None:
        attributes["reasoning_effort"] = output_config["effort"]
    modalities = controls.get("modalities")
    if modalities is None:
        modalities = controls.get("response_modalities")
    if isinstance(modalities, list):
        attributes["modalities"] = modalities
    response_format = controls.get("response_format")
    if isinstance(response_format, Mapping):
        attributes["response_format"] = response_format.get("type")
    elif isinstance(response_format, str):
        attributes["response_format"] = response_format
    tool_choice = controls.get("tool_choice")
    if isinstance(tool_choice, Mapping):
        attributes["tool_choice"] = tool_choice.get("type")
        if tool_choice.get("name") is not None:
            attributes["tool_choice_name"] = tool_choice["name"]
    elif isinstance(tool_choice, (bool, int, float, str)):
        attributes["tool_choice"] = tool_choice
    tools = input_summary.get("tools")
    if isinstance(tools, Mapping):
        attributes["tool_count"] = tools.get("count")
    for key, value in input_summary.items():
        if key in attributes or _is_sensitive_key(key):
            continue
        if value is None or isinstance(value, (bool, int, float, str)):
            attributes[key] = value
    return attributes


def _response_format_summary(value: Any) -> Any:
    """Keep output-format diagnostics without copying an arbitrary JSON schema."""

    if isinstance(value, str):
        return value
    if not isinstance(value, Mapping):
        return {"type": type(value).__name__}
    format_type = value.get("type")
    result: dict[str, Any] = {
        "type": format_type if isinstance(format_type, str) else "object"
    }
    schema = value.get("json_schema")
    if isinstance(schema, Mapping):
        name = schema.get("name")
        if isinstance(name, str):
            result["name"] = name
        if isinstance(schema.get("strict"), bool):
            result["strict"] = schema["strict"]
    return result


def _tool_choice_summary(value: Any) -> Any:
    """Keep a selected tool name without persisting tool-call arguments."""

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if not isinstance(value, Mapping):
        return {"type": type(value).__name__}
    choice_type = value.get("type")
    result: dict[str, Any] = {
        "type": choice_type if isinstance(choice_type, str) else "object"
    }
    function = value.get("function")
    name = function.get("name") if isinstance(function, Mapping) else value.get("name")
    if isinstance(name, str):
        result["name"] = name
    return result


def _output_config_summary(value: Any) -> dict[str, Any]:
    """Keep output controls without copying a caller-provided JSON schema."""

    mapping = _as_mapping(value)
    if mapping is None:
        return {"type": type(value).__name__}
    result: dict[str, Any] = {}
    effort = mapping.get("effort")
    if isinstance(effort, (bool, int, float, str)):
        result["effort"] = effort
    format_value = _as_mapping(mapping.get("format"))
    if format_value is not None:
        format_summary: dict[str, Any] = {}
        for key in ("type", "name", "strict"):
            item = format_value.get(key)
            if item is None or isinstance(item, (bool, int, float, str)):
                if key in format_value:
                    format_summary[key] = item
        schema = format_value.get("schema") or format_value.get("json_schema")
        schema_mapping = _as_mapping(schema)
        if schema_mapping is not None:
            format_summary["schema_field_count"] = len(schema_mapping)
        result["format"] = format_summary
    return result


def _control_structure_summary(
    value: Any,
    key: str,
    redacted: list[str],
) -> dict[str, Any] | list[Any] | Any:
    """Summarize only documented safe controls for structured parameters."""

    mapping = _as_mapping(value)
    if mapping is None:
        if isinstance(value, (list, tuple)):
            return [_control_structure_summary(item, key, redacted) for item in value]
        return _safe_scalar(value)
    safe_children: dict[str, frozenset[str]] = {
        "reasoning": frozenset({"effort", "summary"}),
        "thinking": frozenset({"type", "budget_tokens"}),
        "thinking_config": frozenset(
            {"thinking_budget", "thinking_level", "include_thoughts"}
        ),
        "modalities": frozenset(),
        "response_modalities": frozenset(),
    }
    allowed = safe_children.get(key, frozenset())
    result: dict[str, Any] = {}
    for child_key, child_value in mapping.items():
        child_text = str(child_key)
        if _is_sensitive_key(child_text):
            redacted.append(f"{key}.{child_text}")
        elif child_text in allowed and (
            child_value is None or isinstance(child_value, (bool, int, float, str))
        ):
            result[child_text] = child_value
    return result


def _safety_settings_summary(value: Any) -> dict[str, Any]:
    """Summarize Gemini safety controls without preserving arbitrary values."""

    items = value if isinstance(value, (list, tuple)) else [value]
    categories: list[str] = []
    thresholds: list[str] = []
    for item in items:
        mapping = _as_mapping(item)
        if mapping is None:
            continue
        category = mapping.get("category")
        threshold = mapping.get("threshold")
        if isinstance(category, (str, int)):
            categories.append(str(category))
        if isinstance(threshold, (str, int)):
            thresholds.append(str(threshold))
    return {
        "count": len(items) if value is not None else 0,
        "categories": categories,
        "thresholds": thresholds,
    }


def _content_summary(value: Any) -> dict[str, Any]:
    if value is None:
        return {"type": "none", "count": 0}
    if isinstance(value, str):
        return {
            "type": "text",
            "chars": len(value),
            "bytes": len(value.encode("utf-8", errors="replace")),
        }
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"type": "binary", "bytes": len(value)}
    if isinstance(value, Mapping):
        roles = Counter()
        content_types = Counter()
        total_chars = 0
        for item in value.values():
            if isinstance(item, str):
                total_chars += len(item)
        return {
            "type": "object",
            "field_count": len(value),
            "roles": dict(roles),
            "content_types": dict(content_types),
            "chars": total_chars,
        }
    if isinstance(value, (list, tuple)):
        roles: Counter[str] = Counter()
        content_types: Counter[str] = Counter()
        total_chars = 0
        for item in value:
            if isinstance(item, str):
                total_chars += len(item)
                content_types["text"] += 1
                continue
            item_mapping = _as_mapping(item)
            if item_mapping is None:
                content_types[type(item).__name__] += 1
                continue
            item = item_mapping
            role = item.get("role")
            if isinstance(role, str):
                roles[role] += 1
            item_type = item.get("type")
            if isinstance(item_type, str):
                content_types[item_type] += 1
            content = item.get("content")
            if isinstance(content, str):
                total_chars += len(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, Mapping):
                        part_type = part.get("type")
                        if isinstance(part_type, str):
                            content_types[part_type] += 1
                        text = part.get("text")
                        if isinstance(text, str):
                            total_chars += len(text)
            parts = item.get("parts")
            if isinstance(parts, list):
                for part in parts:
                    part_mapping = _as_mapping(part)
                    if part_mapping is None:
                        content_types[type(part).__name__] += 1
                        continue
                    part_type = part_mapping.get("type")
                    if isinstance(part_type, str):
                        content_types[part_type] += 1
                    elif "text" in part_mapping:
                        content_types["text"] += 1
                    elif "inline_data" in part_mapping:
                        content_types["inline_data"] += 1
                    text = part_mapping.get("text")
                    if isinstance(text, str):
                        total_chars += len(text)
        result: dict[str, Any] = {
            "type": "list",
            "count": len(value),
            "chars": total_chars,
        }
        if roles:
            result["roles"] = dict(roles)
        if content_types:
            result["content_types"] = dict(content_types)
        return result
    return {"type": type(value).__name__}


def _as_mapping(value: Any) -> Mapping[str, Any] | None:
    """Convert a known SDK model to a mapping for in-memory summarization."""

    if isinstance(value, Mapping):
        return value
    model_dump = getattr(value, "model_dump", None)
    if not callable(model_dump):
        return None
    try:
        dumped = model_dump(exclude_none=True)
    except TypeError:
        try:
            dumped = model_dump()
        except Exception:
            return None
    except Exception:
        return None
    return dumped if isinstance(dumped, Mapping) else None


def _is_safe_input_summary_key(key: str) -> bool:
    lowered = key.lower().replace("-", "_")
    return lowered in _SAFE_INPUT_SUMMARY_FIELDS


def _safe_input_summary_value(key: str, value: Any) -> Any:
    """Validate adapter summaries by semantic field, not just Python type."""

    lowered = key.lower().replace("-", "_")
    if value is None:
        return value
    if lowered.endswith("_hash"):
        return (
            value
            if isinstance(value, str) and value.startswith("sha256:")
            else {"type": type(value).__name__}
        )
    if lowered.endswith(("_count", "_chars", "_bytes")):
        return (
            value
            if isinstance(value, int) and not isinstance(value, bool)
            else {"type": type(value).__name__}
        )
    if lowered == "audio_duration_seconds":
        return (
            value
            if isinstance(value, (int, float)) and not isinstance(value, bool)
            else {"type": type(value).__name__}
        )
    if lowered == "prompt_cache_enabled":
        return value if isinstance(value, bool) else {"type": type(value).__name__}
    if lowered in {
        "audio_source_type",
        "mcp_server_name",
        "mcp_transport",
        "search_engine",
    }:
        return value if isinstance(value, str) else {"type": type(value).__name__}
    return {"type": type(value).__name__}


def _tool_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, (list, tuple)):
        return {"type": type(value).__name__, "count": int(value is not None)}
    names: list[str] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        function = item.get("function")
        name = (
            function.get("name") if isinstance(function, Mapping) else item.get("name")
        )
        if isinstance(name, str):
            names.append(name)
    return {"type": "list", "count": len(value), "names": names}


def _safe_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [
            item
            for item in value
            if item is None or isinstance(item, (bool, int, float, str))
        ]
    if isinstance(value, Mapping):
        return {"type": "object", "field_count": len(value)}
    return {"type": type(value).__name__}


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower().replace("-", "_")
    if lowered in _SENSITIVE_NAMES:
        return True
    return lowered.endswith(("_api_key", "_password", "_secret"))


def _error_category(error: BaseException, status_code: int | None) -> str:
    if type(error).__name__ == "CancelledError":
        return "cancelled"
    if isinstance(error, GeneratorExit):
        return "consumer_closed"
    if isinstance(error, TimeoutError) or "timeout" in type(error).__name__.lower():
        return "timeout"
    if status_code == 429:
        return "rate_limit"
    if status_code is not None:
        if 400 <= status_code < 500:
            return "client_error"
        if status_code >= 500:
            return "server_error"
    if "connect" in type(error).__name__.lower():
        return "connection"
    return "exception"
