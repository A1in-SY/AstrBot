"""Lifecycle tracing for Core-created Agent Runner instances."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
from collections.abc import AsyncGenerator, Iterator
from typing import Any

from .context import TraceState, current_span_id, current_trace_state
from .outbound import (
    has_outbound_observation,
    record_active_outbound_failure,
    record_outbound_result_attributes,
    sanitize_base_url,
    stable_identifier_hash,
)
from .service import NoopTraceSpan, TraceService, TraceSpan


def _estimate_context_tokens(runner: Any, messages: Any) -> int | None:
    """Estimate one step's context size without persisting message content."""

    if not isinstance(messages, list):
        return None
    manager = getattr(runner, "request_context_manager", None)
    counter = getattr(manager, "token_counter", None)
    count_tokens = getattr(counter, "count_tokens", None)
    if not callable(count_tokens):
        return None
    try:
        return int(count_tokens(messages))
    except Exception:
        return None


def instrument_agent_runner(
    runner: Any,
    trace_service: TraceService | None,
    *,
    attributes: dict[str, Any] | None = None,
) -> None:
    """Attach non-invasive reset/step lifecycle tracing to one runner instance.

    The concrete runner remains the same object and keeps its public API.  The
    wrapper records reset's parent context but delays ``agent.run`` creation
    until the first real consumption of ``step()``.

    Args:
        runner: A Core-created BaseAgentRunner implementation.
        trace_service: The Core trace service, or ``None`` when unavailable.
        attributes: Extra attributes merged into every ``agent.run`` root span
            created by this runner, used to record the trigger origin.
    """

    if trace_service is None:
        return
    try:
        if getattr(runner, "_astrbot_trace_instrumented", False):
            return
        runner._astrbot_trace_extra_attributes = dict(attributes or {})
        original_reset = getattr(runner, "reset", None)
        original_step = getattr(runner, "step", None)
        original_step_until_done = getattr(runner, "step_until_done", None)
    except Exception:
        return
    if not inspect.iscoroutinefunction(
        original_reset
    ) or not inspect.isasyncgenfunction(original_step):
        return

    @functools.wraps(original_reset)
    async def traced_reset(*args: Any, **kwargs: Any) -> Any:
        result = await original_reset(*args, **kwargs)
        try:
            runner._astrbot_trace_service = trace_service
            runner._astrbot_trace_parent_state = current_trace_state()
            runner._astrbot_trace_parent_span_id = current_span_id()
            runner._astrbot_trace_run_span = None
            runner._astrbot_trace_run_finalized = False
            runner._astrbot_trace_step_index = 0
            runner._astrbot_trace_step_kind = "normal"
        except Exception:
            return result
        return result

    @functools.wraps(original_step)
    async def traced_step(*args: Any, **kwargs: Any) -> AsyncGenerator[Any, None]:
        service = getattr(runner, "_astrbot_trace_service", trace_service)
        run_span = _ensure_run_span(runner, service)
        if isinstance(run_span, NoopTraceSpan):
            async for response in original_step(*args, **kwargs):
                yield response
            return

        try:
            runner._astrbot_trace_step_index += 1
            step_kind = getattr(runner, "_astrbot_trace_step_kind", "normal")
            runner._astrbot_trace_step_kind = "normal"
        except Exception:
            async for response in original_step(*args, **kwargs):
                yield response
            return
        messages_before = getattr(getattr(runner, "run_context", None), "messages", [])
        context_tokens_before = _estimate_context_tokens(runner, messages_before)
        request = getattr(runner, "req", None)
        tool_set = getattr(request, "func_tool", None)
        with run_span.activate():
            step_span = service.start_span(
                "agent.step",
                kind="agent",
                attributes={
                    "step_index": runner._astrbot_trace_step_index,
                    "step_kind": step_kind,
                    "messages_before": (
                        len(messages_before) if isinstance(messages_before, list) else 0
                    ),
                    "available_tool_count": len(getattr(tool_set, "tools", []) or []),
                    "context_tokens_before": context_tokens_before,
                },
            )
        model_calls_before = getattr(runner, "_trace_model_call_index", 0)
        tool_calls_before = getattr(runner, "_astrbot_trace_tool_call_count", 0)
        yield_count = 0
        original_generator = original_step(*args, **kwargs)
        original_closed = False
        try:
            while True:
                try:
                    with _activate(run_span), _activate(step_span):
                        response = await anext(original_generator)
                except StopAsyncIteration:
                    original_closed = True
                    messages_after = getattr(
                        getattr(runner, "run_context", None), "messages", []
                    )
                    step_span.set_attributes(
                        messages_after=(
                            len(messages_after)
                            if isinstance(messages_after, list)
                            else 0
                        ),
                        model_call_count=max(
                            0,
                            getattr(runner, "_trace_model_call_index", 0)
                            - model_calls_before,
                        ),
                        tool_call_count=max(
                            0,
                            getattr(runner, "_astrbot_trace_tool_call_count", 0)
                            - tool_calls_before,
                        ),
                        yield_count=yield_count,
                        context_tokens_estimate=getattr(
                            getattr(runner, "stats", None),
                            "current_context_tokens",
                            None,
                        ),
                        context_tokens_after=_estimate_context_tokens(
                            runner,
                            messages_after,
                        ),
                        forced_final=step_kind == "forced_final",
                        termination_reason=(
                            "agent_done" if runner.done() else "step_complete"
                        ),
                    )
                    step_span.finish()
                    break
                yield_count += 1
                yield response
        except GeneratorExit as exc:
            _record_partial_outbound(run_span, exc)
            await _close_generator(original_generator, run_span, step_span)
            original_closed = True
            step_span.finish(status="cancelled", outcome="generator_closed")
            _finish_run(
                runner, run_span, status="cancelled", outcome="generator_closed"
            )
            raise
        except asyncio.CancelledError as exc:
            _record_partial_outbound(run_span, exc)
            await _close_generator(original_generator, run_span, step_span)
            original_closed = True
            step_span.finish(status="cancelled", outcome="cancelled")
            _finish_run(runner, run_span, status="cancelled", outcome="cancelled")
            raise
        except BaseException as exc:
            _record_partial_outbound(run_span, exc)
            await _close_generator(original_generator, run_span, step_span)
            original_closed = True
            step_span.set_attributes(exception_type=type(exc).__name__)
            step_span.finish(status="error", outcome="exception")
            run_span.set_attributes(exception_type=type(exc).__name__)
            _finish_run(runner, run_span, status="error", outcome="exception")
            raise
        else:
            if runner.done():
                _finish_run(runner, run_span)
        finally:
            if not original_closed:
                await _close_generator(original_generator, run_span, step_span)

    traced_step_until_done_fn: Any = None
    if inspect.isasyncgenfunction(original_step_until_done):

        @functools.wraps(original_step_until_done)
        async def traced_step_until_done(
            *args: Any,
            **kwargs: Any,
        ) -> AsyncGenerator[Any, None]:
            max_steps = kwargs.get("max_step", args[0] if args else None)
            try:
                runner._astrbot_trace_max_steps = max_steps
            except Exception:
                pass
            original_generator = original_step_until_done(*args, **kwargs)
            original_closed = False
            try:
                while True:
                    try:
                        response = await anext(original_generator)
                    except StopAsyncIteration:
                        original_closed = True
                        break
                    yield response
            except GeneratorExit:
                _finish_existing_run(
                    runner,
                    status="cancelled",
                    outcome="generator_closed",
                )
                raise
            except asyncio.CancelledError:
                _finish_existing_run(runner, status="cancelled", outcome="cancelled")
                raise
            except BaseException as exc:
                _finish_existing_run(
                    runner,
                    status="error",
                    outcome="exception",
                    exception_type=type(exc).__name__,
                )
                raise
            else:
                if runner.done():
                    _finish_existing_run(runner)
                else:
                    _finish_existing_run(
                        runner,
                        status="incomplete",
                        outcome="step_limit_reached",
                    )
            finally:
                if not original_closed:
                    await _close_generator(original_generator)

        traced_step_until_done_fn = traced_step_until_done

    try:
        runner.reset = traced_reset
        runner.step = traced_step
        if traced_step_until_done_fn is not None:
            runner.step_until_done = traced_step_until_done_fn
        runner._astrbot_trace_instrumented = True
    except Exception:
        try:
            runner.reset = original_reset
            runner.step = original_step
            if traced_step_until_done_fn is not None:
                runner.step_until_done = original_step_until_done
        except Exception:
            pass


