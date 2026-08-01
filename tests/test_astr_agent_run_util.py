from types import SimpleNamespace

import pytest

from astrbot.core.agent.response import AgentResponse
from astrbot.core.astr_agent_run_util import run_agent
from astrbot.core.message.message_event_result import MessageChain


class _FakeEvent:
    """Minimal event surface used by the agent stream bridge."""

    def is_stopped(self) -> bool:
        return False

    def get_extra(self, key: str):
        del key
        return None

    def get_platform_name(self) -> str:
        return "test"


class _StreamingErrorRunner:
    """Agent runner that finishes with one provider error response."""

    streaming = True
    req = None

    def __init__(self, error_text: str) -> None:
        self.error_text = error_text
        self.finished = False
        self.run_context = SimpleNamespace(context=SimpleNamespace(event=_FakeEvent()))

    async def step(self):
        self.finished = True
        yield AgentResponse(
            type="err",
            data={"chain": MessageChain().message(self.error_text)},
        )

    def done(self) -> bool:
        return self.finished


class _MalformedStreamingErrorRunner(_StreamingErrorRunner):
    """Agent runner that returns an invalid provider error payload."""

    async def step(self):
        self.finished = True
        yield AgentResponse(type="err", data={})


class _ClosableStreamingRunner:
    """Runner exposing whether consumer-side close reaches ``step()``."""

    streaming = True
    req = None

    def __init__(self) -> None:
        self.step_closed = False
        self.run_context = SimpleNamespace(context=SimpleNamespace(event=_FakeEvent()))

    async def step(self):
        try:
            yield AgentResponse(
                type="streaming_delta",
                data={"chain": MessageChain().message("partial")},
            )
        finally:
            self.step_closed = True

    def done(self) -> bool:
        return False

    def request_stop(self) -> None:
        return None


@pytest.mark.asyncio
async def test_run_agent_forwards_streaming_provider_error():
    error_text = (
        "LLM 响应错误: Not found the model k2.7-code-highspeed or Permission denied"
    )
    runner = _StreamingErrorRunner(error_text)

    chains = [chain async for chain in run_agent(runner)]

    assert len(chains) == 1
    assert chains[0].get_plain_text() == error_text


@pytest.mark.asyncio
async def test_run_agent_replaces_malformed_streaming_provider_error():
    runner = _MalformedStreamingErrorRunner("unused")

    chains = [chain async for chain in run_agent(runner)]

    assert len(chains) == 1
    assert chains[0].get_plain_text() == "Error occurred during AI execution."


@pytest.mark.asyncio
async def test_run_agent_close_reaches_runner_step():
    runner = _ClosableStreamingRunner()
    stream = run_agent(runner)

    chain = await anext(stream)
    assert chain is not None
    assert chain.get_plain_text() == "partial"
    await stream.aclose()

    assert runner.step_closed is True
