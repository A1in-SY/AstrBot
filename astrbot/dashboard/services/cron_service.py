from __future__ import annotations

import traceback
from datetime import datetime, timezone
from typing import Any

from astrbot.core import logger
from astrbot.core.core_lifecycle import AstrBotCoreLifecycle
from astrbot.core.cron.manager import (
    CronJobAlreadyRunningError,
    CronJobNotFoundError,
    CronJobSchedulingError,
    CronJobShuttingDownError,
    CronScriptDefinitionError,
    CronScriptNotAuthorizedError,
    CronScriptValidationError,
)
from astrbot.core.platform.message_session import MessageSession
from astrbot.script_runtime import spec

_MAPPED_ERROR_TYPES = (
    CronJobAlreadyRunningError,
    CronJobNotFoundError,
    CronJobSchedulingError,
    CronJobShuttingDownError,
    CronScriptDefinitionError,
    CronScriptNotAuthorizedError,
    CronScriptValidationError,
)


class CronServiceError(Exception):
    def __init__(
        self,
        message: str,
        status_code: int = 400,
        code: str = "CRON_ERROR",
        data: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code
        self.data = data


def _wrap(exc: Exception, message: str) -> CronServiceError:
    if isinstance(exc, CronJobNotFoundError):
        return CronServiceError(message, 404, "JOB_NOT_FOUND")
    if isinstance(exc, CronJobAlreadyRunningError):
        return CronServiceError(message, 409, "JOB_ALREADY_RUNNING")
    if isinstance(exc, CronScriptNotAuthorizedError):
        return CronServiceError(message, 403, "SCRIPT_TASK_NOT_AUTHORIZED")
    if isinstance(exc, CronScriptValidationError):
        return CronServiceError(
            str(exc),
            422,
            "SCRIPT_VALIDATION_FAILED",
            data={"validation": exc.validation.to_dict()},
        )
    if isinstance(exc, CronScriptDefinitionError):
        return CronServiceError(message, 422, "SCRIPT_TASK_DEFINITION_ERROR")
    if isinstance(exc, CronJobSchedulingError):
        return CronServiceError(message, 422, "CRON_SCHEDULING_ERROR")
    if isinstance(exc, CronJobShuttingDownError):
        return CronServiceError(message, 503, "CRON_SHUTTING_DOWN")
    return CronServiceError(message, 400, "CRON_ERROR")


class CronService:
    def __init__(self, core_lifecycle: AstrBotCoreLifecycle) -> None:
        self.core_lifecycle = core_lifecycle

    def _get_cron_manager(self):
        cron_mgr = self.core_lifecycle.cron_manager
        if cron_mgr is None:
            raise CronServiceError(
                "Cron manager not initialized", 503, "CRON_NOT_INITIALIZED"
            )
        return cron_mgr

    @staticmethod
    def serialize_job(job) -> dict:
        data = job.model_dump() if hasattr(job, "model_dump") else dict(job.__dict__)
        for key in ["created_at", "updated_at", "last_run_at", "next_run_time"]:
            value = data.get(key)
            if isinstance(value, datetime):
                if value.tzinfo is None:
                    value = value.replace(tzinfo=timezone.utc)
                data[key] = value.isoformat()
        payload = data.get("payload") or {}
        data["note"] = payload.get("note") or data.get("description") or ""
        data["run_at"] = payload.get("run_at")
        data["run_once"] = data.get("run_once", False)
        data["session"] = payload.get("session") or ""
        return data

    async def list_jobs(self, job_type: str | None = None) -> list[dict]:
        try:
            cron_mgr = self._get_cron_manager()
            jobs = await cron_mgr.list_jobs(job_type)
            summaries = {
                summary.job.job_id: summary
                for summary in await cron_mgr.db.list_script_cron_job_summaries()
            }
            result = []
            for job in jobs:
                item = self.serialize_job(job)
                if job.job_type == "script":
                    summary = summaries.get(job.job_id)
                    item["script_summary"] = self._script_summary(summary)
                result.append(item)
            return result
        except CronServiceError:
            raise
        except Exception as exc:
            logger.error(traceback.format_exc())
            raise CronServiceError(
                f"Failed to list jobs: {exc!s}", 500, "CRON_LIST_FAILED"
            ) from exc

    async def get_job(self, job_id: str) -> dict:
        try:
            cron_mgr = self._get_cron_manager()
            job = await cron_mgr.db.get_cron_job(job_id)
            if not job:
                raise CronJobNotFoundError(job_id)
            item = self.serialize_job(job)
            if job.job_type == "script":
                aggregate = await cron_mgr.get_script_job(job_id)
                if aggregate is None:
                    raise CronScriptDefinitionError("script definition row is missing")
                item["script"] = self._script_detail(aggregate)
                item["script_summary"] = self._script_summary_for(aggregate)
            return item
        except CronServiceError:
            raise
        except Exception as exc:
            if isinstance(exc, _MAPPED_ERROR_TYPES):
                raise _wrap(exc, str(exc)) from exc
            logger.error(traceback.format_exc())
            raise CronServiceError(
                f"Failed to get job: {exc!s}", 500, "CRON_GET_FAILED"
            ) from exc

    async def create_job(self, payload: object) -> dict:
        try:
            cron_mgr = self._get_cron_manager()
            if not isinstance(payload, dict):
                raise CronServiceError("Invalid payload", 422, "INVALID_PAYLOAD")
            job_type = str(payload.get("job_type") or "active_agent").strip()
            if job_type == "script":
                return await self._create_script_job(cron_mgr, payload)
            if job_type != "active_agent":
                raise CronServiceError(
                    "job_type must be active_agent or script",
                    422,
                    "JOB_TYPE_INVALID",
                )
            return await self._create_active_agent_job(cron_mgr, payload)
        except CronServiceError:
            raise
        except Exception as exc:
            if isinstance(exc, _MAPPED_ERROR_TYPES):
                raise _wrap(exc, f"Failed to create job: {exc!s}") from exc
            logger.error(traceback.format_exc())
            raise _wrap(exc, f"Failed to create job: {exc!s}") from exc

    async def _create_active_agent_job(self, cron_mgr, payload: dict) -> dict:
        name = payload.get("name") or "active_agent_task"
        cron_expression = payload.get("cron_expression")
        note = payload.get("note") or payload.get("description") or name
        session = str(payload.get("session") or "").strip()
        persona_id = payload.get("persona_id")
        provider_id = payload.get("provider_id")
        timezone_name = payload.get("timezone")
        enabled = bool(payload.get("enabled", True))
        run_once = bool(payload.get("run_once", False))
        run_at = payload.get("run_at")
        if run_once and not run_at:
            raise CronServiceError(
                "run_at is required when run_once=true", 422, "RUN_AT_REQUIRED"
            )
        if (not run_once) and not cron_expression:
            raise CronServiceError(
                "cron_expression is required when run_once=false",
                422,
                "CRON_EXPRESSION_REQUIRED",
            )
        if run_once and cron_expression:
            cron_expression = None
        run_at_dt = self._parse_optional_run_at(run_at)
        job_payload = {
            "session": session,
            "note": note,
            "persona_id": persona_id,
            "provider_id": provider_id,
            "run_at": run_at,
            "origin": "api",
        }
        job = await cron_mgr.add_active_job(
            name=name,
            cron_expression=cron_expression,
            payload=job_payload,
            description=note,
            timezone=timezone_name,
            enabled=enabled,
            run_once=run_once,
            run_at=run_at_dt,
        )
        return self.serialize_job(job)

    async def _create_script_job(self, cron_mgr, payload: dict) -> dict:
        source = payload.get("source")
        if not isinstance(source, str) or not source.strip():
            raise CronServiceError(
                "source is required for script jobs", 422, "SCRIPT_SOURCE_REQUIRED"
            )
        bound_umo = self._resolve_bound_umo(payload)
        name = payload.get("name") or "script_task"
        note = payload.get("note") or payload.get("description") or ""
        cron_expression = payload.get("cron_expression")
        run_once = bool(payload.get("run_once", False))
        run_at = payload.get("run_at")
        enabled = bool(payload.get("enabled", True))
        timezone_name = payload.get("timezone")
        language_version = (
            payload.get("language_version") or spec.DEFAULT_LANGUAGE_VERSION
        )
        if language_version not in spec.SUPPORTED_LANGUAGE_VERSIONS:
            raise CronServiceError(
                f"Unknown language version {language_version!r}",
                422,
                "SCRIPT_LANGUAGE_VERSION_UNKNOWN",
            )
        if run_once and not run_at:
            raise CronServiceError(
                "run_at is required when run_once=true", 422, "RUN_AT_REQUIRED"
            )
        if (not run_once) and not cron_expression:
            raise CronServiceError(
                "cron_expression is required when run_once=false",
                422,
                "CRON_EXPRESSION_REQUIRED",
            )
        if run_once and cron_expression:
            cron_expression = None
        run_at_dt = self._parse_optional_run_at(run_at)
        aggregate = await cron_mgr.add_script_job(
            name=name,
            cron_expression=str(cron_expression) if cron_expression else None,
            source=source,
            bound_umo=bound_umo,
            description=note or None,
            timezone=timezone_name,
            enabled=enabled,
            run_once=run_once,
            run_at=run_at_dt,
            creator_sender_id=None,
            language_version=str(language_version),
        )
        item = self.serialize_job(aggregate.job)
        item["script_summary"] = self._script_summary_for(aggregate)
        return item

    async def update_job(self, job_id: str, payload: object) -> dict:
        try:
            cron_mgr = self._get_cron_manager()
            if not isinstance(payload, dict):
                raise CronServiceError("Invalid payload", 422, "INVALID_PAYLOAD")
            job = await cron_mgr.db.get_cron_job(job_id)
            if not job:
                raise CronJobNotFoundError(job_id)
            if "job_type" in payload and payload.get("job_type") != job.job_type:
                raise CronServiceError(
                    "job_type cannot be changed", 422, "JOB_TYPE_IMMUTABLE"
                )
            if job.job_type == "script":
                updated = await self._update_script_job(cron_mgr, job, payload)
            else:
                updated = await self._update_active_agent_job(cron_mgr, job, payload)
            return self.serialize_job(updated)
        except CronServiceError:
            raise
        except Exception as exc:
            if isinstance(exc, _MAPPED_ERROR_TYPES):
                raise _wrap(exc, f"Failed to update job: {exc!s}") from exc
            logger.error(traceback.format_exc())
            raise _wrap(exc, f"Failed to update job: {exc!s}") from exc

    async def _update_script_job(self, cron_mgr, job, payload: dict):
        updates: dict[str, Any] = {}
        if "name" in payload:
            name = str(payload.get("name") or "").strip()
            if not name:
                raise CronServiceError("name cannot be empty", 422, "NAME_EMPTY")
            updates["name"] = name
        if "description" in payload:
            description = str(payload.get("description") or "").strip()
            updates["description"] = description or None
        if "note" in payload:
            note = str(payload.get("note") or "").strip()
            updates["description"] = note or None
        if "enabled" in payload:
            updates["enabled"] = bool(payload.get("enabled"))
        if "timezone" in payload:
            updates["timezone"] = str(payload.get("timezone") or "").strip() or None
        if "source" in payload:
            source = payload.get("source")
            if not isinstance(source, str) or not source.strip():
                raise CronServiceError(
                    "source cannot be empty", 422, "SCRIPT_SOURCE_REQUIRED"
                )
            updates["source"] = source
        if "language_version" in payload:
            version = str(payload.get("language_version") or "").strip()
            if version not in spec.SUPPORTED_LANGUAGE_VERSIONS:
                raise CronServiceError(
                    f"Unknown language version {version!r}",
                    422,
                    "SCRIPT_LANGUAGE_VERSION_UNKNOWN",
                )
            updates["language_version"] = version
        if "bound_umo" in payload or "session" in payload:
            updates["bound_umo"] = self._resolve_bound_umo(payload)
        if "run_once" in payload or "cron_expression" in payload or "run_at" in payload:
            aggregate = await cron_mgr.get_script_job(job.job_id)
            if aggregate is None:
                raise CronScriptDefinitionError("script definition row is missing")
            merged_payload = dict(job.payload) if isinstance(job.payload, dict) else {}
            run_once = (
                bool(payload.get("run_once"))
                if "run_once" in payload
                else bool(job.run_once)
            )
            cron_expression = (
                str(payload.get("cron_expression") or "").strip()
                if "cron_expression" in payload
                else job.cron_expression
            )
            cron_expression = cron_expression or None
            run_at_raw = (
                payload.get("run_at")
                if "run_at" in payload
                else merged_payload.get("run_at")
            )
            run_at_iso = self._normalize_run_at_iso(run_at_raw)
            if run_once:
                if not run_at_iso:
                    raise CronServiceError(
                        "run_at is required when run_once=true",
                        422,
                        "RUN_AT_REQUIRED",
                    )
                cron_expression = None
                merged_payload["run_at"] = run_at_iso
            else:
                if not cron_expression:
                    raise CronServiceError(
                        "cron_expression is required when run_once=false",
                        422,
                        "CRON_EXPRESSION_REQUIRED",
                    )
                merged_payload.pop("run_at", None)
            updates["run_once"] = run_once
            updates["cron_expression"] = cron_expression
            updates["payload"] = merged_payload
        updated = await cron_mgr.update_job(job.job_id, **updates)
        if updated is None:
            raise CronJobNotFoundError(job.job_id)
        return updated

    async def _update_active_agent_job(self, cron_mgr, job, payload: dict):
        updates: dict[str, Any] = {}
        if "name" in payload:
            name = str(payload.get("name") or "").strip()
            if not name:
                raise CronServiceError("name cannot be empty", 422, "NAME_EMPTY")
            updates["name"] = name
        if "enabled" in payload:
            updates["enabled"] = bool(payload.get("enabled"))
        if "timezone" in payload:
            updates["timezone"] = str(payload.get("timezone") or "").strip() or None
        merged_payload = dict(job.payload) if isinstance(job.payload, dict) else {}
        if "session" in payload:
            session = str(payload.get("session") or "").strip()
            if session:
                merged_payload["session"] = session
            else:
                merged_payload.pop("session", None)
        note_updated = False
        if "note" in payload:
            note = str(payload.get("note") or "").strip()
            if not note:
                raise CronServiceError("note cannot be empty", 422, "NOTE_EMPTY")
            merged_payload["note"] = note
            updates["description"] = note
            note_updated = True
        if "description" in payload:
            description = str(payload.get("description") or "").strip()
            if not description:
                raise CronServiceError("description cannot be empty", 422, "NOTE_EMPTY")
            updates["description"] = description
            merged_payload["note"] = description
            note_updated = True
        if not note_updated:
            existing_note = str(
                merged_payload.get("note") or job.description or ""
            ).strip()
            if existing_note:
                merged_payload["note"] = existing_note
        if "run_once" in payload or "cron_expression" in payload or "run_at" in payload:
            run_once = (
                bool(payload.get("run_once"))
                if "run_once" in payload
                else bool(job.run_once)
            )
            cron_expression = (
                str(payload.get("cron_expression") or "").strip()
                if "cron_expression" in payload
                else job.cron_expression
            )
            cron_expression = cron_expression or None
            run_at_raw = (
                payload.get("run_at")
                if "run_at" in payload
                else merged_payload.get("run_at")
            )
            run_at_iso = self._normalize_run_at_iso(run_at_raw)
            if run_once:
                if not run_at_iso:
                    raise CronServiceError(
                        "run_at is required when run_once=true",
                        422,
                        "RUN_AT_REQUIRED",
                    )
                cron_expression = None
                merged_payload["run_at"] = run_at_iso
            else:
                if not cron_expression:
                    raise CronServiceError(
                        "cron_expression is required when run_once=false",
                        422,
                        "CRON_EXPRESSION_REQUIRED",
                    )
                merged_payload.pop("run_at", None)
            updates["run_once"] = run_once
            updates["cron_expression"] = cron_expression
            updates["payload"] = merged_payload
        if "payload" in payload and isinstance(payload.get("payload"), dict):
            merged_payload.update(payload["payload"])
            updates["payload"] = merged_payload
        updated = await cron_mgr.update_job(job.job_id, **updates)
        if updated is None:
            raise CronJobNotFoundError(job.job_id)
        return updated

    async def delete_job(self, job_id: str) -> None:
        try:
            cron_mgr = self._get_cron_manager()
            await cron_mgr.delete_job(job_id)
        except CronServiceError:
            raise
        except Exception as exc:
            if isinstance(exc, _MAPPED_ERROR_TYPES):
                raise _wrap(exc, f"Failed to delete job: {exc!s}") from exc
            logger.error(traceback.format_exc())
            raise _wrap(exc, f"Failed to delete job: {exc!s}") from exc

    async def run_job_now(self, job_id: str) -> None:
        try:
            cron_mgr = self._get_cron_manager()
            await cron_mgr.run_job_now(job_id)
        except CronServiceError:
            raise
        except Exception as exc:
            if isinstance(exc, _MAPPED_ERROR_TYPES):
                raise _wrap(exc, f"Failed to run job: {exc!s}") from exc
            logger.error(traceback.format_exc())
            raise _wrap(exc, f"Failed to run job: {exc!s}") from exc

    async def validate_script(self, source: str, language_version: str) -> dict:
        try:
            cron_mgr = self._get_cron_manager()
            if language_version not in spec.SUPPORTED_LANGUAGE_VERSIONS:
                raise CronServiceError(
                    f"Unknown language version {language_version!r}",
                    422,
                    "SCRIPT_LANGUAGE_VERSION_UNKNOWN",
                )
            result = await cron_mgr.validate_script_source(source, language_version)
            return result.to_dict()
        except CronServiceError:
            raise
        except Exception as exc:
            logger.error(traceback.format_exc())
            raise CronServiceError(
                f"Failed to validate script: {exc!s}", 500, "SCRIPT_VALIDATION_ERROR"
            ) from exc

    @staticmethod
    def script_languages() -> dict:
        return {
            "default_language_version": spec.DEFAULT_LANGUAGE_VERSION,
            "versions": [
                {
                    "language_version": spec.DEFAULT_LANGUAGE_VERSION,
                    "display_name": "astrbot-python-subset v1",
                    "deprecated": False,
                }
            ],
            "limits": dict(spec.DEFAULT_LIMITS),
        }

    async def reset_script_state(self, job_id: str) -> dict:
        try:
            cron_mgr = self._get_cron_manager()
            aggregate = await cron_mgr.reset_script_cron_state(job_id)
            item = self.serialize_job(aggregate.job)
            item["script"] = self._script_detail(aggregate)
            item["script_summary"] = self._script_summary_for(aggregate)
            return item
        except CronServiceError:
            raise
        except Exception as exc:
            if isinstance(exc, _MAPPED_ERROR_TYPES):
                raise _wrap(exc, f"Failed to reset script state: {exc!s}") from exc
            logger.error(traceback.format_exc())
            raise _wrap(exc, f"Failed to reset script state: {exc!s}") from exc

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_bound_umo(payload: dict) -> str:
        bound_umo = payload.get("bound_umo") or payload.get("session")
        if not isinstance(bound_umo, str) or not bound_umo.strip():
            raise CronServiceError(
                "bound_umo is required for script jobs",
                422,
                "SCRIPT_BOUND_UMO_REQUIRED",
            )
        bound_umo = bound_umo.strip()
        try:
            session = MessageSession.from_str(bound_umo)
        except Exception as exc:
            raise CronServiceError(
                f"bound_umo must be a valid UMO: {exc}",
                422,
                "SCRIPT_BOUND_UMO_INVALID",
            ) from exc
        if not session.platform_id or not session.session_id:
            raise CronServiceError(
                "bound_umo must include a platform and a session",
                422,
                "SCRIPT_BOUND_UMO_INVALID",
            )
        return bound_umo

    @staticmethod
    def _parse_optional_run_at(run_at: object) -> datetime | None:
        if not run_at:
            return None
        try:
            return datetime.fromisoformat(str(run_at))
        except Exception as exc:
            raise CronServiceError(
                "run_at must be ISO datetime", 422, "RUN_AT_INVALID"
            ) from exc

    @staticmethod
    def _normalize_run_at_iso(run_at: object) -> str | None:
        if not run_at:
            return None
        try:
            return datetime.fromisoformat(str(run_at)).isoformat()
        except Exception as exc:
            raise CronServiceError(
                "run_at must be ISO datetime", 422, "RUN_AT_INVALID"
            ) from exc

    def _script_summary(self, summary) -> dict | None:
        if summary is None:
            return None
        try:
            allowed, reason = self._get_cron_manager()._script_authorization(
                summary.bound_umo
            )
        except Exception:  # noqa: BLE001
            allowed, reason = False, "SCRIPT_TASKS_DISABLED"
        return {
            "language_version": summary.language_version,
            "bound_umo": summary.bound_umo,
            "execution_authorization": {
                "allowed": allowed,
                "reason": reason,
            },
        }

    def _script_summary_for(self, aggregate) -> dict:
        cron_mgr = self._get_cron_manager()
        try:
            allowed, reason = cron_mgr._script_authorization(aggregate.script.bound_umo)
        except Exception:  # noqa: BLE001
            allowed, reason = False, "SCRIPT_TASKS_DISABLED"
        return {
            "language_version": aggregate.script.language_version,
            "bound_umo": aggregate.script.bound_umo,
            "execution_authorization": {
                "allowed": allowed,
                "reason": reason,
            },
        }

    @staticmethod
    def _script_detail(aggregate) -> dict:
        return {
            "source": aggregate.script.source,
            "language_version": aggregate.script.language_version,
            "bound_umo": aggregate.script.bound_umo,
            "state": aggregate.script.state or {},
            "creator_sender_id": aggregate.script.creator_sender_id,
        }