def _ensure_run_span(runner: Any, service: TraceService) -> TraceSpan | NoopTraceSpan:
    existing = getattr(runner, "_astrbot_trace_run_span", None)
    if existing is not None:
        return existing
    parent_state: TraceState | None = getattr(
        runner,
        "_astrbot_trace_parent_state",
        None,
    )
    parent_span_id = getattr(runner, "_astrbot_trace_parent_span_id", None)
    attributes = {
        "runner": type(runner).__name__,
        "capture_scope": (
            "full_internal"
            if type(runner).__name__ == "ToolLoopAgentRunner"
            else "external_boundary"
        ),
        "streaming": bool(getattr(runner, "streaming", False)),
    }
    provider = getattr(runner, "provider", None)
    provider_config = getattr(provider, "provider_config", {})
    if isinstance(provider_config, dict):
        try:
            effective_model = (
                provider.get_model()
                if callable(getattr(provider, "get_model", None))
                else None
            )
        except Exception:
            effective_model = None
        attributes.update(
            {
                "provider_id": provider_config.get("id"),
                "provider_type": provider_config.get("type"),
                "effective_model": effective_model,
            }
        )
    run_context = getattr(runner, "run_context", None)
    messages = getattr(run_context, "messages", None)
    request = getattr(runner, "req", None)
    tool_set = getattr(request, "func_tool", None)
    attributes.update(
        {
            "initial_message_count": len(messages) if isinstance(messages, list) else 0,
            "available_tool_count": len(getattr(tool_set, "tools", []) or []),
            "tool_timeout_seconds": getattr(run_context, "tool_call_timeout", None),
            "max_steps": getattr(runner, "_astrbot_trace_max_steps", None),
            "tool_schema_mode": getattr(runner, "tool_schema_mode", None),
            "max_context_tokens": getattr(
                getattr(runner, "request_context_manager_config", None),
                "max_context_tokens",
                None,
            ),
        }
    )
    attributes.update(_external_runner_attributes(runner))
    extra_attributes = getattr(runner, "_astrbot_trace_extra_attributes", None)
    if isinstance(extra_attributes, dict) and extra_attributes:
        attributes = {**attributes, **extra_attributes}
    if parent_state is not None and not parent_state.terminal:
        run_span = service.start_span_with_parent(
            parent_state,
            parent_span_id,
            "agent.run",
            kind="agent",
            attributes=attributes,
        )
    elif service.current_span().trace_id is not None:
        run_span = service.start_span("agent.run", kind="agent", attributes=attributes)
    else:
        run_span = service.start_root("agent.run", kind="agent", attributes=attributes)
    try:
        runner._astrbot_trace_run_span = run_span
    except Exception:
        return NoopTraceSpan()
    return run_span


