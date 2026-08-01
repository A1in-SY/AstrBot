"""HTTP API for durable Core execution traces."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from astrbot.core.trace.storage import (
    TraceDeleteConflictError,
    TraceNotFoundError,
    TraceStorageError,
)
from astrbot.dashboard.responses import ApiError, ok
from astrbot.dashboard.schemas import ExecutionTraceConfigRequest
from astrbot.dashboard.services.execution_trace_service import (
    ExecutionTraceService,
    ExecutionTraceServiceError,
)

from .auth import AuthContext, require_scope

router = APIRouter(tags=["Execution Trace"])


async def require_system_scope(request: Request) -> AuthContext:
    """Require the dashboard's existing system-management permission."""

    return await require_scope(request, "system")


def get_service(request: Request) -> ExecutionTraceService:
    """Resolve the application-owned execution Trace facade."""

    return request.app.state.services.execution_trace


def _raise_trace_error(exc: Exception) -> None:
    if isinstance(exc, TraceNotFoundError):
        raise ApiError("Trace not found", status_code=404) from exc
    if isinstance(exc, TraceDeleteConflictError):
        raise ApiError("Running traces cannot be deleted", status_code=409) from exc
    if isinstance(exc, (ExecutionTraceServiceError, TraceStorageError)):
        raise ApiError(str(exc), status_code=503) from exc
    raise ApiError(f"Execution Trace request failed: {exc}") from exc


@router.get("/traces/overview")
async def get_trace_overview(
    _auth: AuthContext = Depends(require_system_scope),
    service: ExecutionTraceService = Depends(get_service),
):
    """Return aggregate Trace state without loading individual spans."""

    try:
        return ok(await service.get_overview())
    except Exception as exc:
        _raise_trace_error(exc)


@router.get("/traces/config")
async def get_trace_config(
    _auth: AuthContext = Depends(require_system_scope),
    service: ExecutionTraceService = Depends(get_service),
):
    """Return Core execution Trace configuration, not the removed legacy setting."""

    try:
        return ok(service.get_config())
    except Exception as exc:
        _raise_trace_error(exc)


@router.put("/traces/config")
async def update_trace_config(
    payload: ExecutionTraceConfigRequest,
    _auth: AuthContext = Depends(require_system_scope),
    service: ExecutionTraceService = Depends(get_service),
):
    """Apply the execution Trace toggle for subsequently created roots."""

    try:
        return ok(service.set_enabled(payload.enabled))
    except Exception as exc:
        _raise_trace_error(exc)


@router.get("/traces")
async def list_traces(
    limit: int = Query(default=50, ge=1, le=200),
    before_ended_at: float | None = Query(default=None),
    before_trace_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    operation: str | None = Query(default=None),
    plugin_id: str | None = Query(default=None),
    degraded: bool | None = Query(default=None),
    _auth: AuthContext = Depends(require_system_scope),
    service: ExecutionTraceService = Depends(get_service),
):
    """List Trace summaries using a stable newest-first keyset cursor."""

    try:
        traces = await service.list_traces(
            limit=limit,
            before_ended_at=before_ended_at,
            before_trace_id=before_trace_id,
            status=status,
            operation=operation,
            plugin_id=plugin_id,
            degraded=degraded,
        )
        return ok({"items": traces})
    except Exception as exc:
        _raise_trace_error(exc)


@router.get("/traces/artifacts/{content_hash}")
async def get_trace_artifact(
    content_hash: str,
    _auth: AuthContext = Depends(require_system_scope),
    service: ExecutionTraceService = Depends(get_service),
):
    """Load one text/JSON Artifact body on demand."""

    try:
        return ok(await service.get_artifact(content_hash))
    except Exception as exc:
        _raise_trace_error(exc)


@router.get("/traces/{trace_id}")
async def get_trace(
    trace_id: str,
    _auth: AuthContext = Depends(require_system_scope),
    service: ExecutionTraceService = Depends(get_service),
):
    """Return one durable Trace tree."""

    try:
        return ok(await service.get_trace(trace_id))
    except Exception as exc:
        _raise_trace_error(exc)


@router.delete("/traces/{trace_id}")
async def delete_trace(
    trace_id: str,
    _auth: AuthContext = Depends(require_system_scope),
    service: ExecutionTraceService = Depends(get_service),
):
    """Delete one terminal Trace."""

    try:
        await service.delete_trace(trace_id)
        return ok(message="deleted")
    except Exception as exc:
        _raise_trace_error(exc)


@router.delete("/traces")
async def clear_terminal_traces(
    _auth: AuthContext = Depends(require_system_scope),
    service: ExecutionTraceService = Depends(get_service),
):
    """Clear all terminal Traces without deleting active work."""

    try:
        return ok({"deleted": await service.clear_terminal_traces()})
    except Exception as exc:
        _raise_trace_error(exc)


@router.post("/traces/cleanup")
async def cleanup_traces(
    _auth: AuthContext = Depends(require_system_scope),
    service: ExecutionTraceService = Depends(get_service),
):
    """Run one retention/capacity cleanup pass."""

    try:
        return ok(await service.cleanup())
    except Exception as exc:
        _raise_trace_error(exc)
