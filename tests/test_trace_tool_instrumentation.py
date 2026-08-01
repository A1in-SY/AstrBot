"""Regression tests for logical ToolLoop tool span instrumentation."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import mcp
import pytest

from astrbot.core.agent.agent import Agent
from astrbot.core.agent.handoff import HandoffTool
from astrbot.core.agent.hooks import BaseAgentRunHooks
from astrbot.core.agent.mcp_client import MCPTool
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.runners.tool_loop_agent_runner import (
    ToolLoopAgentRunner,
    _trace_tool_operation,
)
from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
from astrbot.core.provider.entities import LLMResponse, ProviderRequest
from astrbot.core.provider.provider import Provider
from astrbot.core.trace.agent_instrumentation import instrument_agent_runner
from astrbot.core.trace.context import current_trace_service
from astrbot.core.trace.provider_instrumentation import instrument_provider
from astrbot.core.trace.service import TraceService


class _ToolCallingProvider(Provider):
    def __init__(self) -> None:
        super().__init__({"id": "trace-tool", "type": "test"}, {})
        self.calls = 0

    def get_current_key(self) -> str:
        return ""

    def set_key(self, key: str) -> None:
        return None

    async def get_models(self) -> list[str]:
        return ["test"]

    async def text_chat(self, **kwargs: Any) -> LLMResponse:
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                role="assistant",
                tools_call_name=["lookup"],
                tools_call_args=[{"query": "Trace"}],
                tools_call_ids=["call-1"],
            )
        return LLMResponse(role="assistant", completion_text="done")


class _YieldingExecutor:
    async def execute(self, **kwargs: Any):
        del kwargs
        yield mcp.types.CallToolResult(
            content=[mcp.types.TextContent(type="text", text="first")]
        )
        yield mcp.types.CallToolResult(
            content=[mcp.types.TextContent(type="text", text="second")]
        )


def test_mcp_tool_uses_the_distinct_mcp_operation() -> None:
    """A real MCPTool must not collapse into the generic tool.call category."""

    tool = MCPTool(
        SimpleNamespace(
            name="quote_lookup",
            description="Look up a quote",
            inputSchema={"type": "object", "properties": {}},
        ),
        SimpleNamespace(),
        "quotes",
    )

    assert _trace_tool_operation(tool) == "mcp.tool.call"


@pytest.mark.asyncio
async def test_tool_loop_records_one_logical_tool_span_and_final_result(tmp_path):
    """Multiple executor yields stay within one tool.call span and one final artifact."""

    service = TraceService(tmp_path / "trace")
    await service.initialize()
    try:
        provider = _ToolCallingProvider()
        instrument_provider(provider, service)
        runner = ToolLoopAgentRunner()
        instrument_agent_runner(runner, service)
        tool = FunctionTool(
            name="lookup",
            description="look up a value",
            parameters={"type": "object", "properties": {"query": {"type": "string"}}},
            handler=lambda *_args, **_kwargs: None,
        )
        request = ProviderRequest(prompt="please look up", func_tool=ToolSet([tool]))
        with service.start_root("group_summary.run"):
            await runner.reset(
                provider=provider,
                request=request,
                run_context=ContextWrapper(context=None),
                tool_executor=_YieldingExecutor(),
                agent_hooks=BaseAgentRunHooks(),
            )
            assert [item async for item in runner.step_until_done(2)]
        await service.flush()

        trace = (await service.store.list_traces())[0]
        detail = await service.store.get_trace(trace["trace_id"])
        tool_spans = [
            span for span in detail["spans"] if span["operation"] == "tool.call"
        ]
        assert len(tool_spans) == 1
        assert tool_spans[0]["status"] == "success"
        assert tool_spans[0]["attributes"]["executor_yield_count"] == 2
        tool_ref_roles = {
            ref["role"]
            for ref in detail["artifact_refs"]
            if ref["span_id"] == tool_spans[0]["span_id"]
        }
        assert {
            "tool.definition",
            "tool.arguments.raw",
            "tool.arguments.effective",
            "tool.result",
            "tool.agent_visible_result",
        } <= tool_ref_roles
    finally:
        await service.close()


class _BackgroundEvent:
    unified_msg_origin = "webchat:FriendMessage:webchat!trace!background"
    role = "member"

    def get_extra(self, _key: str):
        return None


def _background_run_context(service: TraceService) -> ContextWrapper[Any]:
    plugin_context = SimpleNamespace(trace_service=service)
    agent_context = SimpleNamespace(
        context=plugin_context,
        event=_BackgroundEvent(),
    )
    return ContextWrapper(context=agent_context)


@pytest.mark.asyncio
async def test_regular_background_tool_detaches_and_keeps_wake_in_new_trace(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Submitted work gets a linked root and wake-main remains inside it."""

    service = TraceService(tmp_path / "trace")
    await service.initialize()
    worker_started = asyncio.Event()
    release_worker = asyncio.Event()
    worker_finished = asyncio.Event()
    worker_ids: list[tuple[str | None, str | None]] = []
    wake_ids: list[tuple[str | None, str | None]] = []

    async def _fake_execute_local(cls, tool, run_context, **tool_args):
        del cls, tool, run_context, tool_args
        trace_service = current_trace_service()
        assert trace_service is service
        worker_ids.append(service.current_ids())
        worker_started.set()
        await release_worker.wait()
        yield mcp.types.CallToolResult(
            content=[mcp.types.TextContent(type="text", text="background result")]
        )

    async def _fake_wake(cls, run_context, **kwargs):
        del cls, run_context, kwargs
        trace_service = current_trace_service()
        assert trace_service is service
        wake_ids.append(service.current_ids())
        with service.start_span("test.background.wake"):
            pass
        worker_finished.set()

    monkeypatch.setattr(
        FunctionToolExecutor,
        "_execute_local",
        classmethod(_fake_execute_local),
    )
    monkeypatch.setattr(
        FunctionToolExecutor,
        "_wake_main_agent_for_background_result",
        classmethod(_fake_wake),
    )

    tool = FunctionTool(
        name="long_running_tool",
        description="run in background",
        parameters={"type": "object", "properties": {}},
        handler=lambda *_args, **_kwargs: None,
        is_background_task=True,
    )
    run_context = _background_run_context(service)

    try:
        with service.start_root("test.foreground") as foreground_root:
            with service.start_span("tool.call", kind="tool") as submitting_span:
                executor = FunctionToolExecutor.execute(tool, run_context)
                submitted_result = await anext(executor)
                await executor.aclose()
                submitting_trace_id = submitting_span.trace_id
                submitting_span_id = submitting_span.span_id
            foreground_trace_id = foreground_root.trace_id

        await asyncio.wait_for(worker_started.wait(), timeout=5)
        assert service.current_ids() == (None, None)
        assert worker_ids[0][0] != foreground_trace_id
        assert worker_ids[0][1] is not None
        release_worker.set()
        await asyncio.wait_for(worker_finished.wait(), timeout=5)

        assert isinstance(submitted_result, mcp.types.CallToolResult)
        assert "Background task submitted" in submitted_result.content[0].text
        assert wake_ids == worker_ids

        await service.flush()
        traces = await service.store.list_traces()
        assert {trace["operation"] for trace in traces} == {
            "test.foreground",
            "tool.background.run",
        }
        foreground = next(
            trace for trace in traces if trace["operation"] == "test.foreground"
        )
        background = next(
            trace for trace in traces if trace["operation"] == "tool.background.run"
        )
        foreground_detail = await service.store.get_trace(foreground["trace_id"])
        background_detail = await service.store.get_trace(background["trace_id"])
        submitted_span = next(
            span
            for span in foreground_detail["spans"]
            if span["span_id"] == submitting_span_id
        )
        assert submitting_trace_id == foreground["trace_id"]
        assert submitted_span["status"] == "success"
        assert submitted_span["outcome"] == "submitted"
        assert submitted_span["attributes"]["background_kind"] == "tool"
        assert background["attributes"]["background_kind"] == "tool"
        assert len(background_detail["links"]) == 1
        link = background_detail["links"][0]
        assert link["relation"] == "spawned_by"
        assert link["target_trace_id"] == submitting_trace_id
        assert link["target_span_id"] == submitting_span_id
        wake_span = next(
            span
            for span in background_detail["spans"]
            if span["operation"] == "test.background.wake"
        )
        assert wake_span["parent_span_id"] == background["root_span_id"]
    finally:
        release_worker.set()
        await service.close()


