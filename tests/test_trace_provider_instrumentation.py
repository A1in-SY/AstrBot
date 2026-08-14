"""Regression tests for type-preserving Provider trace instrumentation."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from astrbot.core.provider.provider import EmbeddingProvider, Provider
from astrbot.core.trace.provider_instrumentation import instrument_provider
from astrbot.core.trace.service import TraceService


class _ChatProvider(Provider):
    def __init__(self) -> None:
        super().__init__(
            {"id": "test-chat", "type": "test_chat"},
            {},
        )

    def get_current_key(self) -> str:
        return ""

    def set_key(self, key: str) -> None:
        return None

    async def get_models(self) -> list[str]:
        return ["test-model"]

    async def text_chat(self, **kwargs):
        return "response"

    async def text_chat_stream(self, **kwargs):
        yield "one"
        yield "two"


class _EmbeddingProvider(EmbeddingProvider):
    def __init__(self) -> None:
        super().__init__(
            {"id": "test-embedding", "type": "test_embedding"},
            {},
        )

    async def get_embedding(self, text: str) -> list[float]:
        return (await self.get_embeddings([text]))[0]

    async def get_embeddings(self, text: list[str]) -> list[list[float]]:
        return [[float(len(item))] for item in text]

    def get_dim(self) -> int:
        return 1


class _RetryingBatchEmbeddingProvider(EmbeddingProvider):
    def __init__(self) -> None:
        super().__init__(
            {
                "id": "test-batch-embedding",
                "type": "openai_embedding",
                "embedding_api_base": "https://example.com/v1",
            },
            {},
        )
        self.calls: dict[tuple[str, ...], int] = {}

    async def get_embedding(self, text: str) -> list[float]:
        return (await self.get_embeddings([text]))[0]

    async def get_embeddings(self, text: list[str]) -> list[list[float]]:
        key = tuple(text)
        self.calls[key] = self.calls.get(key, 0) + 1
        if key == ("aa", "bb") and self.calls[key] == 1:
            raise RuntimeError("temporary batch failure")
        return [[float(len(item))] for item in text]

    def get_dim(self) -> int:
        return 1


@pytest.mark.asyncio
async def test_direct_chat_provider_call_becomes_a_root_trace(tmp_path):
    """A Core-managed direct Provider call must not need a Pipeline context."""

    service = TraceService(tmp_path / "trace")
    await service.initialize()
    try:
        provider = _ChatProvider()
        instrument_provider(provider, service)

        assert await provider.text_chat(prompt="hello") == "response"
        await service.flush()

        trace = (await service.store.list_traces())[0]
        assert trace["operation"] == "model.call"
        detail = await service.store.get_trace(trace["trace_id"])
        assert detail["spans"][0]["attributes"]["provider_id"] == "test-chat"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_provider_call_joins_existing_business_trace_as_a_child(tmp_path):
    """Direct provider dispatch in a business trace must not create a second root."""

    service = TraceService(tmp_path / "trace")
    await service.initialize()
    try:
        provider = _ChatProvider()
        instrument_provider(provider, service)

        with service.start_root("group_summary.run"):
            await provider.text_chat(prompt="hello")
        await service.flush()

        traces = await service.store.list_traces()
        assert len(traces) == 1
        detail = await service.store.get_trace(traces[0]["trace_id"])
        spans = {span["operation"]: span for span in detail["spans"]}
        assert spans["model.call"]["parent_span_id"] == traces[0]["root_span_id"]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_embedding_internal_delegation_creates_one_logical_span(tmp_path):
    """get_embedding delegating to get_embeddings must not double-record a call."""

    service = TraceService(tmp_path / "trace")
    await service.initialize()
    try:
        provider = _EmbeddingProvider()
        instrument_provider(provider, service)

        assert await provider.get_embedding("abc") == [3.0]
        await service.flush()

        trace = (await service.store.list_traces())[0]
        detail = await service.store.get_trace(trace["trace_id"])
        assert [span["operation"] for span in detail["spans"]] == ["embedding.call"]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_embedding_batch_records_real_dispatches_and_business_retry(
    tmp_path,
    monkeypatch,
):
    """The batch coordinator must not masquerade as one transport attempt."""

    monkeypatch.setattr(
        "astrbot.core.provider.provider.asyncio.sleep",
        AsyncMock(),
    )
    service = TraceService(tmp_path / "trace")
    await service.initialize()
    try:
        provider = _RetryingBatchEmbeddingProvider()
        instrument_provider(provider, service)

        result = await provider.get_embeddings_batch(
            ["aa", "bb", "cc", "dd"],
            batch_size=2,
            tasks_limit=2,
            max_retries=2,
        )
        assert result == [[2.0], [2.0], [2.0], [2.0]]
        await service.flush()

        trace = (await service.store.list_traces())[0]
        detail = await service.store.get_trace(trace["trace_id"])
        assert [span["operation"] for span in detail["spans"]] == ["embedding.call"]
        span = detail["spans"][0]
        assert span["attributes"]["attempt_count"] == 3
        assert span["attributes"]["retry_count"] == 1
        assert span["attributes"]["request_variant_count"] == 2
        assert span["attributes"]["batch_count"] == 2
        assert span["attributes"]["concurrency"] == 2
        assert [
            event["name"]
            for event in detail["events"]
            if event["name"] == "outbound.request.retry"
        ] == ["outbound.request.retry"]
        refs = [
            ref
            for ref in detail["artifact_refs"]
            if ref["role"] == "outbound.effective_request"
        ]
        assert len(refs) == 2
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_provider_probe_is_explicitly_suppressed(tmp_path):
    """Provider.test must never emit a business trace."""

    service = TraceService(tmp_path / "trace")
    await service.initialize()
    try:
        provider = _ChatProvider()
        instrument_provider(provider, service)

        await provider.test()
        await service.flush()
        assert await service.store.list_traces() == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_streaming_provider_creates_one_span_and_aggregates_chunk_count(tmp_path):
    """A stream records one logical operation rather than one row per chunk."""

    service = TraceService(tmp_path / "trace")
    await service.initialize()
    try:
        provider = _ChatProvider()
        instrument_provider(provider, service)

        chunks = [chunk async for chunk in provider.text_chat_stream(prompt="hello")]
        assert chunks == ["one", "two"]
        await service.flush()

        trace = (await service.store.list_traces())[0]
        detail = await service.store.get_trace(trace["trace_id"])
        assert detail["spans"][0]["attributes"]["chunk_count"] == 2
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_stream_context_is_not_visible_while_consumer_handles_yield(tmp_path):
    """A yielded provider chunk must not leak its model.call to the consumer."""

    service = TraceService(tmp_path / "trace")
    await service.initialize()
    try:
        provider = _ChatProvider()
        instrument_provider(provider, service)
        with service.start_root("group_summary.run") as root:
            stream = provider.text_chat_stream(prompt="hello")
            assert await anext(stream) == "one"
            assert service.current_ids() == (root.trace_id, root.span_id)
            await stream.aclose()
        await service.flush()

        trace = (await service.store.list_traces())[0]
        detail = await service.store.get_trace(trace["trace_id"])
        model = next(
            span for span in detail["spans"] if span["operation"] == "model.call"
        )
        assert model["status"] == "cancelled"
        assert model["outcome"] == "generator_closed"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_provider_artifacts_capture_semantics_without_base64_payload(tmp_path):
    """Known inline media stays a reference in stored Provider request artifacts."""

    service = TraceService(tmp_path / "trace")
    await service.initialize()
    try:
        provider = _ChatProvider()
        instrument_provider(provider, service)
        encoded_media = "data:image/png;base64," + "A" * 8192
        await provider.text_chat(
            contexts=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": encoded_media}}
                    ],
                }
            ]
        )
        await service.flush()

        trace = (await service.store.list_traces())[0]
        detail = await service.store.get_trace(trace["trace_id"])
        refs_by_role = {ref["role"]: ref for ref in detail["artifact_refs"]}
        body, _ = await service.store.get_artifact_body(
            refs_by_role["provider.request"]["content_hash"]
        )
        request = json.loads(body)
        stored_url = request["kwargs"]["contexts"][0]["content"][0]["image_url"]["url"]
        assert stored_url["media_ref"]["encoding"] == "data_uri_base64"
        assert "A" * 128 not in body.decode("utf-8")
        assert "provider.response" in refs_by_role
    finally:
        await service.close()
