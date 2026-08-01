"""Lifecycle tracing for Core-created Agent Runner instances."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
from collections.abc import AsyncGenerator, Iterator
from typing import Any

from .context import TraceState, current_span_id, current_trace_state
from .service import NoopTraceSpan, TraceService, TraceSpan


def instrument_agent_runner(runner: Any, trace_service: TraceService | None) -> None:
    """Attach non-invasive reset/step lifecycle tracing to one runner instance.

    The concrete runner remains the same object and keeps its public API.  The
    wrapper records reset's parent context but delays ``agent.run`` creation
    until the first real consumption of ``step()``.

    Args:
        runner: A Core-created BaseAgentRunner implementation.
        trace_service: The Core trace service, or ``None`` when unavailable.
    """

    if trace_service is None:
        return
    try:
        if getattr(runner, "_astrbot_trace_instrumented", False):
            return
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
        with run_span.activate():
            step_span = service.start_span(
                "agent.step",
                kind="agent",
                attributes={
                    "step_index": runner._astrbot_trace_step_index,
                    "step_kind": step_kind,
                },
            )
        original_generator = original_step(*args, **kwargs)
        original_closed = False
        try:
            while True:
                try:
                    with _activate(run_span), _activate(step_span):
                        response = await anext(original_generator)
                except StopAsyncIteration:
                    original_closed = True
                    step_span.finish()
                    break
                yield response
        except GeneratorExit:
            await _close_generator(original_generator, run_span, step_span)
            original_closed = True
            step_span.finish(status="cancelled", outcome="generator_closed")
            _finish_run(
                runner, run_span, status="cancelled", outcome="generator_closed"
            )
            raise
        except asyncio.CancelledError:
            await _close_generator(original_generator, run_span, step_span)
            original_closed = True
            step_span.finish(status="cancelled", outcome="cancelled")
            _finish_run(runner, run_span, status="cancelled", outcome="cancelled")
            raise
        except BaseException as exc:
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
    run_span.finish(status=status, outcome=outcome)


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