@pytest.mark.asyncio
async def test_background_handoff_uses_same_detached_linked_trace_boundary(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Dynamic Handoff background mode follows the regular detached contract."""

    service = TraceService(tmp_path / "trace")
    await service.initialize()
    worker_finished = asyncio.Event()
    worker_ids: list[tuple[str | None, str | None]] = []

    async def _fake_handoff_worker(cls, tool, run_context, task_id, **tool_args):
        del cls, tool, run_context, task_id, tool_args
        assert current_trace_service() is service
        worker_ids.append(service.current_ids())
        worker_finished.set()

    monkeypatch.setattr(
        FunctionToolExecutor,
        "_do_handoff_background",
        classmethod(_fake_handoff_worker),
    )

    tool = HandoffTool(Agent(name="researcher"))
    run_context = _background_run_context(service)

    try:
        with service.start_root("test.foreground"):
            with service.start_span("tool.call", kind="tool") as submitting_span:
                executor = FunctionToolExecutor.execute(
                    tool,
                    run_context,
                    input="research this",
                    background_task=True,
                )
                submitted_result = await anext(executor)
                await executor.aclose()
                submitting_trace_id = submitting_span.trace_id
                submitting_span_id = submitting_span.span_id

        await asyncio.wait_for(worker_finished.wait(), timeout=5)
        assert isinstance(submitted_result, mcp.types.CallToolResult)
        assert "dedicated to subagent" in submitted_result.content[0].text

        await service.flush()
        traces = await service.store.list_traces()
        foreground = next(
            trace for trace in traces if trace["operation"] == "test.foreground"
        )
        background = next(
            trace for trace in traces if trace["operation"] == "tool.background.run"
        )
        assert worker_ids == [(background["trace_id"], background["root_span_id"])]
        assert background["attributes"]["background_kind"] == "handoff"

        foreground_detail = await service.store.get_trace(foreground["trace_id"])
        submitted_span = next(
            span
            for span in foreground_detail["spans"]
            if span["span_id"] == submitting_span_id
        )
        assert submitted_span["outcome"] == "submitted"
        assert submitted_span["attributes"]["background_kind"] == "handoff"

        background_detail = await service.store.get_trace(background["trace_id"])
        link = background_detail["links"][0]
        assert link["relation"] == "spawned_by"
        assert link["target_trace_id"] == submitting_trace_id
        assert link["target_span_id"] == submitting_span_id
    finally:
        await service.close()
