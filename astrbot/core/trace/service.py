"""Fail-open execution tracing runtime and plugin-facing span handles."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from collections.abc import Iterator
from contextvars import Token
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .context import (
    SpanState,
    TraceState,
    _current_span_id,
    _current_trace_state,
    _trace_suppressed,
    current_span_id,
    current_trace_state,
    trace_suppressed,
)
from .models import (
    MAX_ARTIFACT_LOGICAL_BYTES,
    MAX_ATTRIBUTES_BYTES_PER_SPAN,
    MAX_ATTRIBUTES_PER_SPAN,
    MAX_EVENTS_PER_SPAN,
    MAX_EVENTS_PER_TRACE,
    MAX_LINKS_PER_TRACE,
    MAX_SPANS_PER_TRACE,
    MAX_TRACE_CAPTURED_BYTES,
    TRACE_STATUS_CANCELLED,
    TRACE_STATUS_ERROR,
    TRACE_STATUS_SUCCESS,
    TRACE_STATUSES,
    canonical_json,
    content_hash,
    new_span_id,
    new_trace_id,
)
from .storage import StoreCommand, TraceStore

_CRITICAL_QUEUE_SIZE = 256
_DETAIL_QUEUE_SIZE = 2048
_WRITER_BATCH_SIZE = 128
_MAX_CRITICAL_BATCHES_BEFORE_DETAIL = 8
_MAX_DEGRADATION_REASONS = 32
_CLEANUP_INTERVAL_SECONDS = 5 * 60
_LIFECYCLE_IO_TIMEOUT_SECONDS = 10
_PLUGIN_OPERATION_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
_RESERVED_PLUGIN_OPERATIONS = frozenset(
    {
        "message.process",
        "message.send",
        "agent.run",
        "agent.step",
        "model.call",
        "tool.call",
        "mcp.tool.call",
        "skill.load",
        "stt.pipeline",
        "stt.call",
        "tts.pipeline",
        "tts.call",
        "embedding.call",
        "rerank.call",
        "plugin.handler",
        "plugin.hook",
        "response.deliver",
        "conversation.history.persist",
    }
)


@dataclass
class _QueuedCommand:
    """A command accepted by the runtime writer queues."""

    sequence: int
    trace_id: str
    command: StoreCommand
    detail: bool
    state: TraceState
    terminal: bool = False


class TraceService:
    """Own Core trace contexts, lifecycle, bounded writes, and durable storage.

    The service intentionally accepts writes synchronously from business code and
    sends them to a bounded background writer.  A full queue or storage error
    only degrades the current trace; it must never block the caller's business
    path.
    """

    def __init__(
        self,
        root: Path,
        *,
        enabled: bool = True,
        retention_days: int = 30,
        max_storage_bytes: int = 2 * 1024 * 1024 * 1024,
        cleanup_target_bytes: int = int(1.6 * 1024 * 1024 * 1024),
    ) -> None:
        self.store = TraceStore(root)
        self.enabled = enabled
        self.retention_days = retention_days
        self.max_storage_bytes = max_storage_bytes
        self.cleanup_target_bytes = cleanup_target_bytes
        self._critical_queue: asyncio.Queue[_QueuedCommand] = asyncio.Queue(
            maxsize=_CRITICAL_QUEUE_SIZE
        )
        self._detail_queue: asyncio.Queue[_QueuedCommand] = asyncio.Queue(
            maxsize=_DETAIL_QUEUE_SIZE
        )
        # Terminal root updates must never be lost merely because the bounded
        # detail/critical queues are saturated. They are released only after
        # all accepted earlier commands for the same trace have completed.
        self._terminal_queue: asyncio.Queue[_QueuedCommand] = asyncio.Queue()
        self._wake = asyncio.Event()
        self._writer_task: asyncio.Task[None] | None = None
        self._accepting = False
        self._stopping = False
        self._next_sequence = 0
        self._outstanding_sequences: set[int] = set()
        self._pending_by_trace: dict[str, int] = {}
        self._deferred_terminals: dict[str, _QueuedCommand] = {}
        self._flush_waiters: list[tuple[int, asyncio.Future[None]]] = []
        self.last_writer_error: str | None = None
        self._last_cleanup_at = 0.0
        self._critical_batches_since_detail = 0

    async def initialize(self) -> None:
        """Initialize persistent storage without making startup depend on it."""

        if self._writer_task is not None:
            return
        try:
            await asyncio.wait_for(
                self._initialize_store(),
                timeout=_LIFECYCLE_IO_TIMEOUT_SECONDS,
            )
        except Exception:
            self.enabled = False
            try:
                await asyncio.wait_for(
                    self.store.close(),
                    timeout=_LIFECYCLE_IO_TIMEOUT_SECONDS,
                )
            except Exception:
                pass
            return
        self._accepting = True
        self._stopping = False
        self._writer_task = asyncio.create_task(
            self._writer_loop(),
            name="astrbot:trace-writer",
        )

    async def close(self) -> None:
        """Stop accepting writes, flush accepted work, and close the store."""

        self._accepting = False
        try:
            await asyncio.wait_for(
                self.flush(),
                timeout=_LIFECYCLE_IO_TIMEOUT_SECONDS,
            )
        except Exception:
            pass
        self._stopping = True
        self._wake.set()
        writer_task, self._writer_task = self._writer_task, None
        if writer_task is not None:
            try:
                await asyncio.wait_for(writer_task, timeout=10)
            except asyncio.TimeoutError:
                writer_task.cancel()
                await asyncio.gather(writer_task, return_exceptions=True)
        try:
            await self.store.close()
        except Exception:
            pass

    async def flush(self) -> None:
        """Wait until every command accepted before this call is processed."""

        if self._writer_task is None:
            return
        target = self._next_sequence
        if not any(sequence <= target for sequence in self._outstanding_sequences):
            return
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._flush_waiters.append((target, future))
        self._wake.set()
        await future

    def set_enabled(self, enabled: bool) -> None:
        """Enable or disable capture for traces that have not started yet."""

        self.enabled = enabled

    async def cleanup(self) -> dict[str, int]:
        """Apply retention and capacity cleanup through the owned Trace store."""

        if self._writer_task is None:
            return {"deleted": 0, "before_capacity_cleanup": 0, "physical_size": 0}
        try:
            result = await self.store.cleanup(
                retention_days=self.retention_days,
                max_bytes=self.max_storage_bytes,
                target_bytes=self.cleanup_target_bytes,
            )
        except Exception as exc:  # noqa: BLE001 - cleanup must remain fail-open
            self.last_writer_error = type(exc).__name__
            return {"deleted": 0, "before_capacity_cleanup": 0, "physical_size": 0}
        self._last_cleanup_at = time.monotonic()
        return result

    async def _initialize_store(self) -> None:
        """Run startup-only Trace maintenance before accepting business writes."""

        await self.store.initialize()
        await self.store.clean_stale_tmp()
        await self.store.clean_orphan_objects()
        await self.store.mark_running_incomplete()
        await self.store.cleanup(
            retention_days=self.retention_days,
            max_bytes=self.max_storage_bytes,
            target_bytes=self.cleanup_target_bytes,
        )

    @contextlib.contextmanager
    def suppress(self) -> Iterator[None]:
        """Suppress automatic trace creation in the current context.

        This is used for provider configuration tests and health probes.  It is
        intentionally explicit instead of relying on method names or prompts.
        """

        token = _trace_suppressed.set(True)
        try:
            yield
        finally:
            _trace_suppressed.reset(token)

    def start_lazy_trace(
        self,
        operation: str,
        *,
        kind: str = "pipeline",
        attributes: dict[str, Any] | None = None,
        source: str = "core",
        plugin_id: str | None = None,
    ) -> LazyTrace | NoopTraceScope:
        """Create a message-like root that persists only after materialization."""

        if not self._can_create_trace():
            return NoopTraceScope()
        return self._create_root(
            operation,
            kind=kind,
            attributes=attributes,
            source=source,
            plugin_id=plugin_id,
            lazy=True,
        )

    def start_root(
        self,
        operation: str,
        *,
        kind: str = "business",
        attributes: dict[str, Any] | None = None,
        source: str = "core",
        plugin_id: str | None = None,
    ) -> TraceSpan | NoopTraceSpan:
        """Create an explicit root trace regardless of the active parent context."""

        if not self._can_create_trace():
            return NoopTraceSpan()
        return self._create_root(
            operation,
            kind=kind,
            attributes=attributes,
            source=source,
            plugin_id=plugin_id,
            lazy=False,
        ).root_span

    def start_span(
        self,
        operation: str,
        *,
        kind: str = "business",
        attributes: dict[str, Any] | None = None,
        source: str = "core",
        plugin_id: str | None = None,
        materialize: bool = True,
    ) -> TraceSpan | NoopTraceSpan:
        """Start a child span inside the active trace, or return a no-op span.

        A normal span never creates an implicit root.  That distinction lets
        plugins choose explicitly between joining an active message trace and
        creating independent business work through ``start_root``.
        """

        state = current_trace_state()
        if state is None or state.terminal or not self._can_record(state):
            return NoopTraceSpan()
        return self.start_span_with_parent(
            state,
            current_span_id(),
            operation,
            kind=kind,
            attributes=attributes,
            source=source,
            plugin_id=plugin_id,
            materialize=materialize,
        )

    def start_span_with_parent(
        self,
        state: TraceState,
        parent_span_id: str | None,
        operation: str,
        *,
        kind: str = "business",
        attributes: dict[str, Any] | None = None,
        source: str = "core",
        plugin_id: str | None = None,
        materialize: bool = True,
    ) -> TraceSpan | NoopTraceSpan:
        """Start a span under an explicitly captured in-process parent.

        This private-Core primitive supports known async-generator handoffs such
        as Agent runners.  It never serializes or propagates IDs outside this
        process.
        """

        if state.terminal or not self._can_record(state):
            return NoopTraceSpan()
        if materialize:
            self.materialize(state)
        if state.terminal:
            return NoopTraceSpan()
        if len(state.spans) >= MAX_SPANS_PER_TRACE:
            self._drop(state, "span_limit_exceeded")
            return NoopTraceSpan()
        if parent_span_id not in state.spans:
            parent_span_id = state.root_span_id
        span_id = new_span_id()
        span_state = SpanState(
            span_id=span_id,
            parent_span_id=parent_span_id,
            operation=operation,
            kind=kind,
            source=source,
            plugin_id=plugin_id,
            started_at=time.time(),
        )
        self._merge_attributes(span_state, attributes or {})
        state.spans[span_id] = span_state
        span = TraceSpan(self, state, span_state)
        self._emit(
            state,
            StoreCommand("span_create", self._span_payload(state, span_state)),
            detail=False,
        )
        self._touch(state)
        return span

    def materialize(self, state: TraceState | None = None) -> bool:
        """Persist a lazy trace and all of its previously captured skeleton data."""

        state = state or current_trace_state()
        if state is None or state.terminal or state.materialized:
            return bool(state and state.materialized)
        if not self._can_record(state):
            return False
        state.materialized = True
        self._enqueue(
            state,
            StoreCommand("trace_create", self._trace_payload(state)),
            detail=False,
        )
        root_span = state.spans[state.root_span_id]
        self._enqueue(
            state,
            StoreCommand("span_create", self._span_payload(state, root_span)),
            detail=False,
        )
        pending_commands, state.pending_commands = state.pending_commands, []
        for command, detail in pending_commands:
            self._enqueue(state, command, detail=detail)
        return True

    def current_ids(self) -> tuple[str | None, str | None]:
        """Return the active trace and span identifiers for plugin diagnostics."""

        state = current_trace_state()
        return (state.trace_id if state else None, current_span_id())

    def current_span(self) -> TraceSpan | NoopTraceSpan:
        """Return a handle for the active span without creating new structure."""

        state = current_trace_state()
        span_id = current_span_id()
        if (
            state is None
            or state.service is not self
            or span_id is None
            or span_id not in state.spans
        ):
            return NoopTraceSpan()
        return TraceSpan(self, state, state.spans[span_id])

    def get_plugin_tracer(self, plugin_id: str | None) -> PluginTracer:
        """Return a plugin-bound tracer without exposing storage internals."""

        return PluginTracer(self, plugin_id)

    def _create_root(
        self,
        operation: str,
        *,
        kind: str,
        attributes: dict[str, Any] | None,
        source: str,
        plugin_id: str | None,
        lazy: bool,
    ) -> LazyTrace:
        now = time.time()
        trace_id = new_trace_id()
        root_span_id = new_span_id()
        root_span = SpanState(
            span_id=root_span_id,
            parent_span_id=None,
            operation=operation,
            kind=kind,
            source=source,
            plugin_id=plugin_id,
            started_at=now,
        )
        state = TraceState(
            trace_id=trace_id,
            service=self,
            root_span_id=root_span_id,
            operation=operation,
            kind=kind,
            source=source,
            plugin_id=plugin_id,
            started_at=now,
            lazy=lazy,
            materialized=not lazy,
            spans={root_span_id: root_span},
        )
        self._merge_attributes(state, attributes or {})
        self._merge_attributes(root_span, attributes or {})
        root = TraceSpan(self, state, root_span, root=True)
        scope = LazyTrace(self, state, root)
        if not lazy:
            self._emit(
                state,
                StoreCommand("trace_create", self._trace_payload(state)),
                detail=False,
            )
            self._emit(
                state,
                StoreCommand("span_create", self._span_payload(state, root_span)),
                detail=False,
            )
        return scope

    def _can_create_trace(self) -> bool:
        return self.enabled and self._accepting and not trace_suppressed()

    def _can_record(self, state: TraceState) -> bool:
        return self._accepting and state.service is self and not state.terminal

    def _emit(self, state: TraceState, command: StoreCommand, *, detail: bool) -> bool:
        if state.lazy and not state.materialized:
            state.pending_commands.append((command, detail))
            return True
        return self._enqueue(state, command, detail=detail)

    def _enqueue(
        self, state: TraceState, command: StoreCommand, *, detail: bool
    ) -> bool:
        if not self._accepting:
            return False
        if state.terminal and command.action != "trace_patch":
            return False
        self._next_sequence += 1
        queued = _QueuedCommand(
            sequence=self._next_sequence,
            trace_id=state.trace_id,
            command=command,
            detail=detail,
            state=state,
            terminal=command.action == "trace_patch"
            and command.payload.get("ended_at") is not None,
        )
        if queued.terminal:
            if self._pending_by_trace.get(state.trace_id, 0) > 0:
                self._outstanding_sequences.add(queued.sequence)
                self._deferred_terminals[state.trace_id] = queued
                self._wake.set()
                return True
            self._terminal_queue.put_nowait(queued)
            self._outstanding_sequences.add(queued.sequence)
            self._wake.set()
            return True
        queue = self._detail_queue if detail else self._critical_queue
        try:
            queue.put_nowait(queued)
        except asyncio.QueueFull:
            self._drop(state, "writer_queue_full")
            return False
        self._outstanding_sequences.add(queued.sequence)
        if not queued.terminal:
            self._pending_by_trace[state.trace_id] = (
                self._pending_by_trace.get(state.trace_id, 0) + 1
            )
        self._wake.set()
        return True

    async def _writer_loop(self) -> None:
        while True:
            command = self._dequeue_nowait()
            if command is None:
                if self._stopping and not self._outstanding_sequences:
                    return
                self._wake.clear()
                if self._has_pending_commands():
                    self._wake.set()
                    continue
                await self._wake.wait()
                continue
            batch = [command]
            while len(batch) < _WRITER_BATCH_SIZE:
                following = self._dequeue_nowait(detail=command.detail)
                if following is None:
                    break
                batch.append(following)
            try:
                await self.store.apply_batch([item.command for item in batch])
            except Exception as exc:  # noqa: BLE001 - tracing must fail open
                self.last_writer_error = type(exc).__name__
                if command.detail and len(batch) > 1:
                    if await self._isolate_failed_detail_batch(batch):
                        await self._maybe_cleanup()
                else:
                    for item in batch:
                        self._mark_command_failed(item)
            else:
                for item in batch:
                    self._mark_command_complete(item)
                await self._maybe_cleanup()
            if command.detail:
                self._critical_batches_since_detail = 0
            elif not command.terminal:
                self._critical_batches_since_detail += 1
            self._resolve_flush_waiters()

    async def _isolate_failed_detail_batch(self, batch: list[_QueuedCommand]) -> bool:
        """Retry a failed detail batch command-by-command.

        Detail writes are intentionally batched for throughput, but one rejected
        ArtifactRef (for example after its trace's bounded structural queue was
        dropped) must not roll back every valid ArtifactRef that happened to be
        in the same SQLite transaction.  This slower path runs only after the
        original batch failed; each accepted command receives the same terminal
        and flush bookkeeping as a normal successful batch.
        """

        any_success = False
        for item in batch:
            try:
                await self.store.apply_batch([item.command])
            except Exception as exc:  # noqa: BLE001 - tracing must fail open
                self.last_writer_error = type(exc).__name__
                self._mark_command_failed(item)
            else:
                any_success = True
                self._mark_command_complete(item)
        return any_success

    def _dequeue_nowait(self, *, detail: bool | None = None) -> _QueuedCommand | None:
        if detail is True:
            try:
                return self._detail_queue.get_nowait()
            except asyncio.QueueEmpty:
                return None
        if detail is False:
            try:
                return self._critical_queue.get_nowait()
            except asyncio.QueueEmpty:
                return None
        try:
            return self._terminal_queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        if self._critical_batches_since_detail >= _MAX_CRITICAL_BATCHES_BEFORE_DETAIL:
            try:
                return self._detail_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            return self._critical_queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        try:
            return self._detail_queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    def _has_pending_commands(self) -> bool:
        return (
            not self._critical_queue.empty()
            or not self._detail_queue.empty()
            or not self._terminal_queue.empty()
            or bool(self._deferred_terminals)
        )

    def _mark_command_complete(self, queued: _QueuedCommand) -> None:
        self._outstanding_sequences.discard(queued.sequence)
        if not queued.terminal:
            self._release_pending(queued.trace_id)

    def _mark_command_failed(self, queued: _QueuedCommand) -> None:
        self._mark_degraded(queued.state, "writer_failed")
        self._outstanding_sequences.discard(queued.sequence)
        if not queued.terminal:
            self._release_pending(queued.trace_id)

    def _release_pending(self, trace_id: str) -> None:
        remaining = self._pending_by_trace.get(trace_id, 0) - 1
        if remaining > 0:
            self._pending_by_trace[trace_id] = remaining
            return
        self._pending_by_trace.pop(trace_id, None)
        terminal = self._deferred_terminals.pop(trace_id, None)
        if terminal is None:
            return
        if terminal.command.action == "trace_patch":
            terminal.command = StoreCommand(
                "trace_patch",
                self._trace_payload(terminal.state),
            )
        self._terminal_queue.put_nowait(terminal)
        self._wake.set()

    def _resolve_flush_waiters(self) -> None:
        unresolved: list[tuple[int, asyncio.Future[None]]] = []
        for target, future in self._flush_waiters:
            if future.done():
                continue
            if not any(sequence <= target for sequence in self._outstanding_sequences):
                future.set_result(None)
            else:
                unresolved.append((target, future))
        self._flush_waiters = unresolved

    def _finish_span(
        self,
        state: TraceState,
        span_state: SpanState,
        *,
        status: str,
        outcome: str | None,
    ) -> None:
        if span_state.terminal:
            return
        if status not in TRACE_STATUSES:
            self._mark_degraded(state, "invalid_span_status")
            status = TRACE_STATUS_ERROR
            outcome = outcome or "invalid_status"
        span_state.status = status
        span_state.outcome = outcome
        span_state.ended_at = time.time()
        span_state.terminal = True
        self._emit(
            state,
            StoreCommand("span_patch", self._span_payload(state, span_state)),
            detail=False,
        )
        if span_state.span_id != state.root_span_id:
            self._touch(state)
            return
        state.status = status
        state.outcome = outcome
        state.ended_at = span_state.ended_at
        state.terminal = True
        if state.lazy and not state.materialized:
            return
        self._emit(
            state,
            StoreCommand("trace_patch", self._trace_payload(state)),
            detail=False,
        )

    def _add_event(
        self,
        state: TraceState,
        span_state: SpanState,
        name: str,
        attributes: dict[str, Any] | None,
    ) -> None:
        if span_state.terminal:
            return
        if state.event_count >= MAX_EVENTS_PER_TRACE:
            self._drop(state, "trace_event_limit_exceeded")
            return
        if span_state.event_count >= MAX_EVENTS_PER_SPAN:
            self._drop(state, "span_event_limit_exceeded")
            return
        state.event_count += 1
        span_state.event_count += 1
        self._emit(
            state,
            StoreCommand(
                "event",
                {
                    "trace_id": state.trace_id,
                    "span_id": span_state.span_id,
                    "event_index": span_state.event_count,
                    "name": name,
                    "occurred_at": time.time(),
                    "attributes": self._safe_attributes(attributes or {}),
                },
            ),
            detail=False,
        )
        self._touch(state)

    def _record_artifact(
        self,
        state: TraceState,
        span_state: SpanState,
        *,
        role: str,
        body: bytes,
        media_type: str,
        metadata: dict[str, Any] | None,
    ) -> str | None:
        if span_state.terminal:
            return None
        original_size = len(body)
        logical_hash = content_hash(body)
        truncated = original_size > MAX_ARTIFACT_LOGICAL_BYTES
        captured = body
        if truncated:
            captured = canonical_json(
                {
                    "truncated": True,
                    "logical_size": original_size,
                    "head": body[: 8 * 1024].decode("utf-8", errors="replace"),
                    "tail": body[-8 * 1024 :].decode("utf-8", errors="replace"),
                }
            )
            self._mark_degraded(state, "artifact_too_large")
        if logical_hash not in state.unique_artifact_hashes:
            if state.captured_artifact_bytes + original_size > MAX_TRACE_CAPTURED_BYTES:
                self._drop(state, "trace_capture_budget_exceeded")
                return None
        captured_hash = content_hash(captured)
        ref_index = span_state.artifact_ref_count + 1
        safe_metadata = self._safe_attributes(metadata or {})
        if truncated:
            safe_metadata["logical_content_hash"] = logical_hash
        accepted = self._emit(
            state,
            StoreCommand(
                "artifact_ref",
                {
                    "trace_id": state.trace_id,
                    "span_id": span_state.span_id,
                    "ref_index": ref_index,
                    "content_hash": captured_hash,
                    "media_type": media_type,
                    "logical_size": original_size,
                    "captured_size": len(captured),
                    "body": captured,
                    "truncated": truncated,
                    "role": role,
                    "metadata": safe_metadata,
                    "created_at": time.time(),
                },
            ),
            detail=True,
        )
        if not accepted:
            return None
        if logical_hash not in state.unique_artifact_hashes:
            state.unique_artifact_hashes.add(logical_hash)
            state.captured_artifact_bytes += original_size
        span_state.artifact_ref_count = ref_index
        self._touch(state)
        return captured_hash

    def _add_link(
        self,
        state: TraceState,
        span_state: SpanState,
        *,
        relation: str,
        target_trace_id: str | None,
        target_span_id: str | None,
        attributes: dict[str, Any] | None,
    ) -> None:
        if state.link_count >= MAX_LINKS_PER_TRACE:
            self._drop(state, "link_limit_exceeded")
            return
        state.link_count += 1
        self._emit(
            state,
            StoreCommand(
                "link",
                {
                    "trace_id": state.trace_id,
                    "span_id": span_state.span_id,
                    "link_index": state.link_count,
                    "relation": relation,
                    "target_trace_id": target_trace_id,
                    "target_span_id": target_span_id,
                    "attributes": self._safe_attributes(attributes or {}),
                },
            ),
            detail=False,
        )
        self._touch(state)

    def _mark_degraded(self, state: TraceState, reason: str) -> None:
        state.degraded = True
        if reason not in state.degradation_reasons:
            if len(state.degradation_reasons) < _MAX_DEGRADATION_REASONS:
                state.degradation_reasons.append(reason)
            else:
                self._drop(state, "degradation_reason_limit_exceeded")

    def _drop(self, state: TraceState, reason: str) -> None:
        state.dropped[reason] = state.dropped.get(reason, 0) + 1
        self._mark_degraded(state, reason)

    def _touch(self, state: TraceState) -> None:
        """Advance a running trace revision after one durable structure change."""

        if state.materialized and not state.terminal:
            self._emit(
                state,
                StoreCommand("trace_patch", self._trace_payload(state)),
                detail=False,
            )

    async def _maybe_cleanup(self) -> None:
        """Run bounded retention work periodically on the single writer task."""

        if self._stopping or not self._accepting:
            return
        if time.monotonic() - self._last_cleanup_at < _CLEANUP_INTERVAL_SECONDS:
            return
        await self.cleanup()

    def _merge_attributes(
        self, holder: TraceState | SpanState, values: dict[str, Any]
    ) -> None:
        if not values:
            return
        safe = self._safe_attributes(values)
        available = MAX_ATTRIBUTES_PER_SPAN - len(holder.attributes)
        for key, value in list(safe.items())[: max(0, available)]:
            holder.attributes[key] = value
        try:
            encoded = canonical_json(holder.attributes)
        except (TypeError, ValueError, UnicodeError):
            holder.attributes.clear()
            holder.attributes["attributes_truncated"] = True
            if isinstance(holder, TraceState):
                self._mark_degraded(holder, "attribute_serialization_failed")
            return
        if len(encoded) > MAX_ATTRIBUTES_BYTES_PER_SPAN:
            holder.attributes.clear()
            holder.attributes["attributes_truncated"] = True
            if isinstance(holder, TraceState):
                self._mark_degraded(holder, "attribute_size_limit_exceeded")

    def _safe_attributes(self, values: dict[str, Any]) -> dict[str, Any]:
        try:
            return json.loads(canonical_json(values))
        except (TypeError, ValueError, UnicodeError):
            return {"serialization_error": type(values).__name__}

    def _frozen_json_value(self, value: Any, default: Any) -> Any:
        try:
            return json.loads(canonical_json(value))
        except (TypeError, ValueError, UnicodeError):
            return default

    def _trace_payload(self, state: TraceState) -> dict[str, Any]:
        state.revision += 1
        return {
            "trace_id": state.trace_id,
            "root_span_id": state.root_span_id,
            "operation": state.operation,
            "kind": state.kind,
            "source": state.source,
            "plugin_id": state.plugin_id,
            "started_at": state.started_at,
            "ended_at": state.ended_at,
            "status": state.status,
            "outcome": state.outcome,
            "degraded": state.degraded,
            "degradation_reasons": self._frozen_json_value(
                state.degradation_reasons, []
            ),
            "attributes": self._frozen_json_value(state.attributes, {}),
            "revision": state.revision,
            "dropped": self._frozen_json_value(state.dropped, {}),
        }

    def _span_payload(self, state: TraceState, span: SpanState) -> dict[str, Any]:
        return {
            "trace_id": state.trace_id,
            "span_id": span.span_id,
            "parent_span_id": span.parent_span_id,
            "operation": span.operation,
            "kind": span.kind,
            "source": span.source,
            "plugin_id": span.plugin_id,
            "started_at": span.started_at,
            "ended_at": span.ended_at,
            "status": span.status,
            "outcome": span.outcome,
            "degraded": span.degraded,
            "degradation_reasons": self._frozen_json_value(
                span.degradation_reasons, []
            ),
            "attributes": self._frozen_json_value(span.attributes, {}),
        }


class LazyTrace:
    """Context manager for an explicit or lazy root span."""

    def __init__(
        self, service: TraceService, state: TraceState, root_span: TraceSpan
    ) -> None:
        self.service = service
        self.state = state
        self.root_span = root_span
        self._trace_token: Token[TraceState | None] | None = None
        self._span_token: Token[str | None] | None = None

    def __enter__(self) -> TraceSpan:
        self._trace_token = _current_trace_state.set(self.state)
        self._span_token = _current_span_id.set(self.state.root_span_id)
        return self.root_span

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: Any,
    ) -> None:
        if exc is not None and self.state.lazy and not self.state.materialized:
            self.service.materialize(self.state)
        self.root_span._finish_from_exception(exc)
        if self._span_token is not None:
            _current_span_id.reset(self._span_token)
        if self._trace_token is not None:
            _current_trace_state.reset(self._trace_token)

    async def __aenter__(self) -> TraceSpan:
        return self.__enter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: Any,
    ) -> None:
        self.__exit__(exc_type, exc, traceback)


class NoopTraceScope:
    """A no-op lazy scope used while Core tracing is disabled or unavailable."""

    def __enter__(self) -> NoopTraceSpan:
        return NoopTraceSpan()

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        return None

    async def __aenter__(self) -> NoopTraceSpan:
        return NoopTraceSpan()

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        return None


class TraceSpan:
    """A mutable span handle that becomes immutable at terminal state."""

    def __init__(
        self,
        service: TraceService,
        trace_state: TraceState,
        span_state: SpanState,
        *,
        root: bool = False,
    ) -> None:
        self._service = service
        self._trace_state = trace_state
        self._span_state = span_state
        self._root = root
        self._trace_token: Token[TraceState | None] | None = None
        self._span_token: Token[str | None] | None = None

    @property
    def trace_id(self) -> str:
        """Return the immutable trace identifier."""

        return self._trace_state.trace_id

    @property
    def span_id(self) -> str:
        """Return the immutable span identifier."""

        return self._span_state.span_id

    @property
    def operation(self) -> str:
        """Return the immutable operation name used by Core internals."""

        return self._span_state.operation

    def __enter__(self) -> TraceSpan:
        if current_trace_state() is not self._trace_state:
            self._trace_token = _current_trace_state.set(self._trace_state)
        self._span_token = _current_span_id.set(self.span_id)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: Any,
    ) -> None:
        self._finish_from_exception(exc)
        if self._span_token is not None:
            _current_span_id.reset(self._span_token)
        if self._trace_token is not None:
            _current_trace_state.reset(self._trace_token)

    @contextlib.contextmanager
    def activate(self) -> Iterator[TraceSpan]:
        """Make this running span current without finishing it on scope exit."""

        trace_token: Token[TraceState | None] | None = None
        if current_trace_state() is not self._trace_state:
            trace_token = _current_trace_state.set(self._trace_state)
        span_token = _current_span_id.set(self.span_id)
        try:
            yield self
        finally:
            _current_span_id.reset(span_token)
            if trace_token is not None:
                _current_trace_state.reset(trace_token)

    async def __aenter__(self) -> TraceSpan:
        return self.__enter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: Any,
    ) -> None:
        self.__exit__(exc_type, exc, traceback)

    def set_attributes(self, **attributes: Any) -> TraceSpan:
        """Add JSON-compatible attributes while the span is still running."""

        if not self._span_state.terminal:
            self._service._merge_attributes(self._span_state, attributes)
        return self

    def set_outcome(self, outcome: str | None) -> TraceSpan:
        """Set an extensible business outcome for the span."""

        if not self._span_state.terminal:
            self._span_state.outcome = outcome
        return self

    def mark_degraded(self, reason: str) -> TraceSpan:
        """Mark this span and its root trace degraded without changing success."""

        if self._span_state.terminal:
            return self
        self._span_state.degraded = True
        if reason not in self._span_state.degradation_reasons:
            self._span_state.degradation_reasons.append(reason)
        self._service._mark_degraded(self._trace_state, reason)
        return self

    def add_event(self, name: str, **attributes: Any) -> TraceSpan:
        """Record one instantaneous fact on this span."""

        self._service._add_event(
            self._trace_state,
            self._span_state,
            name,
            attributes,
        )
        return self

    def record_text(
        self,
        role: str,
        text: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> str | None:
        """Capture immutable UTF-8 text and return its content hash when accepted."""

        try:
            body = text.encode("utf-8")
        except (AttributeError, UnicodeError):
            self.mark_degraded("artifact_serialization_failed")
            return None
        return self._service._record_artifact(
            self._trace_state,
            self._span_state,
            role=role,
            body=body,
            media_type="text/plain; charset=utf-8",
            metadata=metadata,
        )

    def record_json(
        self,
        role: str,
        value: Any,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> str | None:
        """Capture immutable canonical JSON and return its content hash when accepted."""

        try:
            body = canonical_json(value)
        except (TypeError, ValueError, UnicodeError):
            self.mark_degraded("artifact_serialization_failed")
            return None
        return self._service._record_artifact(
            self._trace_state,
            self._span_state,
            role=role,
            body=body,
            media_type="application/json",
            metadata=metadata,
        )

    def make_link(
        self,
        relation: str,
        *,
        target_trace_id: str | None = None,
        target_span_id: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> TraceSpan:
        """Create a non-tree causal link to another trace or span."""

        self._service._add_link(
            self._trace_state,
            self._span_state,
            relation=relation,
            target_trace_id=target_trace_id,
            target_span_id=target_span_id,
            attributes=attributes,
        )
        return self

    def finish(
        self,
        *,
        status: str = TRACE_STATUS_SUCCESS,
        outcome: str | None = None,
    ) -> None:
        """Finish the span once; subsequent changes are ignored."""

        self._service._finish_span(
            self._trace_state,
            self._span_state,
            status=status,
            outcome=outcome if outcome is not None else self._span_state.outcome,
        )

    def _finish_from_exception(self, exc: BaseException | None) -> None:
        if self._span_state.terminal:
            return
        if exc is None:
            self.finish()
            return
        if isinstance(exc, asyncio.CancelledError):
            self.finish(status=TRACE_STATUS_CANCELLED, outcome="cancelled")
            return
        if isinstance(exc, GeneratorExit):
            self.finish(status=TRACE_STATUS_CANCELLED, outcome="generator_closed")
            return
        self.set_attributes(exception_type=type(exc).__name__)
        self.finish(status=TRACE_STATUS_ERROR, outcome="exception")


class NoopTraceSpan:
    """A stable no-op implementation returned outside a trace context."""

    trace_id: str | None = None
    span_id: str | None = None

    def __enter__(self) -> NoopTraceSpan:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        return None

    async def __aenter__(self) -> NoopTraceSpan:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        return None

    def set_attributes(self, **attributes: Any) -> NoopTraceSpan:
        return self

    def set_outcome(self, outcome: str | None) -> NoopTraceSpan:
        return self

    def mark_degraded(self, reason: str) -> NoopTraceSpan:
        return self

    def add_event(self, name: str, **attributes: Any) -> NoopTraceSpan:
        return self

    def record_text(
        self,
        role: str,
        text: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        return None

    def record_json(
        self,
        role: str,
        value: Any,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        return None

    def make_link(self, relation: str, **kwargs: Any) -> NoopTraceSpan:
        return self

    def finish(self, **kwargs: Any) -> None:
        return None


class PluginSpan:
    """Restricted, plugin-facing handle for one Core-managed Trace span."""

    def __init__(self, inner: TraceSpan | NoopTraceSpan) -> None:
        self._inner = inner

    @property
    def trace_id(self) -> str | None:
        """Return the immutable visible Trace identifier."""

        return self._inner.trace_id

    @property
    def span_id(self) -> str | None:
        """Return the immutable visible Span identifier."""

        return self._inner.span_id

    def __enter__(self) -> PluginSpan:
        self._inner.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self._inner.__exit__(exc_type, exc, traceback)

    async def __aenter__(self) -> PluginSpan:
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        await self._inner.__aexit__(exc_type, exc, traceback)

    def set_attributes(self, **attributes: Any) -> PluginSpan:
        """Add JSON-compatible attributes while the span remains running."""

        self._inner.set_attributes(**attributes)
        return self

    def set_outcome(self, outcome: str | None) -> PluginSpan:
        """Set an extensible business outcome for this span."""

        self._inner.set_outcome(outcome)
        return self

    def mark_degraded(self, reason: str) -> PluginSpan:
        """Mark this span and its Trace degraded without changing success."""

        self._inner.mark_degraded(reason)
        return self

    def add_event(self, name: str, **attributes: Any) -> PluginSpan:
        """Record an instantaneous fact on this span."""

        self._inner.add_event(name, **attributes)
        return self

    def record_text(
        self,
        role: str,
        text: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> str | None:
        """Capture text as an immutable Artifact."""

        return self._inner.record_text(role, text, metadata=metadata)

    def record_json(
        self,
        role: str,
        value: Any,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> str | None:
        """Capture canonical JSON as an immutable Artifact."""

        return self._inner.record_json(role, value, metadata=metadata)

    def make_link(
        self,
        relation: str,
        *,
        target_trace_id: str | None = None,
        target_span_id: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> PluginSpan:
        """Create a non-tree causal link from this span."""

        self._inner.make_link(
            relation,
            target_trace_id=target_trace_id,
            target_span_id=target_span_id,
            attributes=attributes,
        )
        return self

    def finish(
        self,
        *,
        status: str = TRACE_STATUS_SUCCESS,
        outcome: str | None = None,
    ) -> None:
        """Finish the span using the fixed Core Trace status set."""

        if status not in TRACE_STATUSES:
            self._inner.mark_degraded("invalid_span_status")
            status = TRACE_STATUS_ERROR
            outcome = outcome or "invalid_status"
        self._inner.finish(status=status, outcome=outcome)


class PluginTracer:
    """Stable, plugin-bound extension surface over the Core TraceService."""

    def __init__(self, service: TraceService | None, plugin_id: str | None) -> None:
        self._service = service
        self._plugin_id = plugin_id

    @property
    def trace_id(self) -> str | None:
        """Return the trace identifier visible to this plugin call."""

        if self._service is None:
            return None
        return self._service.current_ids()[0]

    @property
    def span_id(self) -> str | None:
        """Return the active span identifier visible to this plugin call."""

        if self._service is None:
            return None
        return self._service.current_ids()[1]

    def start_span(
        self,
        operation: str,
        *,
        attributes: dict[str, Any] | None = None,
    ) -> PluginSpan:
        """Join the active lazy/real trace or return a no-op outside one."""

        if self._service is None or not _plugin_operation_allowed(operation):
            return PluginSpan(NoopTraceSpan())
        state = current_trace_state()
        if state is None or state.service is not self._service:
            return PluginSpan(NoopTraceSpan())
        return PluginSpan(
            self._service.start_span(
                operation,
                kind="business",
                attributes=attributes,
                source="plugin",
                plugin_id=self._plugin_id,
            )
        )

    def start_root(
        self,
        operation: str,
        *,
        attributes: dict[str, Any] | None = None,
    ) -> PluginSpan:
        """Create an explicit independent plugin business trace."""

        if self._service is None or not _plugin_operation_allowed(operation):
            return PluginSpan(NoopTraceSpan())
        return PluginSpan(
            self._service.start_root(
                operation,
                kind="business",
                attributes=attributes,
                source="plugin",
                plugin_id=self._plugin_id,
            )
        )

    def set_attribute(self, key: str, value: Any) -> PluginSpan:
        """Set one attribute on the active plugin-visible span."""

        return self._active_span().set_attributes(**{key: value})

    def set_attributes(self, **attributes: Any) -> PluginSpan:
        """Set JSON-compatible attributes on the active plugin-visible span."""

        return self._active_span().set_attributes(**attributes)

    def add_event(self, name: str, **attributes: Any) -> PluginSpan:
        """Add an instantaneous event to the active plugin-visible span."""

        return self._active_span().add_event(name, **attributes)

    def record_text(
        self,
        role: str,
        text: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> str | None:
        """Record text on the active plugin-visible span."""

        return self._active_span().record_text(role, text, metadata=metadata)

    def record_json(
        self,
        role: str,
        value: Any,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> str | None:
        """Record canonical JSON on the active plugin-visible span."""

        return self._active_span().record_json(role, value, metadata=metadata)

    def set_outcome(self, outcome: str | None) -> PluginSpan:
        """Set the active plugin-visible span's business outcome."""

        return self._active_span().set_outcome(outcome)

    def mark_degraded(self, reason: str) -> PluginSpan:
        """Mark the active plugin-visible span and its trace degraded."""

        return self._active_span().mark_degraded(reason)

    def make_link(self, relation: str, **kwargs: Any) -> PluginSpan:
        """Add a causal link from the active plugin-visible span."""

        return self._active_span().make_link(relation, **kwargs)

    def _active_span(self) -> PluginSpan:
        if self._service is None:
            return PluginSpan(NoopTraceSpan())
        state = current_trace_state()
        span_id = current_span_id()
        if (
            state is None
            or state.service is not self._service
            or span_id is None
            or span_id not in state.spans
        ):
            return PluginSpan(NoopTraceSpan())
        return PluginSpan(TraceSpan(self._service, state, state.spans[span_id]))


NOOP_PLUGIN_TRACER = PluginTracer(None, None)


def _plugin_operation_allowed(operation: str) -> bool:
    """Keep plugin business spans distinct from Core automatic operations."""

    return bool(_PLUGIN_OPERATION_RE.fullmatch(operation)) and (
        operation not in _RESERVED_PLUGIN_OPERATIONS
    )
