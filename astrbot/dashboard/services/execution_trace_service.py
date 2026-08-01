"""Dashboard-facing access to the Core execution Trace service."""

from __future__ import annotations

from typing import Any

from astrbot.core.core_lifecycle import AstrBotCoreLifecycle
from astrbot.core.trace.storage import (
    TraceDeleteConflictError,
    TraceNotFoundError,
    TraceStorageError,
)


class ExecutionTraceServiceError(RuntimeError):
    """Raised when the Core execution Trace service is unavailable."""


class ExecutionTraceService:
    """Expose durable Core Trace data without exposing its storage internals."""

    def __init__(self, core_lifecycle: AstrBotCoreLifecycle) -> None:
        self.core_lifecycle = core_lifecycle

    def _trace_service(self):
        service = getattr(self.core_lifecycle, "trace_service", None)
        if service is None:
            raise ExecutionTraceServiceError("Execution Trace is unavailable")
        return service

    async def get_overview(self) -> dict[str, Any]:
        """Return the small aggregate used by the Trace landing page."""

        return await self._trace_service().store.get_overview()

    async def list_traces(
        self,
        *,
        limit: int,
        before_ended_at: float | None,
        before_trace_id: str | None,
        status: str | None,
        operation: str | None,
        plugin_id: str | None,
        degraded: bool | None,
    ) -> list[dict[str, Any]]:
        """Return a keyset-paginated newest-first Trace list."""

        return await self._trace_service().store.list_traces(
            limit=limit,
            before_ended_at=before_ended_at,
            before_trace_id=before_trace_id,
            status=status,
            operation=operation,
            plugin_id=plugin_id,
            degraded=degraded,
        )

    async def get_trace(self, trace_id: str) -> dict[str, Any]:
        """Return one Trace tree without eagerly loading Artifact bodies."""

        return await self._trace_service().store.get_trace(trace_id)

    async def get_artifact(self, content_hash: str) -> dict[str, Any]:
        """Return one captured text/JSON Artifact body and immutable metadata."""

        body, metadata = await self._trace_service().store.get_artifact_body(
            content_hash
        )
        return {
            "metadata": metadata,
            "content": body.decode("utf-8", errors="replace"),
        }

    async def delete_trace(self, trace_id: str) -> None:
        """Delete one terminal Trace and any exclusively referenced Artifacts."""

        await self._trace_service().store.delete_trace(trace_id)

    async def clear_terminal_traces(self) -> int:
        """Delete all terminal Traces while preserving in-flight executions."""

        return await self._trace_service().store.clear_terminal_traces()

    async def cleanup(self) -> dict[str, int]:
        """Run the configured retention and capacity cleanup once."""

        return await self._trace_service().cleanup()

    def get_config(self) -> dict[str, Any]:
        """Return the persisted toggle and runtime availability state."""

        service = getattr(self.core_lifecycle, "trace_service", None)
        config = self.core_lifecycle.astrbot_config
        return {
            "enabled": bool(config.get("execution_trace_enable", True)),
            "runtime_available": bool(
                service is not None and getattr(service, "_accepting", False)
            ),
        }

    def set_enabled(self, enabled: bool) -> dict[str, Any]:
        """Persist and apply the execution Trace toggle for future roots."""

        config = self.core_lifecycle.astrbot_config
        config["execution_trace_enable"] = bool(enabled)
        save_config = getattr(config, "save_config", None)
        if callable(save_config):
            try:
                save_config()
            except TypeError:
                # Lightweight dashboard test/config adapters historically
                # required the complete replacement mapping as an argument.
                save_config(dict(config))
        service = getattr(self.core_lifecycle, "trace_service", None)
        if service is not None:
            service.set_enabled(bool(enabled))
        return self.get_config()


__all__ = [
    "ExecutionTraceService",
    "ExecutionTraceServiceError",
    "TraceDeleteConflictError",
    "TraceNotFoundError",
    "TraceStorageError",
]
