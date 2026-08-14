"""Tests for Core outbound call diagnostics."""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anyio
import httpx
import pytest

import astrbot.core.provider.sources.request_retry as request_retry
from astrbot.core.agent.mcp_client import MCPClient
from astrbot.core.conversation_mgr import ConversationManager
from astrbot.core.provider.sources.request_retry import retry_provider_request
from astrbot.core.trace.history_instrumentation import agent_history_persist_context
from astrbot.core.trace.outbound import (
    OutboundCallRecorder,
    OutboundRequestSnapshot,
    record_active_outbound_failure,
    record_outbound_first_chunk,
    record_outbound_recovery,
    sanitize_base_url,
    split_configured_endpoint,
)
from astrbot.core.trace.service import NoopTraceSpan, TraceService


def test_sanitize_base_url_removes_credentials_query_and_fragment() -> None:
    assert (
        sanitize_base_url(
            "https://user:password@example.com:8443/v1/?token=secret#fragment"
        )
        == "https://example.com:8443/v1"
    )
    assert sanitize_base_url("not-an-absolute-url") is None
    assert split_configured_endpoint(
        "https://user:password@example.com/tenant/raw-session?api_key=secret#fragment",
        dynamic_path_template="/{configured_path}",
        static_paths=("/v1/messages",),
    ) == ("https://example.com", "/{configured_path}")


