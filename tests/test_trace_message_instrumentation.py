"""Focused Trace tests for adapter-neutral message delivery instrumentation."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator

import pytest

from astrbot.core.message.components import Image, Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.trace.context import current_span_id
from astrbot.core.trace.service import TraceService


class _Event(AstrMessageEvent):
    """Minimal concrete event that exposes the dynamic delivery wrappers."""

    def __init__(self, *args, **kwargs) -> None:
        self.sent: list[MessageChain | None] = []
        super().__init__(*args, **kwargs)

    async def send(self, message: MessageChain | None) -> None:
        self.sent.append(message)

    async def send_streaming(
        self,
        generator: AsyncGenerator[MessageChain, None],
        use_fallback: bool = False,
    ) -> None:
        async for chain in generator:
            await self.send(chain)


def _event() -> _Event:
    """Build a minimal message event independent of a real platform SDK."""

    message = AstrBotMessage()
    message.type = MessageType.FRIEND_MESSAGE
    message.self_id = "bot"
    message.session_id = "session"
    message.message_id = "message"
    message.sender = MessageMember(user_id="sender", nickname="Sender")
    message.message = [Plain("hello")]
    message.message_str = "hello"
    return _Event(
        message_str="hello",
        message_obj=message,
        platform_meta=PlatformMetadata(
            name="test",
            description="test platform",
            id="test-id",
        ),
        session_id="session",
    )


@pytest.mark.asyncio
async def test_direct_delivery_is_a_child_span_with_media_references(tmp_path):
    """A normal adapter send persists one semantic outgoing MessageChain."""

    service = TraceService(tmp_path / "trace")
    await service.initialize()
    try:
        event = _event()
        encoded = "A" * 8192
        with service.start_root("message.process"):
            await event.send(
                MessageChain([Plain("reply"), Image.fromBase64(encoded)]),
            )
        await service.flush()

        trace = (await service.store.list_traces())[0]
        detail = await service.store.get_trace(trace["trace_id"])
        delivery = next(
            span for span in detail["spans"] if span["operation"] == "message.send"
        )
        assert delivery["parent_span_id"] == trace["root_span_id"]
        ref = next(
            ref for ref in detail["artifact_refs"] if ref["role"] == "message.outgoing"
        )
        body, _ = await service.store.get_artifact_body(ref["content_hash"])
        payload = json.loads(body)
        assert payload["components"][0]["data"]["text"] == "reply"
        media_ref = payload["components"][1]["data"]["file"]["media_ref"]
        assert media_ref["encoding"] == "base64_uri"
        assert encoded[:128] not in body.decode("utf-8")
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_stream_delivery_keeps_agent_output_outside_delivery_span(tmp_path):
    """One terminal stream artifact replaces per-delta durable delivery records."""

    service = TraceService(tmp_path / "trace")
    await service.initialize()
    try:
        event = _event()
        with service.start_root("message.process") as root:

            async def source() -> AsyncGenerator[MessageChain, None]:
                assert current_span_id() == root.span_id
                yield MessageChain([Plain("hello ")])
                assert current_span_id() == root.span_id
                yield MessageChain([Plain("world")])
                yield MessageChain(
                    [Plain("A" * 8192)],
                    type="audio_chunk",
                )

            await event.send_streaming(source())
        await service.flush()

        trace = (await service.store.list_traces())[0]
        detail = await service.store.get_trace(trace["trace_id"])
        spans = detail["spans"]
        stream = next(span for span in spans if span["operation"] == "response.deliver")
        assert stream["parent_span_id"] == trace["root_span_id"]
        assert not any(span["operation"] == "message.send" for span in spans)
        ref = next(
            ref for ref in detail["artifact_refs"] if ref["role"] == "response.delivery"
        )
        body, _ = await service.store.get_artifact_body(ref["content_hash"])
        payload = json.loads(body)
        assert payload["text"] == "hello world"
        assert payload["audio_chunk_count"] == 1
        assert "A" * 128 not in body.decode("utf-8")
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_delivery_without_active_trace_keeps_adapter_behavior(tmp_path):
    """A bare adapter event never creates an implicit delivery root Trace."""

    service = TraceService(tmp_path / "trace")
    await service.initialize()
    try:
        event = _event()
        await event.send(MessageChain([Plain("reply")]))
        assert event.sent[0].get_plain_text() == "reply"
        await service.flush()
        assert await service.store.list_traces() == []
    finally:
        await service.close()
