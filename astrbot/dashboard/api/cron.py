from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from astrbot.dashboard.responses import ApiError, ok
from astrbot.dashboard.schemas import (
    CronJobCreateRequest,
    CronJobPatchRequest,
    CronJobRequest,
    ScriptValidateRequest,
)
from astrbot.dashboard.services.cron_service import CronService, CronServiceError

from .auth import AuthContext, require_dashboard_user, require_scope

router = APIRouter(tags=["Cron"])
legacy_router = APIRouter(
    prefix="/api/cron",
    tags=["Dashboard Cron"],
    include_in_schema=False,
)


async def require_system_scope(request: Request) -> AuthContext:
    return await require_scope(request, "system")


def get_service(request: Request) -> CronService:
    return request.app.state.services.cron


def _payload_dict(payload: CronJobRequest) -> dict:
    return payload.model_dump(exclude_none=True)


def _raise_cron_error(exc: CronServiceError) -> None:
    data: dict = {}
    if isinstance(exc.data, dict):
        data.update(exc.data)
    elif exc.data is not None:
        data["details"] = exc.data
    data["code"] = exc.code
    raise ApiError(
        exc.message,
        status_code=exc.status_code,
        data=data,
    ) from exc


async def _list_jobs(job_type: str | None, service: CronService):
    try:
        return ok(await service.list_jobs(job_type))
    except CronServiceError as exc:
        _raise_cron_error(exc)


async def _create_job(payload: CronJobRequest, service: CronService):
    try:
        return ok(await service.create_job(_payload_dict(payload)))
    except CronServiceError as exc:
        _raise_cron_error(exc)


async def _update_job(job_id: str, payload: CronJobRequest, service: CronService):
    try:
        return ok(await service.update_job(job_id, _payload_dict(payload)))
    except CronServiceError as exc:
        _raise_cron_error(exc)


async def _delete_job(job_id: str, service: CronService):
    try:
        await service.delete_job(job_id)
        return ok(message="deleted")
    except CronServiceError as exc:
        _raise_cron_error(exc)


async def _run_job(job_id: str, service: CronService):
    try:
        await service.run_job_now(job_id)
        return ok({"job_id": job_id, "accepted": True}, message="started")
    except CronServiceError as exc:
        _raise_cron_error(exc)


async def _get_job(job_id: str, service: CronService):
    try:
        return ok(await service.get_job(job_id))
    except CronServiceError as exc:
        _raise_cron_error(exc)


async def _validate_script(payload: ScriptValidateRequest, service: CronService):
    try:
        return ok(
            await service.validate_script(payload.source, payload.language_version)
        )
    except CronServiceError as exc:
        _raise_cron_error(exc)


async def _script_languages(service: CronService):
    return ok(service.script_languages())


async def _reset_script_state(job_id: str, service: CronService):
    try:
        return ok(await service.reset_script_state(job_id))
    except CronServiceError as exc:
        _raise_cron_error(exc)


@router.get("/cron/jobs")
async def list_cron_jobs(
    job_type: str | None = Query(default=None, alias="type"),
    _auth: AuthContext = Depends(require_system_scope),
    service: CronService = Depends(get_service),
):
    return await _list_jobs(job_type, service)


@router.post("/cron/jobs")
async def create_cron_job(
    payload: CronJobCreateRequest,
    _auth: AuthContext = Depends(require_system_scope),
    service: CronService = Depends(get_service),
):
    return await _create_job(payload, service)


@router.patch("/cron/jobs/{job_id}")
async def update_cron_job(
    job_id: str,
    payload: CronJobPatchRequest,
    _auth: AuthContext = Depends(require_system_scope),
    service: CronService = Depends(get_service),
):
    return await _update_job(job_id, payload, service)


@router.get("/cron/jobs/{job_id}")
async def get_cron_job(
    job_id: str,
    _auth: AuthContext = Depends(require_system_scope),
    service: CronService = Depends(get_service),
):
    return await _get_job(job_id, service)


@router.delete("/cron/jobs/{job_id}")
async def delete_cron_job(
    job_id: str,
    _auth: AuthContext = Depends(require_system_scope),
    service: CronService = Depends(get_service),
):
    return await _delete_job(job_id, service)


@router.post("/cron/jobs/{job_id}/run")
async def run_cron_job(
    job_id: str,
    _auth: AuthContext = Depends(require_system_scope),
    service: CronService = Depends(get_service),
):
    return await _run_job(job_id, service)


@router.post("/cron/script/validate")
async def validate_cron_script(
    payload: ScriptValidateRequest,
    _auth: AuthContext = Depends(require_system_scope),
    service: CronService = Depends(get_service),
):
    return await _validate_script(payload, service)


@router.get("/cron/script/languages")
async def cron_script_languages(
    _auth: AuthContext = Depends(require_system_scope),
    service: CronService = Depends(get_service),
):
    return await _script_languages(service)


@router.post("/cron/jobs/{job_id}/state/reset")
async def reset_cron_script_state(
    job_id: str,
    _auth: AuthContext = Depends(require_system_scope),
    service: CronService = Depends(get_service),
):
    return await _reset_script_state(job_id, service)


@legacy_router.get("/jobs")
async def list_dashboard_cron_jobs(
    job_type: str | None = Query(default=None, alias="type"),
    _username: str = Depends(require_dashboard_user),
    service: CronService = Depends(get_service),
):
    return await _list_jobs(job_type, service)


@legacy_router.get("/jobs/{job_id}")
async def get_dashboard_cron_job(
    job_id: str,
    _username: str = Depends(require_dashboard_user),
    service: CronService = Depends(get_service),
):
    return await _get_job(job_id, service)


@legacy_router.post("/jobs")
async def create_dashboard_cron_job(
    payload: CronJobRequest,
    _username: str = Depends(require_dashboard_user),
    service: CronService = Depends(get_service),
):
    return await _create_job(payload, service)


@legacy_router.patch("/jobs/{job_id}")
async def update_dashboard_cron_job(
    job_id: str,
    payload: CronJobRequest,
    _username: str = Depends(require_dashboard_user),
    service: CronService = Depends(get_service),
):
    return await _update_job(job_id, payload, service)


@legacy_router.delete("/jobs/{job_id}")
async def delete_dashboard_cron_job(
    job_id: str,
    _username: str = Depends(require_dashboard_user),
    service: CronService = Depends(get_service),
):
    return await _delete_job(job_id, service)


@legacy_router.post("/jobs/{job_id}/run")
async def run_dashboard_cron_job(
    job_id: str,
    _username: str = Depends(require_dashboard_user),
    service: CronService = Depends(get_service),
):
    return await _run_job(job_id, service)


@legacy_router.post("/jobs/{job_id}/state/reset")
async def reset_dashboard_cron_script_state(
    job_id: str,
    _username: str = Depends(require_dashboard_user),
    service: CronService = Depends(get_service),
):
    return await _reset_script_state(job_id, service)


@legacy_router.post("/script/validate")
async def validate_dashboard_cron_script(
    payload: ScriptValidateRequest,
    _username: str = Depends(require_dashboard_user),
    service: CronService = Depends(get_service),
):
    return await _validate_script(payload, service)


@legacy_router.get("/script/languages")
async def dashboard_cron_script_languages(
    _username: str = Depends(require_dashboard_user),
    service: CronService = Depends(get_service),
):
    return await _script_languages(service)
