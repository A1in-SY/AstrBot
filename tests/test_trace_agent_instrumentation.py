"""Focused lifecycle tests for dynamically instrumented Agent Runners."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest

from astrbot.core.agent.hooks import BaseAgentRunHooks
from astrbot.core.agent.runners.base import AgentState, BaseAgentRunner
from astrbot.core.trace.agent_instrumentation import instrument_agent_runner
from astrbot.core.trace.service import TraceService


class _Runner(BaseAgentRunner[Any]):
    async def reset(self, run_context, agent_hooks, **kwargs) -> None:
        self.run_context = run_context
        self.agent_hooks = agent_hooks
        self._state = AgentState.IDLE
        self._step_count = 0

    async def step(self) -> AsyncGenerator[str, None]:
        self._state = AgentState.RUNNING
        self._step_count += 1
        yield f"step-{self._step_count}"
        if self._step_count >= 2:
            self._state = AgentState.DONE

    async def step_until_done(self, max_step: int) -> AsyncGenerator[str, None]:
        while not self.done() and self._step_count < max_step:
            async for response in self.step():
                yield response

    def done(self) -> bool:
        return self._state in (AgentState.DONE, AgentState.ERROR)

    def get_final_llm_resp(self):
        return None


@pytest.mark.asyncio
async def test_runner_reset_is_lazy_and_steps_share_one_agent_run(tmp_path):
    """Reset should not trace; two consumed steps share one agent.run root child."""

    service = TraceService(tmp_path / "trace")
    await service.initialize()
    try:
        runner = _Runner()
        instrument_agent_runner(runner, service)
        with service.start_lazy_trace("message.process"):
            await runner.reset(None, BaseAgentRunHooks())
            assert await service.store.list_traces() == []
            assert [item async for item in runner.step()] == ["step-1"]
            assert [item async for item in runner.step()] == ["step-2"]
        await service.flush()

        trace = (await service.store.list_traces())[0]
        detail = await service.store.get_trace(trace["trace_id"])
        spans_by_operation: dict[str, list[dict]] = {}
        for span in detail["spans"]:
            spans_by_operation.setdefault(span["operation"], []).append(span)
        assert len(spans_by_operation["agent.run"]) == 1
        assert len(spans_by_operation["agent.step"]) == 2
        assert spans_by_operation["agent.run"][0]["status"] == "success"
        assert [
            span["attributes"]["step_index"]
            for span in spans_by_operation["agent.step"]
        ] == [
            1,
            2,
        ]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_closed_step_generator_finalizes_the_agent_run(tmp_path):
    """Consumer-side generator closure must not leave a running agent span behind."""

    service = TraceService(tmp_path / "trace")
    await service.initialize()
    try:
        runner = _Runner()
        instrument_agent_runner(runner, service)
        with service.start_root("group_summary.run"):
            await runner.reset(None, BaseAgentRunHooks())
            stream = runner.step()
            assert await anext(stream) == "step-1"
            await stream.aclose()
        await service.flush()

        trace = (await service.store.list_traces())[0]
        detail = await service.store.get_trace(trace["trace_id"])
        run = next(span for span in detail["spans"] if span["operation"] == "agent.run")
        assert run["status"] == "cancelled"
        assert run["outcome"] == "generator_closed"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_agent_step_context_is_not_visible_while_consumer_handles_yield(tmp_path):
    """A yielded response must not leave delivery work parented to agent.step."""

    service = TraceService(tmp_path / "trace")
    await service.initialize()
    try:
        runner = _Runner()
        instrument_agent_runner(runner, service)
        with service.start_root("group_summary.run") as root:
            await runner.reset(None, BaseAgentRunHooks())
            stream = runner.step()
            assert await anext(stream) == "step-1"
            assert service.current_ids() == (root.trace_id, root.span_id)
            await stream.aclose()
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_step_until_done_marks_an_exhausted_run_incomplete(tmp_path):
    """A bounded runner workflow cannot leave a durable agent.run running."""

    service = TraceService(tmp_path / "trace")
    await service.initialize()
    try:
        runner = _Runner()
        instrument_agent_runner(runner, service)
        with service.start_root("group_summary.run"):
            await runner.reset(None, BaseAgentRunHooks())
            assert [item async for item in runner.step_until_done(1)] == ["step-1"]
        await service.flush()

        trace = (await service.store.list_traces())[0]
        detail = await service.store.get_trace(trace["trace_id"])
        run = next(span for span in detail["spans"] if span["operation"] == "agent.run")
        assert run["status"] == "incomplete"
        assert run["outcome"] == "step_limit_reached"
    finally:
        await service.close()