def _external_runner_attributes(runner: Any) -> dict[str, Any]:
    """Describe an external Agent boundary without serializing its config."""

    runner_name = type(runner).__name__
    api_base = sanitize_base_url(getattr(runner, "api_base", None))
    timeout = getattr(runner, "timeout", None)
    if runner_name == "DifyAgentRunner":
        api_mode = getattr(runner, "api_type", None)
        resource_path = "/workflows/run" if api_mode == "workflow" else "/chat-messages"
        return {
            "external_agent": "dify",
            "external_api_mode": api_mode,
            "base_url": api_base,
            "resource_path": resource_path,
            "route_resolution": "constructed",
            "timeout_seconds": timeout,
        }
    if runner_name == "CozeAgentRunner":
        return {
            "external_agent": "coze",
            "external_api_mode": "chat",
            "base_url": api_base,
            "resource_path": "/v3/chat",
            "route_resolution": "constructed",
            "timeout_seconds": timeout,
            "remote_resource_id_hash": stable_identifier_hash(
                getattr(runner, "bot_id", None)
            ),
            "auto_save_history": getattr(runner, "auto_save_history", None),
        }
    if runner_name == "DeerFlowAgentRunner":
        return {
            "external_agent": "deerflow",
            "external_api_mode": "langgraph_stream",
            "base_url": api_base,
            "resource_path": "/api/langgraph/threads/{thread_id}/runs/stream",
            "route_resolution": "constructed",
            "timeout_seconds": timeout,
            "proxy_configured": bool(getattr(runner, "proxy", None)),
            "remote_resource_id_hash": stable_identifier_hash(
                getattr(runner, "assistant_id", None)
            ),
            "effective_model": getattr(runner, "model_name", None),
            "plan_mode": getattr(runner, "plan_mode", None),
            "subagent_enabled": getattr(runner, "subagent_enabled", None),
        }
    return {}


