"""Trace coverage for Live Mode's concurrent TTS worker."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from types import SimpleNamespace

import pytest

import astrbot.core.astr_agent_run_util as agent_run_util
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.trace.provider_instrumentation import instrument_provider
from astrbot.core.trace.service import TraceService


class _TTSProvider:
    """Small managed-provider stand-in that produces one audio chunk."""

    def support_stream(self) -> bool:
        return True

    def meta(self):
        return SimpleNamespace(type="test_tts")

    async def get_audio_stream(self, text_queue, audio_queue) -> None:
        while True:
            text = await text_queue.get()
            if text is None:
                return
            await audio_queue.put(text.encode("utf-8"))


@pytest.mark.asyncio
async def test_live_tts_is_a_sibling_pipeline_with_provider_child(
    tmp_path,
    monkeypatch,
):
    """Live TTS does not inherit the Agent Span and keeps its Provider child."""

    async def fake_run_agent(
        *args, **kwargs
    ) -> AsyncGenerator[MessageChain | None, None]:
        yield MessageChain([Plain("hello")])

    monkeypatch.setattr(agent_run_util, "run_agent", fake_run_agent)
    service = TraceService(tmp_path / "trace")
    await service.initialize()
    try:
        provider = _TTSProvider()
        instrument_provider(provider, service)
        event = SimpleNamespace(track_temporary_local_file=lambda path: None)
        runner = SimpleNamespace(
            run_context=SimpleNamespace(context=SimpleNamespace(event=event)),
            provider=SimpleNamespace(get_model=lambda: "test_model"),
        )
        with service.start_root("message.process") as root:
            outputs = [
                chain
                async for chain in agent_run_util.run_live_agent(
                    runner,
                    provider,
                )
            ]
        await service.flush()

        assert outputs[0].type == "audio_chunk"
        trace = (await service.store.list_traces())[0]
        detail = await service.store.get_trace(trace["trace_id"])
        tts_pipeline = next(
            span for span in detail["spans"] if span["operation"] == "tts.pipeline"
        )
        tts_call = next(
            span for span in detail["spans"] if span["operation"] == "tts.call"
        )
        assert tts_pipeline["parent_span_id"] == root.span_id
        assert tts_call["parent_span_id"] == tts_pipeline["span_id"]
        assert tts_pipeline["attributes"]["audio_chunk_count"] == 1
        assert tts_pipeline["status"] == "success"
    finally:
        await service.close()