@pytest.mark.asyncio
async def test_outbound_snapshot_stores_controls_and_summarizes_content(
    tmp_path,
) -> None:
    service = TraceService(tmp_path / "trace")
    await service.initialize()
    try:
        with service.start_root("model.call", kind="provider"):
            recorder = OutboundCallRecorder(
                OutboundRequestSnapshot(
                    api_family="openai.responses",
                    sdk_operation="client.responses.create",
                    base_url=(
                        "https://user:password@example.com/v1?api_key=raw-secret"
                    ),
                    resource_path="/responses",
                    route_resolution="sdk_declared",
                    streaming=True,
                    proxy_configured=True,
                    parameters={
                        "model": "gpt-test",
                        "input": [
                            {"role": "user", "content": "do-not-copy-this-prompt"}
                        ],
                        "reasoning": {"effort": "high"},
                        "max_output_tokens": 4096,
                        "authorization": "Bearer raw-secret",
                        "extra_body": {
                            "temperature": 0.2,
                            "api_key": "nested-secret",
                            "vendor_flag": {"opaque": True},
                        },
                    },
                    transformations=("max_tokens->max_output_tokens",),
                )
            )
            attempt = recorder.record_attempt()
            recorder.record_completed(object(), attempt_number=attempt)
        await service.flush()

        trace = (await service.store.list_traces())[0]
        detail = await service.store.get_trace(trace["trace_id"])
        span = detail["spans"][0]
        assert span["attributes"]["base_url"] == "https://example.com/v1"
        assert span["attributes"]["reasoning_effort"] == "high"
        assert span["attributes"]["token_limit_field"] == "max_output_tokens"
        assert span["attributes"]["attempt_count"] == 1
        assert span["attributes"]["transport_metadata_available"] is False

        ref = next(
            item
            for item in detail["artifact_refs"]
            if item["role"] == "outbound.effective_request"
        )
        body, _ = await service.store.get_artifact_body(ref["content_hash"])
        body_text = body.decode("utf-8")
        manifest = json.loads(body_text)
        assert "do-not-copy-this-prompt" not in body_text
        assert "raw-secret" not in body_text
        assert "nested-secret" not in body_text
        assert manifest["input_summary"]["input"]["count"] == 1
        assert "authorization" in manifest["redacted_fields"]
        assert "extra_body.api_key" in manifest["redacted_fields"]
        assert manifest["unknown_parameters"] == [
            {"path": "extra_body.vendor_flag", "type": "dict"}
        ]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_retry_records_attempts_without_duplicate_variant_artifacts(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(request_retry, "REQUEST_RETRY_WAIT_MIN_S", 0)
    monkeypatch.setattr(request_retry, "REQUEST_RETRY_WAIT_MAX_S", 0)
    service = TraceService(tmp_path / "trace")
    await service.initialize()
    calls = 0
    try:
        with service.start_root("model.call", kind="provider"):
            recorder = OutboundCallRecorder(
                OutboundRequestSnapshot(
                    api_family="openai.chat.completions",
                    sdk_operation="client.chat.completions.create",
                    base_url="https://example.com/v1",
                    resource_path="/chat/completions",
                    route_resolution="sdk_declared",
                    parameters={"model": "gpt-test", "messages": []},
                )
            )

            async def request():
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise httpx.ConnectError("temporary")
                return object()

            await retry_provider_request(
                "Test",
                request,
                max_attempts=2,
                recorder=recorder,
            )
        await service.flush()

        trace = (await service.store.list_traces())[0]
        detail = await service.store.get_trace(trace["trace_id"])
        span = detail["spans"][0]
        assert span["attributes"]["attempt_count"] == 2
        assert span["attributes"]["retry_count"] == 1
        refs = [
            item
            for item in detail["artifact_refs"]
            if item["role"] == "outbound.effective_request"
        ]
        assert len(refs) == 1
        names = [event["name"] for event in detail["events"]]
        assert names == [
            "outbound.request.prepared",
            "outbound.request.attempt",
            "outbound.request.retry",
            "outbound.request.attempt",
            "outbound.request.completed",
        ]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_retry_status_does_not_masquerade_as_final_status(tmp_path) -> None:
    """An intermediate HTTP status remains on its event, not the span result."""

    service = TraceService(tmp_path / "trace")
    await service.initialize()
    try:
        with service.start_root("model.call", kind="provider"):
            recorder = OutboundCallRecorder(
                OutboundRequestSnapshot(
                    api_family="openai.responses",
                    sdk_operation="client.responses.create",
                    parameters={"model": "gpt-test", "input": "content"},
                )
            )
            first_attempt = recorder.record_attempt()
            recorder.record_retry_response(
                attempt_number=first_attempt,
                next_attempt_number=2,
                status_code=429,
                backoff_seconds=1,
            )
            final_attempt = recorder.record_attempt()
            recorder.record_completed(object(), attempt_number=final_attempt)
        await service.flush()

        trace = (await service.store.list_traces())[0]
        detail = await service.store.get_trace(trace["trace_id"])
        span = detail["spans"][0]
        assert "status_code" not in span["attributes"]
        assert span["attributes"]["transport_metadata_available"] is True
        retry = next(
            event
            for event in detail["events"]
            if event["name"] == "outbound.request.retry"
        )
        assert retry["attributes"]["status_code"] == 429
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_mcp_reconnect_recovery_does_not_hide_transport_retry(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "astrbot.core.agent.mcp_client.wait_exponential",
        lambda **kwargs: lambda retry_state: 0,
    )
    service = TraceService(tmp_path / "trace")
    await service.initialize()
    client = MCPClient()
    result = SimpleNamespace(isError=False)
    client._server_name = "diagnostic-server"
    client._mcp_server_config = {
        "url": "https://user:password@example.com/mcp?token=secret#fragment",
        "transport": "streamable_http",
        "headers": {"Authorization": "Bearer never-persist-this"},
    }
    client.session = SimpleNamespace(
        call_tool=AsyncMock(
            side_effect=[
                anyio.ClosedResourceError(),
                result,
            ]
        )
    )
    client._reconnect = AsyncMock()
    try:
        with service.start_root("mcp.tool.call", kind="tool"):
            assert (
                await client.call_tool_with_reconnect(
                    "safe_tool_name",
                    {"prompt": "never-persist-this-argument"},
                    timedelta(seconds=5),
                )
                is result
            )
        await service.flush()

        trace = (await service.store.list_traces())[0]
        detail = await service.store.get_trace(trace["trace_id"])
        span = detail["spans"][0]
        assert span["attributes"]["attempt_count"] == 2
        assert span["attributes"]["retry_count"] == 1
        assert span["attributes"]["recovery_count"] == 1
        assert span["attributes"]["reconnect_count"] == 1
        assert [event["name"] for event in detail["events"]] == [
            "outbound.request.prepared",
            "outbound.request.attempt",
            "outbound.request.recovered",
            "outbound.request.retry",
            "outbound.request.attempt",
            "outbound.request.completed",
        ]
        persisted = (tmp_path / "trace" / "trace.db").read_bytes()
        assert b"never-persist-this" not in persisted
        assert span["attributes"]["base_url"] == "https://example.com"
        assert span["attributes"]["resource_path"] == "/mcp/tools/call"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_equal_size_payload_changes_create_distinct_sanitized_variants(
    tmp_path,
) -> None:
    service = TraceService(tmp_path / "trace")
    await service.initialize()
    try:
        with service.start_root("model.call", kind="provider"):
            for prompt in ("alpha-secret", "bravo-secret"):
                recorder = OutboundCallRecorder(
                    OutboundRequestSnapshot(
                        api_family="openai.responses",
                        sdk_operation="client.responses.create",
                        base_url="https://example.com/v1",
                        resource_path="/responses",
                        route_resolution="sdk_declared",
                        parameters={
                            "model": "gpt-test",
                            "input": prompt,
                            "max_output_tokens": 128,
                        },
                    )
                )
                attempt = recorder.record_attempt()
                recorder.record_completed(object(), attempt_number=attempt)
        await service.flush()

        trace = (await service.store.list_traces())[0]
        detail = await service.store.get_trace(trace["trace_id"])
        assert detail["spans"][0]["attributes"]["request_variant_count"] == 2
        refs = [
            item
            for item in detail["artifact_refs"]
            if item["role"] == "outbound.effective_request"
        ]
        assert [item["metadata"]["variant_index"] for item in refs] == [1, 2]
        persisted = []
        for ref in refs:
            body, _ = await service.store.get_artifact_body(ref["content_hash"])
            persisted.append(body.decode("utf-8"))
        assert "alpha-secret" not in "".join(persisted)
        assert "bravo-secret" not in "".join(persisted)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_new_outbound_storage_contains_no_secrets_or_bodies(tmp_path) -> None:
    trace_root = tmp_path / "trace"
    secrets = [
        "api-key-plain",
        "authorization-plain",
        "cookie-plain",
        "proxy-password-plain",
        "mcp-header-plain",
        "mcp-env-plain",
        "prompt-body-plain",
        "document-body-plain",
        "raw-conversation-id",
        "must-not-persist",
    ]
    service = TraceService(trace_root)
    await service.initialize()
    try:
        with service.start_root("mcp.tool.call", kind="tool"):
            recorder = OutboundCallRecorder(
                OutboundRequestSnapshot(
                    api_family="mcp.tools.call",
                    sdk_operation="session.call_tool",
                    base_url=(
                        "https://user:proxy-password-plain@example.com/mcp"
                        "?api_key=api-key-plain#authorization-plain"
                    ),
                    resource_path="tools/call",
                    route_resolution="sdk_declared",
                    proxy_configured=True,
                    parameters={
                        "api_key": "api-key-plain",
                        "authorization": "authorization-plain",
                        "cookie": "cookie-plain",
                        "headers": {"X-MCP-Key": "mcp-header-plain"},
                        "env": {"MCP_SECRET": "mcp-env-plain"},
                        "messages": [{"role": "user", "content": "prompt-body-plain"}],
                        "documents": ["document-body-plain"],
                        "model": "safe-model",
                    },
                    input_summary={
                        "conversation_id_hash": "raw-conversation-id",
                        "unknown_safe_looking_field": "must-not-persist",
                    },
                )
            )
            attempt = recorder.record_attempt()
            recorder.record_completed(object(), attempt_number=attempt)
        await service.flush()

        persisted = trace_root.joinpath("trace.db").read_bytes()
        wal_path = trace_root / "trace.db-wal"
        if wal_path.is_file():
            persisted += wal_path.read_bytes()
        for object_file in trace_root.joinpath("objects").rglob("*"):
            if object_file.is_file():
                persisted += object_file.read_bytes()
        for secret in secrets:
            assert secret.encode() not in persisted
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_recorder_failures_do_not_change_request_or_retry_behavior(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(request_retry, "REQUEST_RETRY_WAIT_MIN_S", 0)
    monkeypatch.setattr(request_retry, "REQUEST_RETRY_WAIT_MAX_S", 0)
    service = TraceService(tmp_path / "trace")
    await service.initialize()
    calls = 0
    try:
        with service.start_root("model.call", kind="provider"):
            recorder = OutboundCallRecorder(
                OutboundRequestSnapshot(
                    api_family="openai.chat.completions",
                    sdk_operation="client.chat.completions.create",
                    parameters={"model": "gpt-test", "messages": []},
                )
            )
            assert recorder.span is not None
            monkeypatch.setattr(
                recorder.span,
                "add_event",
                lambda *args, **kwargs: (_ for _ in ()).throw(
                    RuntimeError("trace-event-failed")
                ),
            )

            async def request() -> str:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise httpx.ConnectError("retry-me")
                return "business-result"

            assert (
                await retry_provider_request(
                    "Test",
                    request,
                    max_attempts=2,
                    recorder=recorder,
                )
                == "business-result"
            )
            assert calls == 2
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_disabled_trace_creates_no_outbound_data(tmp_path) -> None:
    service = TraceService(tmp_path / "trace", enabled=False)
    await service.initialize()
    try:
        span = service.start_root("model.call", kind="provider")
        assert isinstance(span, NoopTraceSpan)
        recorder = OutboundCallRecorder(
            OutboundRequestSnapshot(
                api_family="openai.responses",
                sdk_operation="client.responses.create",
                parameters={"input": "not-persisted"},
            )
        )
        assert recorder.record_attempt() == 0
        record_active_outbound_failure(RuntimeError("business-error"))
        await service.flush()
        assert await service.store.list_traces() == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_history_persist_span_records_summary_without_history_body(
    tmp_path,
) -> None:
    service = TraceService(tmp_path / "trace")
    await service.initialize()
    db = SimpleNamespace(update_conversation=AsyncMock())
    manager = ConversationManager(db, service)
    try:
        history = [
            {"role": "user", "content": "history-body-plain"},
            {"role": "assistant", "content": "history-answer-plain"},
        ]
        with service.start_root("message.process"):
            with agent_history_persist_context("internal_agent"):
                await manager.update_conversation(
                    "platform:FriendMessage:dynamic-session",
                    "dynamic-conversation-id",
                    history=history,
                    token_usage=42,
                )
        await service.flush()

        trace = (await service.store.list_traces())[0]
        detail = await service.store.get_trace(trace["trace_id"])
        persist = next(
            span
            for span in detail["spans"]
            if span["operation"] == "conversation.history.persist"
        )
        assert persist["attributes"]["pending_message_count"] == 2
        assert persist["attributes"]["role_distribution"] == {
            "assistant": 1,
            "user": 1,
        }
        assert persist["attributes"]["write_performed"] is True
        assert persist["parent_span_id"] == trace["root_span_id"]
        stored = (tmp_path / "trace" / "trace.db").read_bytes()
        assert b"history-body-plain" not in stored
        assert b"history-answer-plain" not in stored
        db.update_conversation.assert_awaited_once()
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_history_persist_without_request_context_creates_independent_root(
    tmp_path,
) -> None:
    service = TraceService(tmp_path / "trace")
    await service.initialize()
    db = SimpleNamespace(update_conversation=AsyncMock())
    manager = ConversationManager(db, service)
    try:
        with agent_history_persist_context("background_agent_result"):
            await manager.update_conversation(
                "platform:FriendMessage:background-session",
                "background-conversation",
                history=[{"role": "assistant", "content": "done"}],
            )
        await service.flush()

        traces = await service.store.list_traces()
        assert len(traces) == 1
        assert traces[0]["operation"] == "conversation.history.persist"
        assert traces[0]["attributes"]["trigger_source"] == "background_agent_result"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_golden_trace_connects_retry_recovery_tool_delivery_and_history(
    tmp_path,
) -> None:
    """One business Trace preserves logical spans and content-free diagnostics."""

    trace_root = tmp_path / "trace"
    service = TraceService(trace_root)
    await service.initialize()
    db = SimpleNamespace(update_conversation=AsyncMock())
    manager = ConversationManager(db, service)
    secret_values = (
        "golden-api-key",
        "golden-first-prompt",
        "golden-second-prompt",
        "golden-tool-argument",
        "golden-history-body",
        "golden-conversation-id",
    )
    try:
        with service.start_root("message.process", kind="pipeline") as root:
            with service.start_span("agent.run", kind="agent") as agent:
                with service.start_span("agent.step", kind="agent") as step:
                    with service.start_span("model.call", kind="provider") as model:
                        first_variant = OutboundCallRecorder(
                            OutboundRequestSnapshot(
                                api_family="openai.responses",
                                sdk_operation="client.responses.create",
                                base_url=(
                                    "https://user:golden-api-key@example.com/v1"
                                    "?api_key=golden-api-key"
                                ),
                                resource_path="/responses",
                                route_resolution="sdk_declared",
                                streaming=True,
                                parameters={
                                    "model": "gpt-golden",
                                    "input": "golden-first-prompt",
                                    "reasoning": {"effort": "high"},
                                    "max_output_tokens": 512,
                                    "api_key": "golden-api-key",
                                    "modalities": ["text", "image"],
                                },
                            )
                        )
                        first_attempt = first_variant.record_attempt()
                        first_variant.record_retry(
                            httpx.ConnectError("retry without persisting this text"),
                            attempt_number=first_attempt,
                            next_attempt_number=2,
                            backoff_seconds=0.25,
                        )
                        second_attempt = first_variant.record_attempt()
                        record_outbound_recovery(
                            "fallback_to_text_modality",
                            attempt_number=second_attempt,
                        )

                        recovered_variant = OutboundCallRecorder(
                            OutboundRequestSnapshot(
                                api_family="openai.responses",
                                sdk_operation="client.responses.create",
                                base_url="https://example.com/v1",
                                resource_path="/responses",
                                route_resolution="sdk_declared",
                                streaming=True,
                                parameters={
                                    "model": "gpt-golden",
                                    "input": "golden-second-prompt",
                                    "reasoning": {"effort": "high"},
                                    "max_output_tokens": 512,
                                    "modalities": ["text"],
                                },
                                transformations=("remove_image_modality",),
                            )
                        )
                        third_attempt = recovered_variant.record_attempt()
                        response = SimpleNamespace(
                            status_code=200,
                            request_id="req-golden",
                        )
                        record_outbound_first_chunk(response)
                        recovered_variant.record_completed(
                            response,
                            attempt_number=third_attempt,
                        )

                    with service.start_span("tool.call", kind="tool") as tool:
                        with service.start_span("mcp.tool.call", kind="tool"):
                            mcp_recorder = OutboundCallRecorder(
                                OutboundRequestSnapshot(
                                    api_family="mcp.tools.call",
                                    sdk_operation="session.call_tool",
                                    base_url="https://mcp.example.com/mcp",
                                    resource_path="/mcp/tools/call",
                                    route_resolution="sdk_declared",
                                    parameters={
                                        "tool_name": "golden_search",
                                        "input": "golden-tool-argument",
                                    },
                                    input_summary={
                                        "mcp_server_name": "golden-server",
                                        "mcp_transport": "streamable_http",
                                    },
                                )
                            )
                            mcp_attempt = mcp_recorder.record_attempt()
                            mcp_recorder.record_completed(
                                SimpleNamespace(status_code=200),
                                attempt_number=mcp_attempt,
                            )

            with service.start_span("message.send", kind="delivery") as delivery:
                delivery.set_attributes(
                    adapter_method="GoldenEvent.send",
                    streaming=False,
                    component_count=1,
                    platform_message_id_hash="sha256:golden-message",
                )

            with agent_history_persist_context("internal_agent"):
                await manager.update_conversation(
                    "platform:FriendMessage:golden-session",
                    "golden-conversation-id",
                    history=[{"role": "assistant", "content": "golden-history-body"}],
                    token_usage=7,
                )

        await service.flush()

        traces = await service.store.list_traces()
        assert len(traces) == 1
        detail = await service.store.get_trace(traces[0]["trace_id"])
        spans_by_operation: dict[str, list[dict]] = {}
        for span in detail["spans"]:
            spans_by_operation.setdefault(span["operation"], []).append(span)

        assert len(spans_by_operation["model.call"]) == 1
        model_row = spans_by_operation["model.call"][0]
        assert model_row["parent_span_id"] == step.span_id
        assert (
            model_row["attributes"]
            | {
                "api_family": "openai.responses",
                "base_url": "https://example.com/v1",
                "resource_path": "/responses",
                "reasoning_effort": "high",
                "attempt_count": 3,
                "retry_count": 1,
                "recovery_count": 1,
                "request_variant_count": 2,
                "status_code": 200,
                "remote_request_id": "req-golden",
            }
            == model_row["attributes"]
        )
        model_events = [
            event["name"]
            for event in detail["events"]
            if event["span_id"] == model.span_id
        ]
        assert model_events == [
            "outbound.request.prepared",
            "outbound.request.attempt",
            "outbound.request.retry",
            "outbound.request.attempt",
            "outbound.request.recovered",
            "outbound.request.prepared",
            "outbound.request.attempt",
            "outbound.response.first_chunk",
            "outbound.request.completed",
        ]
        assert spans_by_operation["agent.run"][0]["parent_span_id"] == root.span_id
        assert spans_by_operation["agent.step"][0]["parent_span_id"] == agent.span_id
        assert spans_by_operation["tool.call"][0]["parent_span_id"] == step.span_id
        assert spans_by_operation["mcp.tool.call"][0]["parent_span_id"] == tool.span_id
        assert spans_by_operation["message.send"][0]["parent_span_id"] == root.span_id
        assert (
            spans_by_operation["conversation.history.persist"][0]["parent_span_id"]
            == root.span_id
        )
        assert (
            len(
                [
                    ref
                    for ref in detail["artifact_refs"]
                    if ref["span_id"] == model.span_id
                    and ref["role"] == "outbound.effective_request"
                ]
            )
            == 2
        )

        persisted = trace_root.joinpath("trace.db").read_bytes()
        wal_path = trace_root / "trace.db-wal"
        if wal_path.is_file():
            persisted += wal_path.read_bytes()
        for object_file in trace_root.joinpath("objects").rglob("*"):
            if object_file.is_file():
                persisted += object_file.read_bytes()
        for value in secret_values:
            assert value.encode() not in persisted
    finally:
        await service.close()