def _finish_run(
    runner: Any,
    run_span: TraceSpan,
    *,
    status: str | None = None,
    outcome: str | None = None,
) -> None:
    if getattr(runner, "_astrbot_trace_run_finalized", False):
        return
    if status is None:
        if callable(getattr(runner, "was_aborted", None)) and runner.was_aborted():
            status = "cancelled"
            outcome = "aborted"
        elif getattr(getattr(runner, "_state", None), "name", "") == "ERROR":
            status = "error"
            outcome = "agent_error"
        else:
            status = "success"
            outcome = outcome or "completed"
    runner._astrbot_trace_run_finalized = True
    stats = getattr(runner, "stats", None)
    usage = getattr(stats, "token_usage", None)
    run_span.set_attributes(
        step_count=getattr(runner, "_astrbot_trace_step_index", 0),
        model_call_count=getattr(runner, "_trace_model_call_index", 0),
        final_message_count=len(
            getattr(getattr(runner, "run_context", None), "messages", []) or []
        ),
        tool_call_count=getattr(runner, "_astrbot_trace_tool_call_count", 0),
        forced_final=bool(getattr(runner, "_astrbot_trace_forced_final", False)),
        aborted=bool(
            callable(getattr(runner, "was_aborted", None)) and runner.was_aborted()
        ),
        usage_input_tokens=getattr(usage, "input", None),
        usage_output_tokens=getattr(usage, "output", None),
        usage_total_tokens=getattr(usage, "total", None),
    )
    run_span.finish(status=status, outcome=outcome)


def _record_partial_outbound(run_span: TraceSpan, error: BaseException) -> None:
    """Close an external Agent stream attempt when its consumer stops early."""

    try:
        if not has_outbound_observation(run_span):
            return
        record_active_outbound_failure(error, span=run_span)
        record_outbound_result_attributes(span=run_span, partial=True)
    except Exception:
        return


@contextlib.contextmanager
def _activate(span: TraceSpan | NoopTraceSpan) -> Iterator[None]:
    """Make a running span current only while advancing an async generator."""

    if isinstance(span, NoopTraceSpan):
        yield
        return
    with span.activate():
        yield


async def _close_generator(
    generator: AsyncGenerator[Any, None],
    *active_spans: TraceSpan | NoopTraceSpan,
) -> None:
    """Close a wrapped generator with its execution context temporarily active."""

    try:
        with contextlib.ExitStack() as stack:
            for span in active_spans:
                stack.enter_context(_activate(span))
            await generator.aclose()
    except Exception:
        return


def _finish_existing_run(
    runner: Any,
    *,
    status: str | None = None,
    outcome: str | None = None,
    exception_type: str | None = None,
) -> None:
    """Finish an already-created run without materializing an empty workflow."""

    run_span = getattr(runner, "_astrbot_trace_run_span", None)
    if not isinstance(run_span, TraceSpan):
        return
    if exception_type is not None:
        run_span.set_attributes(exception_type=exception_type)
    _finish_run(runner, run_span, status=status, outcome=outcome)
