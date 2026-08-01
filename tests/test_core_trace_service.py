"""Focused unit tests for the Core trace runtime before instrumentation lands."""

from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

from astrbot.core.trace import storage as trace_storage
from astrbot.core.trace.models import MAX_ARTIFACT_LOGICAL_BYTES
from astrbot.core.trace.service import (
    NoopTraceSpan,
    PluginSpan,
    PluginTracer,
    TraceService,
)


@pytest.mark.asyncio
async def test_lazy_trace_materializes_only_for_a_real_child_operation(tmp_path):
    """A message skeleton should reach SQLite only after an operation starts."""

    service = TraceService(tmp_path / "trace")
    await service.initialize()
    try:
        with service.start_lazy_trace("message.process") as root:
            root.record_text("message.received", "hello")
            assert await service.store.list_traces() == []
            with service.start_span("agent.run", kind="agent") as agent:
                agent.add_event("agent.started", step_index=1)
                agent.record_json("request_manifest", {"messages": ["hello"]})
        await service.flush()

        traces = await service.store.list_traces()
        assert len(traces) == 1
        assert traces[0]["operation"] == "message.process"
        assert traces[0]["status"] == "success"
        detail = await service.store.get_trace(traces[0]["trace_id"])
        assert {span["operation"] for span in detail["spans"]} == {
            "message.process",
            "agent.run",
        }
        assert len(detail["artifact_refs"]) == 2
        assert {event["name"] for event in detail["events"]} == {"agent.started"}
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_lazy_trace_without_trigger_is_discarded(tmp_path):
    """A non-triggering message must not become a durable trace."""

    service = TraceService(tmp_path / "trace")
    await service.initialize()
    try:
        with service.start_lazy_trace("message.process") as root:
            root.add_event("routing.completed", wake=False)
            root.record_text("message.received", "help")
        await service.flush()
        assert await service.store.list_traces() == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_automatic_plugin_skeleton_waits_for_a_later_trigger(tmp_path):
    """A normal handler remains in memory until Agent or Provider work starts."""

    service = TraceService(tmp_path / "trace")
    await service.initialize()
    try:
        with service.start_lazy_trace("message.process"):
            with service.start_span(
                "plugin.handler",
                kind="plugin",
                materialize=False,
            ) as handler:
                handler.add_event("plugin.handler.completed", yield_count=1)
            assert await service.store.list_traces() == []
            with service.start_span("agent.run", kind="agent"):
                pass
        await service.flush()

        trace = (await service.store.list_traces())[0]
        detail = await service.store.get_trace(trace["trace_id"])
        assert {span["operation"] for span in detail["spans"]} == {
            "message.process",
            "plugin.handler",
            "agent.run",
        }
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_lazy_exception_materializes_a_diagnostic_trace(tmp_path):
    """An exception must materialize a lazy trace even without an Agent call."""

    service = TraceService(tmp_path / "trace")
    await service.initialize()
    try:
        with pytest.raises(RuntimeError, match="diagnostic"):
            with service.start_lazy_trace("message.process"):
                raise RuntimeError("diagnostic")
        await service.flush()

        trace = (await service.store.list_traces())[0]
        assert trace["status"] == "error"
        assert trace["outcome"] == "exception"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_explicit_plugin_root_binds_plugin_identity_and_context(tmp_path):
    """An explicit plugin root owns its context and may create child spans."""

    service = TraceService(tmp_path / "trace")
    await service.initialize()
    tracer = PluginTracer(service, "a1in/group-summary")
    try:
        with tracer.start_root("group_summary.run") as root:
            assert root.trace_id == tracer.trace_id
            with tracer.start_span("group_summary.messages.load") as load:
                load.record_json("group_summary.source_messages", [{"id": 1}])
        await service.flush()

        trace = (await service.store.list_traces())[0]
        assert trace["source"] == "plugin"
        assert trace["plugin_id"] == "a1in/group-summary"
        detail = await service.store.get_trace(trace["trace_id"])
        child = next(
            span
            for span in detail["spans"]
            if span["operation"] == "group_summary.messages.load"
        )
        assert child["plugin_id"] == "a1in/group-summary"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_artifact_content_is_deduplicated_by_canonical_content(tmp_path):
    """Repeated text references should use one immutable artifact object."""

    service = TraceService(tmp_path / "trace")
    await service.initialize()
    try:
        with service.start_root("model.call", kind="model") as root:
            first = root.record_text("system_prompt", "same content")
            second = root.record_text("user_prompt", "same content")
        await service.flush()

        assert first == second
        trace = (await service.store.list_traces())[0]
        detail = await service.store.get_trace(trace["trace_id"])
        assert len(detail["artifact_refs"]) == 2
        body, metadata = await service.store.get_artifact_body(first)
        assert body == b"same content"
        assert metadata["artifact_status"] == "available"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_truncated_artifact_uses_the_hash_of_the_captured_cas_body(tmp_path):
    """A truncated ArtifactRef must never point at a differently hashed body."""

    service = TraceService(tmp_path / "trace")
    await service.initialize()
    try:
        original = "x" * (MAX_ARTIFACT_LOGICAL_BYTES + 1)
        with service.start_root("model.call", kind="model") as root:
            artifact_hash = root.record_text("provider.response", original)
        await service.flush()

        assert artifact_hash is not None
        trace = (await service.store.list_traces())[0]
        detail = await service.store.get_trace(trace["trace_id"])
        ref = detail["artifact_refs"][0]
        body, metadata = await service.store.get_artifact_body(artifact_hash)
        assert hashlib.sha256(body).hexdigest() == artifact_hash
        assert metadata["truncated"] is True
        assert (
            ref["metadata"]["logical_content_hash"]
            == hashlib.sha256(original.encode("utf-8")).hexdigest()
        )
        assert json.loads(body)["truncated"] is True
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_artifact_serialization_is_fail_open_for_invalid_unicode(tmp_path):
    """A malformed text Artifact must not escape into the business path."""

    service = TraceService(tmp_path / "trace")
    await service.initialize()
    try:
        with service.start_root("model.call", kind="model") as root:
            assert root.record_text("provider.response", "bad\ud800text") is None
        await service.flush()

        trace = (await service.store.list_traces())[0]
        assert trace["status"] == "success"
        assert trace["degraded"] is True
        assert "artifact_serialization_failed" in trace["degradation_reasons"]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_terminal_trace_update_survives_a_full_critical_queue(tmp_path):
    """Deferred terminal updates use their own unbounded queue after prior work."""

    service = TraceService(tmp_path / "trace")
    await service.initialize()
    try:
        writer = service._writer_task
        assert writer is not None
        writer.cancel()
        await asyncio.gather(writer, return_exceptions=True)
        service._writer_task = None
        service._critical_queue = asyncio.Queue(maxsize=1)

        root = service.start_root("model.call", kind="model")
        root.finish()
        assert service._deferred_terminals

        accepted_before_terminal = service._critical_queue.get_nowait()
        service._mark_command_complete(accepted_before_terminal)
        assert not service._terminal_queue.empty()
        terminal = service._terminal_queue.get_nowait()
        assert terminal.command.action == "trace_patch"
        assert terminal.command.payload["status"] == "success"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_failed_detail_batch_isolated_from_orphan_trace_artifacts(tmp_path):
    """One queue-dropped Trace must not roll back another Trace's ArtifactRef."""

    service = TraceService(tmp_path / "trace")
    await service.initialize()
    try:
        writer = service._writer_task
        assert writer is not None
        writer.cancel()
        await asyncio.gather(writer, return_exceptions=True)
        service._writer_task = None
        service._critical_queue = asyncio.Queue(maxsize=4)

        with service.start_root("valid.root") as valid:
            valid.record_text("payload", "valid")
        with service.start_root("dropped.root") as dropped:
            dropped.record_text("payload", "orphan")

        # The first root consumes the four structural commands.  The second
        # root's structural writes are rejected, but its detail ArtifactRef is
        # still present beside the valid ArtifactRef in the detail batch.
        assert service._detail_queue.qsize() == 2
        service._writer_task = asyncio.create_task(service._writer_loop())
        await service.flush()

        traces = await service.store.list_traces(limit=10)
        assert len(traces) == 1
        assert traces[0]["operation"] == "valid.root"
        assert traces[0]["status"] == "success"
        assert traces[0]["degraded"] is False
        detail = await service.store.get_trace(traces[0]["trace_id"])
        assert len(detail["artifact_refs"]) == 1
        assert detail["artifact_refs"][0]["role"] == "payload"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_plugin_span_without_context_is_a_noop(tmp_path):
    """Plugins must explicitly start a root when they have no active trace."""

    service = TraceService(tmp_path / "trace")
    await service.initialize()
    try:
        tracer = PluginTracer(service, "a1in/example")
        span = tracer.start_span("example.work")
        assert isinstance(span, PluginSpan)
        assert span.trace_id is None
        tracer.record_text("ignored", "not persisted")
        await service.flush()
        assert await service.store.list_traces() == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_disabled_trace_is_fail_open(tmp_path):
    """Disabling trace creation must leave plugin calls safe and non-persistent."""

    service = TraceService(tmp_path / "trace", enabled=False)
    await service.initialize()
    try:
        assert isinstance(service.start_root("group_summary.run"), NoopTraceSpan)
        span = PluginTracer(service, "a1in/group-summary").start_root("x")
        assert isinstance(span, PluginSpan)
        assert span.trace_id is None
        await service.flush()
        assert await service.store.list_traces() == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_disabling_stops_new_roots_but_finishes_existing_trace(tmp_path):
    """The dashboard toggle must not strand a Trace already in progress."""

    service = TraceService(tmp_path / "trace")
    await service.initialize()
    try:
        with service.start_root("agent.run") as root:
            service.set_enabled(False)
            with service.start_span("agent.step") as step:
                step.set_attributes(step_index=1)

        assert isinstance(service.start_root("agent.run"), NoopTraceSpan)
        await service.flush()

        traces = await service.store.list_traces()
        assert len(traces) == 1
        detail = await service.store.get_trace(root.trace_id)
        assert detail["trace"]["status"] == "success"
        assert any(span["operation"] == "agent.step" for span in detail["spans"])
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_plugin_cannot_impersonate_core_operations(tmp_path):
    """Plugin business spans cannot use reserved Core automatic operation names."""

    service = TraceService(tmp_path / "trace")
    await service.initialize()
    try:
        tracer = PluginTracer(service, "a1in/example")
        with service.start_root("message.process"):
            span = tracer.start_span("model.call")
            assert span.trace_id is None
        root = tracer.start_root("mcp.tool.call")
        assert root.trace_id is None
        await service.flush()
        assert [trace["operation"] for trace in await service.store.list_traces()] == [
            "message.process"
        ]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_trace_list_summaries_filter_roots_and_preserve_keyset_pagination(
    tmp_path,
):
    """List summaries expose per-Trace counts without loading full details."""

    service = TraceService(tmp_path / "trace")
    await service.initialize()
    try:
        plugin_tracer = PluginTracer(service, "a1in/group-summary")
        with plugin_tracer.start_root("group_summary.run") as root:
            root.mark_degraded("partial_source")
            root.add_event("summary.started")
            root.record_text("summary.prompt", "summarize these messages")
            root.make_link("caused_by", target_trace_id="upstream-trace")
            with plugin_tracer.start_span("group_summary.messages.load") as load:
                load.add_event("messages.loaded", count=2)
                load.record_json("summary.messages", [{"id": 1}, {"id": 2}])
                load.make_link("references", target_span_id="upstream-span")

        with service.start_root("core.complete", kind="agent"):
            pass

        running_root = service.start_root("core.running", kind="agent")
        with running_root.activate():
            pending_span = service.start_span("core.pending", kind="tool")
            await asyncio.sleep(0.01)
            active_span = service.start_span("core.current", kind="model")
        await service.flush()

        plugin_summaries = await service.store.list_traces(
            limit=10,
            source="plugin",
            kind="business",
            plugin_id="a1in/group-summary",
            status="success",
            degraded=True,
        )
        assert len(plugin_summaries) == 1
        plugin_summary = plugin_summaries[0]
        assert plugin_summary["operation"] == "group_summary.run"
        assert plugin_summary["span_count"] == 2
        assert plugin_summary["event_count"] == 2
        assert plugin_summary["artifact_count"] == 2
        assert plugin_summary["link_count"] == 2
        assert plugin_summary["active_span_operation"] is None

        core_terminal = await service.store.list_traces(
            limit=10,
            source="core",
            kind="agent",
            status="success",
            degraded=False,
        )
        assert [summary["operation"] for summary in core_terminal] == ["core.complete"]

        running = await service.store.list_traces(
            limit=10,
            source="core",
            kind="agent",
            status="running",
            degraded=False,
        )
        assert [summary["operation"] for summary in running] == ["core.running"]
        assert running[0]["active_span_operation"] == active_span.operation

        all_summaries = await service.store.list_traces(limit=10)
        first_page = await service.store.list_traces(limit=2)
        cursor = first_page[-1]
        second_page = await service.store.list_traces(
            limit=10,
            before_ended_at=cursor["ended_at"] or cursor["started_at"],
            before_trace_id=cursor["trace_id"],
        )
        assert [summary["trace_id"] for summary in first_page + second_page] == [
            summary["trace_id"] for summary in all_summaries
        ]

        active_span.finish()
        pending_span.finish()
        running_root.finish()
        await service.flush()
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_trace_store_creates_expression_index_for_list_order(tmp_path):
    """The newest-first keyset order must remain indexable for polling."""

    service = TraceService(tmp_path / "trace")
    await service.initialize()
    try:
        db = service.store._require_read_db()
        async with db.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
            ("traces_list_order_idx",),
        ) as cursor:
            row = await cursor.fetchone()

        assert row is not None
        assert "COALESCE(ended_at, started_at) DESC, trace_id DESC" in row[0]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_capacity_cleanup_compacts_in_bounded_batches(tmp_path, monkeypatch):
    """Capacity retention must compact before deciding to delete more history.

    Inline artifacts deliberately keep all data in SQLite/WAL.  Before the
    checkpoint/VACUUM boundary, their physical size does not decrease for each
    DELETE, so this regression test would previously delete every terminal
    trace and then fail with ``SQL statements in progress`` at VACUUM.
    """

    monkeypatch.setattr(trace_storage, "_CAPACITY_CLEANUP_BATCH_SIZE", 2)
    service = TraceService(tmp_path / "trace")
    await service.initialize()
    try:
        for index in range(3):
            with service.start_root("test.root") as root:
                root.record_text("payload", "x" * 900 + str(index))
        await service.flush()

        # Stabilize the WAL first so the one-byte threshold below reflects a
        # real capacity overage rather than reclaimable pre-existing WAL pages.
        assert await service.store._checkpoint_and_vacuum()
        before = await service.store.physical_size()
        result = await service.store.cleanup(
            retention_days=30,
            max_bytes=before - 1,
            target_bytes=before - 1,
        )

        remaining = await service.store.list_traces(limit=10)
        assert result["deleted"] == 1
        assert len(remaining) == 2
        assert {trace["status"] for trace in remaining} == {"success"}
        assert result["physical_size"] <= before
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_capacity_cleanup_preserves_history_while_read_snapshot_is_active(
    tmp_path,
):
    """A WebUI read snapshot must block capacity deletion, not trigger it."""

    service = TraceService(tmp_path / "trace")
    await service.initialize()
    try:
        for index in range(3):
            with service.start_root("test.root") as root:
                root.record_text("payload", "x" * 900 + str(index))
        await service.flush()

        before = await service.store.physical_size()
        read_db = service.store._read_db
        assert read_db is not None
        begin_cursor = await read_db.execute("BEGIN")
        await begin_cursor.close()
        snapshot_cursor = await read_db.execute("SELECT * FROM traces")
        try:
            assert await snapshot_cursor.fetchone() is not None
        finally:
            await snapshot_cursor.close()

        try:
            result = await service.store.cleanup(
                retention_days=30,
                max_bytes=before - 1,
                target_bytes=before - 1,
            )
        finally:
            await read_db.rollback()

        assert result["deleted"] == 0
        assert len(await service.store.list_traces(limit=10)) == 3
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_capacity_cleanup_does_not_delete_after_checkpoint_resolves_overage(
    tmp_path,
):
    """Reclaimable WAL alone must not trigger target-hysteresis deletion."""

    service = TraceService(tmp_path / "trace")
    await service.initialize()
    try:
        for index in range(3):
            with service.start_root("test.root") as root:
                root.record_text("payload", "x" * 900 + str(index))
        await service.flush()

        before = await service.store.physical_size()
        result = await service.store.cleanup(
            retention_days=30,
            max_bytes=before - 1,
            target_bytes=0,
        )

        assert result["deleted"] == 0
        assert len(await service.store.list_traces(limit=10)) == 3
    finally:
        await service.close()
