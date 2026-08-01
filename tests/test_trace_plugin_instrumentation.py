"""Regression tests for automatic plugin handler Trace boundaries."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from astrbot.core.pipeline.process_stage.stage import StarRequestSubStage
from astrbot.core.star.star import StarMetadata, star_map
from astrbot.core.star.star_handler import EventType, StarHandlerMetadata
from astrbot.core.trace.service import TraceService


class _Event:
    """Small event double exposing only the plugin Stage contract."""

    def __init__(self, handler: StarHandlerMetadata[Any]) -> None:
        self._extras = {
            "activated_handlers": [handler],
            "handlers_parsed_params": {},
        }
        self._result: Any = None
        self._stopped = False
        self.is_at_or_wake_command = False
        self.plugins_name = ["*"]

    def get_extra(self, key: str) -> Any:
        return self._extras.get(key)

    def is_stopped(self) -> bool:
        return self._stopped

    def stop_event(self) -> None:
        self._stopped = True

    def get_result(self) -> Any:
        return self._result

    def set_result(self, result: Any) -> None:
        self._result = result

    def clear_result(self) -> None:
        self._result = None


def _handler_metadata(handler: Any, module_path: str) -> StarHandlerMetadata[Any]:
    return StarHandlerMetadata(
        event_type=EventType.AdapterMessageEvent,
        handler_full_name=f"{module_path}_{handler.__name__}",
        handler_name=handler.__name__,
        handler_module_path=module_path,
        handler=handler,
        event_filters=[],
    )


def _stage(service: TraceService) -> StarRequestSubStage:
    stage = StarRequestSubStage()
    stage.ctx = SimpleNamespace(trace_service=service)
    return stage


@pytest.mark.asyncio
async def test_plugin_handler_span_is_reset_before_yield_and_active_during_close(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Downstream work stays under the root while plugin cleanup stays traced."""

    service = TraceService(tmp_path / "trace")
    await service.initialize()
    module_path = "tests.trace_plugin_yield"
    handler_contexts: list[tuple[str | None, str | None]] = []
    close_contexts: list[tuple[str | None, str | None]] = []

    async def yielding_handler(event):
        del event
        handler_contexts.append(service.current_ids())
        try:
            yield "plugin-result"
        finally:
            close_contexts.append(service.current_ids())

    metadata = _handler_metadata(yielding_handler, module_path)
    monkeypatch.setitem(
        star_map,
        module_path,
        StarMetadata(name="TracePlugin", author="Tests"),
    )

    try:
        with service.start_root("test.plugin_pipeline") as root:
            stream = _stage(service).process(_Event(metadata))
            assert await anext(stream) == "plugin-result"

            plugin_ids = handler_contexts[0]
            assert plugin_ids[0] == root.trace_id
            assert plugin_ids[1] != root.span_id
            assert service.current_ids() == (root.trace_id, root.span_id)

            with service.start_span("test.downstream") as downstream:
                downstream_span_id = downstream.span_id

            await stream.aclose()
            assert close_contexts == [plugin_ids]
            assert service.current_ids() == (root.trace_id, root.span_id)

        await service.flush()
        trace = (await service.store.list_traces())[0]
        detail = await service.store.get_trace(trace["trace_id"])
        plugin_span = next(
            span for span in detail["spans"] if span["operation"] == "plugin.handler"
        )
        downstream_span = next(
            span for span in detail["spans"] if span["span_id"] == downstream_span_id
        )
        assert plugin_span["status"] == "cancelled"
        assert plugin_span["outcome"] == "generator_closed"
        assert downstream_span["parent_span_id"] == root.span_id
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_plugin_handler_cancellation_resets_context_and_terminalizes_span(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Cancellation during ``anext`` produces one cancelled handler Span."""

    service = TraceService(tmp_path / "trace")
    await service.initialize()
    module_path = "tests.trace_plugin_cancel"
    started = asyncio.Event()
    handler_contexts: list[tuple[str | None, str | None]] = []

    async def blocking_handler(event):
        del event
        handler_contexts.append(service.current_ids())
        started.set()
        await asyncio.Future()
        yield None

    metadata = _handler_metadata(blocking_handler, module_path)
    monkeypatch.setitem(
        star_map,
        module_path,
        StarMetadata(name="TracePlugin", author="Tests"),
    )

    try:
        with service.start_root("test.plugin_pipeline") as root:
            stream = _stage(service).process(_Event(metadata))
            pending = asyncio.create_task(anext(stream))
            await asyncio.wait_for(started.wait(), timeout=5)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending

            assert handler_contexts[0][0] == root.trace_id
            assert handler_contexts[0][1] != root.span_id
            assert service.current_ids() == (root.trace_id, root.span_id)

        await service.flush()
        trace = (await service.store.list_traces())[0]
        detail = await service.store.get_trace(trace["trace_id"])
        plugin_span = next(
            span for span in detail["spans"] if span["operation"] == "plugin.handler"
        )
        assert plugin_span["status"] == "cancelled"
        assert plugin_span["outcome"] == "cancelled"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_plugin_handler_normal_exhaustion_finishes_successfully(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A fully consumed handler retains its normal success lifecycle."""

    service = TraceService(tmp_path / "trace")
    await service.initialize()
    module_path = "tests.trace_plugin_success"

    async def yielding_handler(event):
        del event
        yield "plugin-result"

    metadata = _handler_metadata(yielding_handler, module_path)
    monkeypatch.setitem(
        star_map,
        module_path,
        StarMetadata(name="TracePlugin", author="Tests"),
    )

    try:
        with service.start_root("test.plugin_pipeline"):
            results = [item async for item in _stage(service).process(_Event(metadata))]
            assert results == ["plugin-result"]

        await service.flush()
        trace = (await service.store.list_traces())[0]
        detail = await service.store.get_trace(trace["trace_id"])
        plugin_span = next(
            span for span in detail["spans"] if span["operation"] == "plugin.handler"
        )
        assert plugin_span["status"] == "success"
        assert plugin_span["attributes"]["yield_count"] == 1
        assert [event["name"] for event in detail["events"]] == [
            "plugin.handler.completed"
        ]
    finally:
        await service.close()
