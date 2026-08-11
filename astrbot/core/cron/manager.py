import asyncio
import json
import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from astrbot import logger
from astrbot.core.agent.tool import ToolSet
from astrbot.core.cron.events import CronMessageEvent
from astrbot.core.cron.script_supervisor import (
    ScriptRunRequest,
    ScriptSupervisor,
    build_proxy_snapshot,
)
from astrbot.core.db import BaseDatabase
from astrbot.core.db.po import CronJob, CronScriptJob
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType
from astrbot.core.provider.entites import ProviderRequest
from astrbot.core.utils.history_saver import persist_agent_history
from astrbot.script_runtime import spec
from astrbot.script_runtime.diagnostics import ValidationResult
from astrbot.script_runtime.errors import SendError, SendTargetUnavailableError
from astrbot.script_runtime.validator import compute_source_hash

if TYPE_CHECKING:
    from astrbot.core.star.context import Context


_CRONTAB_WEEKDAY_NAMES = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
_CRONTAB_WEEKDAY_PATTERN = re.compile(r"^(?:(\*)|(\d+)(?:-(\d+))?)(?:/(\d+))?$")


def _normalize_crontab_day_of_week(day_of_week: str) -> str:
    """Normalize standard crontab weekdays for APScheduler."""
    normalized_parts: list[str] = []
    for raw_part in day_of_week.split(","):
        part = raw_part.strip().lower()
        match = _CRONTAB_WEEKDAY_PATTERN.fullmatch(part)
        if not match:
            normalized_parts.append(part)
            continue
        wildcard, start_text, end_text, step_text = match.groups()
        step = int(step_text or "1")
        if step < 1:
            raise ValueError("day_of_week step must be greater than 0")
        if wildcard:
            if step == 1:
                normalized_parts.append("*")
                continue
            values = range(0, 7, step)
        else:
            start = int(start_text)
            end = int(end_text) if end_text is not None else None
            if start < 0 or start > 7 or (end is not None and (end < 0 or end > 7)):
                raise ValueError("day_of_week values must be between 0 and 7")
            if end is not None and start > end:
                raise ValueError("day_of_week range start must not exceed end")
            if end is None:
                end = 7 if step_text else start
            values = range(start, end + 1, step)
        weekdays: list[int] = []
        for value in values:
            weekday = 0 if value == 7 else value
            if weekday not in weekdays:
                weekdays.append(weekday)
        if len(weekdays) == 7:
            normalized_parts.append("*")
        else:
            normalized_parts.extend(_CRONTAB_WEEKDAY_NAMES[value] for value in weekdays)
    return ",".join(normalized_parts)


class CronJobSchedulingError(Exception):
    """Raised when a cron job cannot be scheduled or its schedule is invalid."""


class CronJobNotFoundError(Exception):
    """Raised when an operation targets a missing cron job."""


class CronJobAlreadyRunningError(Exception):
    """Raised when a manual run is requested for an already-running job."""


class CronJobShuttingDownError(Exception):
    """Raised when admission is attempted during shutdown."""


class CronScriptNotAuthorizedError(Exception):
    """Raised when a manual script run is blocked by the UMO allowlist."""


class CronScriptDefinitionError(Exception):
    """Raised when a script task has an integrity or definition problem."""


class CronScriptValidationError(Exception):
    """Raised when a script task creation/edit fails static validation."""

    def __init__(self, validation: ValidationResult) -> None:
        self.validation = validation
        super().__init__("script source failed static validation")


class CronJobManager:
    """Central scheduler for basic, active-agent and script cron jobs."""

    def __init__(self, db: BaseDatabase) -> None:
        self.db = db
        self.scheduler = AsyncIOScheduler()
        self._basic_handlers: dict[str, Callable[..., Any]] = {}
        self._mutation_lock = asyncio.Lock()
        self._run_claim_lock = asyncio.Lock()
        self._running_job_ids: set[str] = set()
        self._active_run_tasks: set[asyncio.Task] = set()
        self._started = False
        self._db_synced = False
        self._shutting_down = False
        self.ctx: Context | None = None
        self.script_supervisor = ScriptSupervisor()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self, ctx: "Context") -> None:
        self.ctx = ctx
        async with self._mutation_lock:
            if self._db_synced:
                return
            await self.db.mark_running_cron_jobs_interrupted(
                "Interrupted by AstrBot restart before completion."
            )
            if not self._started:
                self.scheduler.start()
                self._started = True
            await self.sync_from_db()
            self._db_synced = True

    async def shutdown(self) -> None:
        async with self._mutation_lock:
            if not self._started and not self._active_run_tasks:
                self._db_synced = False
                return
            self._shutting_down = True
            try:
                self.scheduler.shutdown(wait=False)
            finally:
                self._started = False
        await self.script_supervisor.shutdown()
        tasks = list(self._active_run_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._running_job_ids.clear()
        self._active_run_tasks.clear()
        self._db_synced = False
        self._shutting_down = False

    async def sync_from_db(self) -> None:
        jobs = await self.db.list_cron_jobs()
        summaries = {
            summary.job.job_id: summary
            for summary in await self.db.list_script_cron_job_summaries()
        }
        for job in jobs:
            if not job.enabled or not job.persistent:
                continue
            if job.job_type == "basic" and job.job_id not in self._basic_handlers:
                logger.warning(
                    "Skip scheduling basic cron job %s due to missing handler.",
                    job.job_id,
                )
                continue
            if job.job_type == "script":
                summary = summaries.get(job.job_id)
                if summary is None:
                    await self.db.update_cron_job(
                        job.job_id,
                        status="failed",
                        next_run_time=None,
                        last_error=(
                            "Script task definition row is missing; re-create or "
                            "delete the job."
                        ),
                    )
                    continue
                if summary.language_version not in spec.SUPPORTED_LANGUAGE_VERSIONS:
                    await self.db.update_cron_job(
                        job.job_id,
                        status="unsupported_language_version",
                        next_run_time=None,
                        last_error=(
                            "Unsupported script language version "
                            f"{summary.language_version!r}; migrate from the Dashboard."
                        ),
                    )
                    continue
            try:
                await self._schedule_job(job)
            except CronJobSchedulingError:
                continue  # Error already logged in _schedule_job

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    async def add_basic_job(
        self,
        *,
        name: str,
        cron_expression: str,
        handler: Callable[..., Any | Awaitable[Any]],
        description: str | None = None,
        timezone: str | None = None,
        payload: dict | None = None,
        enabled: bool = True,
        persistent: bool = False,
    ) -> CronJob:
        job = await self.db.create_cron_job(
            name=name,
            job_type="basic",
            cron_expression=cron_expression,
            timezone=timezone,
            payload=payload or {},
            description=description,
            enabled=enabled,
            persistent=persistent,
        )
        self._basic_handlers[job.job_id] = handler
        if enabled:
            async with self._mutation_lock:
                await self._schedule_job(job)
        return job

    async def add_active_job(
        self,
        *,
        name: str,
        cron_expression: str | None,
        payload: dict,
        description: str | None = None,
        timezone: str | None = None,
        enabled: bool = True,
        persistent: bool = True,
        run_once: bool = False,
        run_at: datetime | None = None,
    ) -> CronJob:
        if run_once and run_at:
            payload = {**payload, "run_at": run_at.isoformat()}
        job = await self.db.create_cron_job(
            name=name,
            job_type="active_agent",
            cron_expression=cron_expression,
            timezone=timezone,
            payload=payload,
            description=description,
            enabled=enabled,
            persistent=persistent,
            run_once=run_once,
        )
        if enabled:
            async with self._mutation_lock:
                await self._schedule_job(job)
        return job

    async def add_script_job(
        self,
        *,
        name: str,
        cron_expression: str | None,
        source: str,
        bound_umo: str,
        description: str | None = None,
        timezone: str | None = None,
        enabled: bool = True,
        run_once: bool = False,
        run_at: datetime | None = None,
        creator_sender_id: str | None = None,
        language_version: str = spec.DEFAULT_LANGUAGE_VERSION,
    ) -> CronScriptJob:
        if run_once and not run_at:
            raise CronJobSchedulingError("run_at is required when run_once=true")
        if (not run_once) and not cron_expression:
            raise CronJobSchedulingError(
                "cron_expression is required when run_once=false"
            )
        validation = await self.validate_script_source(source, language_version)
        if not validation.valid:
            raise CronScriptValidationError(validation)
        job_id = str(uuid.uuid4())
        payload = {"run_at": run_at.isoformat()} if (run_once and run_at) else {}
        aggregate = await self.db.create_script_cron_job(
            name=name,
            cron_expression=cron_expression,
            timezone=timezone,
            payload=payload,
            description=description,
            enabled=enabled,
            run_once=run_once,
            job_id=job_id,
            source=source,
            source_hash=compute_source_hash(source),
            language_version=language_version,
            bound_umo=bound_umo,
            creator_sender_id=creator_sender_id,
        )
        if enabled:
            try:
                async with self._mutation_lock:
                    await self._schedule_job(aggregate.job)
            except CronJobSchedulingError:
                self._remove_scheduled(job_id)
                await self.db.delete_cron_job(job_id)
                raise
        else:
            await self.db.update_cron_job(job_id, next_run_time=None)
        return aggregate

    async def validate_script_source(
        self,
        source: str,
        language_version: str = spec.DEFAULT_LANGUAGE_VERSION,
    ) -> ValidationResult:
        return await self.script_supervisor.validate(
            source,
            language_version=language_version,
            limits=self._script_limits(),
        )

    # ------------------------------------------------------------------
    # Update / delete
    # ------------------------------------------------------------------

    async def update_job(self, job_id: str, **kwargs) -> CronJob | None:
        async with self._mutation_lock:
            job = await self.db.get_cron_job(job_id)
            if job is None:
                return None
            if job.job_type == "script":
                return await self._update_script_job_locked(job, kwargs)
            return await self._update_generic_job_locked(job, kwargs)

    async def _update_generic_job_locked(
        self,
        old_job: CronJob,
        kwargs: dict[str, Any],
    ) -> CronJob | None:
        candidate = self._merged_job(old_job, kwargs)
        try:
            self._build_trigger(candidate)
        except (ValueError, TypeError) as exc:
            raise CronJobSchedulingError(str(exc)) from exc
        self._remove_scheduled(old_job.job_id)
        try:
            updated = await self.db.update_cron_job(old_job.job_id, **kwargs)
        except Exception:
            await self._restore_schedule(old_job)
            raise
        if updated is None:
            await self._restore_schedule(old_job)
            return None
        try:
            if updated.enabled:
                await self._schedule_job(updated)
            else:
                await self.db.update_cron_job(updated.job_id, next_run_time=None)
        except CronJobSchedulingError:
            await self._restore_definition_fields(old_job, kwargs)
            await self._restore_schedule(old_job)
            raise
        return updated

    async def _update_script_job_locked(
        self,
        old_job: CronJob,
        kwargs: dict[str, Any],
    ) -> CronJob | None:
        old_aggregate = await self.db.get_script_cron_job(old_job.job_id)
        if old_aggregate is None:
            raise CronScriptDefinitionError("script task definition row is missing")
        source = kwargs.get("source", old_aggregate.script.source)
        language_version = kwargs.get(
            "language_version", old_aggregate.script.language_version
        )
        if "source" in kwargs or "language_version" in kwargs:
            validation = await self.validate_script_source(
                str(source), str(language_version)
            )
            if not validation.valid:
                raise CronScriptValidationError(validation)
        source_hash = compute_source_hash(str(source))
        bound_umo = kwargs.get("bound_umo", old_aggregate.script.bound_umo)
        candidate = self._merged_job(old_job, kwargs)
        try:
            self._build_trigger(candidate)
        except (ValueError, TypeError) as exc:
            raise CronJobSchedulingError(str(exc)) from exc
        self._remove_scheduled(old_job.job_id)
        try:
            script_kwargs = {
                key: value
                for key, value in kwargs.items()
                if key
                in {
                    "name",
                    "cron_expression",
                    "timezone",
                    "payload",
                    "description",
                    "enabled",
                    "run_once",
                    "status",
                    "next_run_time",
                    "last_run_at",
                    "last_error",
                }
            }
            updated = await self.db.update_script_cron_job(
                old_job.job_id,
                **script_kwargs,
                source=source,
                source_hash=source_hash,
                language_version=language_version,
                bound_umo=bound_umo,
            )
        except Exception:
            await self._restore_schedule(old_job)
            raise
        if updated is None:
            await self._restore_schedule(old_job)
            return None
        try:
            if updated.job.enabled:
                await self._schedule_job(updated.job)
            else:
                await self.db.update_cron_job(updated.job.job_id, next_run_time=None)
        except CronJobSchedulingError:
            await self._restore_script_definition(old_aggregate, kwargs)
            await self._restore_schedule(old_job)
            raise
        return updated.job

    async def delete_job(self, job_id: str) -> None:
        async with self._mutation_lock:
            job = await self.db.get_cron_job(job_id)
            if job is not None:
                self._remove_scheduled(job_id)
                self._basic_handlers.pop(job_id, None)
            try:
                await self.db.delete_cron_job(job_id)
            except Exception:
                if job is not None:
                    await self._restore_schedule(job)
                raise

    async def reset_script_cron_state(self, job_id: str) -> CronScriptJob:
        async with self._run_claim_lock:
            if job_id in self._running_job_ids:
                raise CronJobAlreadyRunningError(job_id)
            ok = await self.db.reset_script_cron_state(job_id)
            if not ok:
                raise CronJobNotFoundError(job_id)
            aggregate = await self.db.get_script_cron_job(job_id)
            if aggregate is None:
                raise CronJobNotFoundError(job_id)
            return aggregate

    async def list_jobs(self, job_type: str | None = None) -> list[CronJob]:
        return await self.db.list_cron_jobs(job_type)

    async def get_script_job(self, job_id: str) -> CronScriptJob | None:
        return await self.db.get_script_cron_job(job_id)

    # ------------------------------------------------------------------
    # Scheduling internals
    # ------------------------------------------------------------------

    def _remove_scheduled(self, job_id: str) -> None:
        if self.scheduler.get_job(job_id):
            self.scheduler.remove_job(job_id)

    async def _schedule_job(self, job: CronJob) -> None:
        if not self._started:
            self.scheduler.start()
            self._started = True
        try:
            trigger = self._build_trigger(job)
            self.scheduler.add_job(
                self._run_job,
                id=job.job_id,
                trigger=trigger,
                args=[job.job_id],
                replace_existing=True,
                misfire_grace_time=30,
                max_instances=1,
            )
        except (ValueError, TypeError) as exc:
            logger.exception("Failed to schedule cron job %s", job.job_id)
            raise CronJobSchedulingError(str(exc)) from exc
        next_run = self._get_next_run_time(job.job_id)
        await self.db.update_cron_job(job.job_id, next_run_time=next_run)

    def _build_trigger(self, job: CronJob) -> CronTrigger | DateTrigger:
        tzinfo = None
        if job.timezone:
            try:
                tzinfo = ZoneInfo(job.timezone)
            except Exception:
                logger.warning(
                    "Invalid timezone %s for cron job %s, fallback to system.",
                    job.timezone,
                    job.job_id,
                )
        if job.run_once:
            run_at_str = None
            if isinstance(job.payload, dict):
                run_at_str = job.payload.get("run_at")
            run_at_str = run_at_str or job.cron_expression
            if not run_at_str:
                raise ValueError("run_once job missing run_at timestamp")
            run_at = datetime.fromisoformat(run_at_str)
            if run_at.tzinfo is None and tzinfo is not None:
                run_at = run_at.replace(tzinfo=tzinfo)
            return DateTrigger(run_date=run_at, timezone=tzinfo)
        if not job.cron_expression:
            raise ValueError("recurring job missing cron_expression")
        minute, hour, day, month, day_of_week = job.cron_expression.split()
        normalized = " ".join(
            [
                minute,
                hour,
                day,
                month,
                _normalize_crontab_day_of_week(day_of_week),
            ]
        )
        return CronTrigger.from_crontab(normalized, timezone=tzinfo)

    def _get_next_run_time(self, job_id: str):
        aps_job = self.scheduler.get_job(job_id)
        if not aps_job or aps_job.next_run_time is None:
            return None
        return aps_job.next_run_time.astimezone(timezone.utc)

    async def _restore_schedule(self, job: CronJob) -> None:
        if job.enabled:
            try:
                await self._schedule_job(job)
            except CronJobSchedulingError:
                logger.error("Failed to restore schedule for cron job %s", job.job_id)

    async def _restore_definition_fields(
        self,
        old_job: CronJob,
        kwargs: dict[str, Any],
    ) -> None:
        restore: dict[str, Any] = {}
        for key in kwargs:
            if key in {
                "name",
                "cron_expression",
                "timezone",
                "payload",
                "description",
                "enabled",
                "run_once",
                "status",
                "next_run_time",
                "last_run_at",
                "last_error",
            }:
                restore[key] = getattr(old_job, key)
        if restore:
            await self.db.update_cron_job(old_job.job_id, **restore)

    async def _restore_script_definition(
        self,
        old_aggregate: CronScriptJob,
        kwargs: dict[str, Any],
    ) -> None:
        script = old_aggregate.script
        public: dict[str, Any] = {}
        for key in kwargs:
            if key in {
                "name",
                "cron_expression",
                "timezone",
                "payload",
                "description",
                "enabled",
                "run_once",
                "status",
                "next_run_time",
                "last_run_at",
                "last_error",
            }:
                public[key] = getattr(old_aggregate.job, key)
        await self.db.update_script_cron_job(
            old_aggregate.job.job_id,
            **public,
            source=script.source,
            source_hash=script.source_hash,
            language_version=script.language_version,
            bound_umo=script.bound_umo,
        )

    @staticmethod
    def _merged_job(old_job: CronJob, kwargs: dict[str, Any]) -> CronJob:
        candidate = CronJob(**{col: getattr(old_job, col) for col in _CRON_JOB_COLUMNS})
        for key, value in kwargs.items():
            if hasattr(candidate, key):
                setattr(candidate, key, value)
        return candidate

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def run_job_now(self, job_id: str) -> None:
        """Admit a manual run and return after the background task is created."""
        await self._admit_and_run(job_id, manual=True, delete_run_once=False)

    async def _admit_and_run(
        self,
        job_id: str,
        *,
        manual: bool,
        delete_run_once: bool,
    ) -> bool:
        snapshot = await self._admit(job_id, manual=manual)
        if snapshot is None:
            return False
        task = asyncio.create_task(
            self._run_claimed(
                job_id,
                snapshot,
                manual=manual,
                delete_run_once=delete_run_once,
            )
        )
        self._active_run_tasks.add(task)
        task.add_done_callback(self._active_run_tasks.discard)
        return True

    async def _admit(
        self,
        job_id: str,
        *,
        manual: bool,
    ) -> CronJob | CronScriptJob | None:
        async with self._run_claim_lock:
            if self._shutting_down:
                raise CronJobShuttingDownError("cron manager is shutting down")
            if job_id in self._running_job_ids:
                if manual:
                    raise CronJobAlreadyRunningError(job_id)
                return None
            job = await self.db.get_cron_job(job_id)
            if job is None:
                if manual:
                    raise CronJobNotFoundError(job_id)
                return None
            if not job.enabled and not manual:
                return None
            if job.job_type == "script":
                aggregate = await self.db.get_script_cron_job(job_id)
                if aggregate is None:
                    if manual:
                        raise CronScriptDefinitionError(
                            "script task definition row is missing"
                        )
                    return None
                authorized, reason = self._script_authorization(
                    aggregate.script.bound_umo
                )
                if not authorized:
                    if manual:
                        raise CronScriptNotAuthorizedError(reason)
                    return None
                self._running_job_ids.add(job_id)
                return aggregate
            self._running_job_ids.add(job_id)
            return job

    async def _run_job(
        self,
        job_id: str,
        *,
        ignore_enabled: bool = False,
        delete_run_once: bool = True,
    ) -> None:
        await self._admit_and_run(
            job_id,
            manual=ignore_enabled,
            delete_run_once=delete_run_once,
        )

    async def _run_claimed(
        self,
        job_id: str,
        snapshot: CronJob | CronScriptJob,
        *,
        manual: bool,
        delete_run_once: bool,
    ) -> None:
        start_time = datetime.now(timezone.utc)
        try:
            if isinstance(snapshot, CronScriptJob):
                await self._run_script_job(snapshot, start_time, delete_run_once)
            else:
                job = snapshot
                try:
                    if job.job_type == "basic":
                        await self._run_basic_job(job)
                    elif job.job_type == "active_agent":
                        await self._run_active_agent_job(job, start_time=start_time)
                    else:
                        raise ValueError(f"Unknown cron job type: {job.job_type}")
                except Exception as e:  # noqa: BLE001
                    await self._finalize_failure(
                        job_id, start_time, str(e), delete_run_once
                    )
                    return
                next_run = self._get_next_run_time(job_id)
                await self.db.update_cron_job(
                    job_id,
                    status="completed",
                    last_run_at=start_time,
                    last_error=None,
                    next_run_time=next_run,
                )
                if job.run_once and delete_run_once:
                    await self.delete_job(job_id)
        finally:
            async with self._run_claim_lock:
                self._running_job_ids.discard(job_id)

    async def _finalize_failure(
        self,
        job_id: str,
        start_time: datetime,
        error: str,
        delete_run_once: bool,
    ) -> None:
        next_run = self._get_next_run_time(job_id)
        await self.db.update_cron_job(
            job_id,
            status="failed",
            last_run_at=start_time,
            last_error=error,
            next_run_time=next_run,
        )
        job = await self.db.get_cron_job(job_id)
        if job and job.run_once and delete_run_once:
            await self.delete_job(job_id)

    async def _run_script_job(
        self,
        aggregate: CronScriptJob,
        start_time: datetime,
        delete_run_once: bool,
    ) -> None:
        job = aggregate.job
        script = aggregate.script
        job_id = job.job_id

        # Pre-execution gates (no worker, no side effects until they pass).
        if script.language_version not in spec.SUPPORTED_LANGUAGE_VERSIONS:
            await self._finalize_failure(
                job_id,
                start_time,
                f"Unsupported script language version {script.language_version!r}",
                delete_run_once,
            )
            return
        if compute_source_hash(script.source) != script.source_hash:
            await self._finalize_failure(
                job_id,
                start_time,
                "Script source hash mismatch: definition may have been modified.",
                delete_run_once,
            )
            return
        validation = await self.validate_script_source(
            script.source, script.language_version
        )
        if not validation.valid:
            await self._finalize_failure(
                job_id,
                start_time,
                "Script source failed static validation before execution.",
                delete_run_once,
            )
            return
        try:
            session = MessageSession.from_str(script.bound_umo)
        except Exception as exc:  # noqa: BLE001
            await self._finalize_failure(
                job_id,
                start_time,
                f"Invalid bound session {script.bound_umo!r}: {exc}",
                delete_run_once,
            )
            return

        await self.db.update_cron_job(
            job_id,
            status="running",
            last_run_at=start_time,
            last_error=None,
        )

        async def send_handler(text: str) -> None:
            if self.ctx is None:
                raise SendTargetUnavailableError("context is not available")
            from astrbot.core.message.components import Plain
            from astrbot.core.message.message_event_result import MessageChain

            try:
                ok = await self.ctx.send_message(
                    session,
                    MessageChain([Plain(text)]),
                )
            except Exception as exc:  # noqa: BLE001
                raise SendError(str(exc)) from exc
            if not ok:
                raise SendTargetUnavailableError(
                    "no platform adapter matched the bound session"
                )

        request = ScriptRunRequest(
            job_id=job_id,
            run_id=str(uuid.uuid4()),
            source=script.source,
            source_hash=script.source_hash,
            language_version=script.language_version,
            initial_state=dict(script.state or {}),
            run_started_at=start_time.isoformat(),
            run_timezone=job.timezone or "system",
            limits=self._script_limits(),
            proxy_snapshot=build_proxy_snapshot(self._global_config()),
        )
        result = await self.script_supervisor.execute(
            request,
            send_handler=send_handler,
        )
        if result.worker_stderr_tail:
            logger.error(
                "Script cron job %s run %s stderr: %s",
                job_id,
                request.run_id,
                result.worker_stderr_tail[:2000],
            )
        next_run = self._get_next_run_time(job_id)
        if result.success:
            committed = await self.db.commit_script_cron_state(
                job_id,
                result.state or {},
                expected_source_hash=script.source_hash,
                expected_bound_umo=script.bound_umo,
                expected_language_version=script.language_version,
            )
            if not committed:
                logger.info(
                    "Script cron job %s completed but state was discarded "
                    "(definition changed or job deleted).",
                    job_id,
                )
            await self.db.update_cron_job(
                job_id,
                status="completed",
                last_run_at=start_time,
                last_error=None,
                next_run_time=next_run,
            )
        else:
            await self.db.update_cron_job(
                job_id,
                status="failed",
                last_run_at=start_time,
                last_error=result.message or result.error_code,
                next_run_time=next_run,
            )
        if job.run_once and delete_run_once:
            await self.delete_job(job_id)

    # ------------------------------------------------------------------
    # Script configuration and authorization
    # ------------------------------------------------------------------

    def _global_config(self) -> dict[str, Any]:
        if self.ctx is None:
            return {}
        try:
            config = self.ctx.get_config()
            return dict(config) if isinstance(config, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def _script_config(self) -> dict[str, Any]:
        config = self._global_config()
        script_cfg = config.get("script_task") or {}
        return script_cfg if isinstance(script_cfg, dict) else {}

    def _script_limits(self) -> dict[str, int]:
        script_cfg = self._script_config()
        limits = dict(spec.DEFAULT_LIMITS)
        for key in spec.LIMIT_KEYS:
            raw = script_cfg.get(key)
            if raw is None:
                continue
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            if value > 0:
                limits[key] = value
        return limits

    def _script_authorization(self, bound_umo: str) -> tuple[bool, str | None]:
        script_cfg = self._script_config()
        if not script_cfg.get("enabled", False):
            return False, "SCRIPT_TASKS_DISABLED"
        allowed = script_cfg.get("allowed_umos") or []
        if not isinstance(allowed, list) or bound_umo not in allowed:
            return False, "BOUND_UMO_NOT_ALLOWED"
        return True, None

    # ------------------------------------------------------------------
    # Basic / active-agent internals (unchanged behavior)
    # ------------------------------------------------------------------

    async def _run_basic_job(self, job: CronJob) -> None:
        handler = self._basic_handlers.get(job.job_id)
        if not handler:
            raise RuntimeError(f"Basic cron job handler not found for {job.job_id}")
        payload = job.payload or {}
        result = handler(**payload) if payload else handler()
        if asyncio.iscoroutine(result):
            await result

    async def _run_active_agent_job(self, job: CronJob, start_time: datetime) -> None:
        payload = job.payload or {}
        delivery_session_str = str(payload.get("session") or "").strip()
        session_str = delivery_session_str or str(
            MessageSession(
                platform_name="cron",
                message_type=MessageType.OTHER_MESSAGE,
                session_id=job.job_id,
            )
        )
        note = payload.get("note") or job.description or job.name
        extras = {
            "cron_job": {
                "id": job.job_id,
                "name": job.name,
                "type": job.job_type,
                "run_once": job.run_once,
                "description": job.description,
                "note": note,
                "run_started_at": start_time.isoformat(),
                "run_at": (
                    job.payload.get("run_at") if isinstance(job.payload, dict) else None
                ),
                "session": delivery_session_str,
            },
            "cron_payload": payload,
        }
        await self._woke_main_agent(
            message=note,
            session_str=session_str,
            extras=extras,
            delivery_session_str=delivery_session_str,
        )

    async def _woke_main_agent(
        self,
        *,
        message: str,
        session_str: str,
        extras: dict,
        delivery_session_str: str = "",
    ) -> None:
        """Woke the main agent to handle the cron job message."""
        from astrbot.core.astr_main_agent import (
            MainAgentBuildConfig,
            _get_session_conv,
            build_main_agent,
        )
        from astrbot.core.astr_main_agent_resources import (
            PROACTIVE_AGENT_CRON_WOKE_SYSTEM_PROMPT,
        )
        from astrbot.core.tools.message_tools import SendMessageToUserTool

        if self.ctx is None:
            raise RuntimeError("cron context is not available")
        try:
            session = (
                session_str
                if isinstance(session_str, MessageSession)
                else MessageSession.from_str(session_str)
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"Invalid session for cron job: {e}")
            return

        cron_event = CronMessageEvent(
            context=self.ctx,
            session=session,
            message=message,
            extras=extras or {},
            message_type=session.message_type,
        )
        umo = cron_event.unified_msg_origin
        cfg = self.ctx.get_config(umo=umo)
        cron_payload = extras.get("cron_payload", {}) if extras else {}
        sender_id = cron_payload.get("sender_id")
        admin_ids = cfg.get("admins_id", [])
        if admin_ids:
            cron_event.role = "admin" if sender_id in admin_ids else "member"
        if cron_payload.get("origin", "tool") == "api":
            cron_event.role = "admin"
        provider_settings = cfg.get("provider_settings", {}) or {}
        tool_call_timeout = provider_settings.get("tool_call_timeout", 120)
        config = MainAgentBuildConfig(
            tool_call_timeout=tool_call_timeout,
            llm_safety_mode=False,
            streaming_response=False,
            provider_settings=provider_settings,
        )
        req = ProviderRequest()
        conv = await _get_session_conv(event=cron_event, plugin_context=self.ctx)
        req.conversation = conv
        context = json.loads(conv.history)
        if context:
            req.contexts = context
            context_dump = req._print_friendly_context()
            req.contexts = []
            req.system_prompt += (
                "\n\nBellow is you and user previous conversation history:\n"
                f"---\n{context_dump}\n---\n"
            )
        cron_job_str = json.dumps(extras.get("cron_job", {}), ensure_ascii=False)
        req.system_prompt += PROACTIVE_AGENT_CRON_WOKE_SYSTEM_PROMPT.format(
            cron_job=cron_job_str
        )
        req.prompt = (
            "You are now responding to a scheduled task. "
            "Proceed according to your system instructions. "
            "Output using same language as previous conversation. "
            "After completing your task, summarize and output your actions and results."
        )
        if delivery_session_str:
            if not req.func_tool:
                req.func_tool = ToolSet()
            req.func_tool.add_tool(
                self.ctx.get_llm_tool_manager().get_builtin_tool(SendMessageToUserTool)
            )
        result = await build_main_agent(
            event=cron_event,
            plugin_context=self.ctx,
            config=config,
            req=req,
        )
        if not result:
            logger.error("Failed to build main agent for cron job.")
            return
        runner = result.agent_runner
        async for _ in runner.step_until_done(30):
            pass
        llm_resp = runner.get_final_llm_resp()
        cron_meta = extras.get("cron_job", {}) if extras else {}
        summary_note = (
            f"[CronJob] {cron_meta.get('name') or cron_meta.get('id', 'unknown')}: "
            f"{cron_meta.get('description', '')} triggered at "
            f"{cron_meta.get('run_started_at', 'unknown time')}, "
        )
        if llm_resp and llm_resp.role == "assistant":
            summary_note += (
                f"I finished this job, here is the result: {llm_resp.completion_text}"
            )
        await persist_agent_history(
            self.ctx.conversation_manager,
            event=cron_event,
            req=req,
            summary_note=summary_note,
        )
        if not llm_resp:
            logger.warning("Cron job agent got no response")


_CRON_JOB_COLUMNS = (
    "id",
    "job_id",
    "name",
    "description",
    "job_type",
    "cron_expression",
    "timezone",
    "payload",
    "enabled",
    "persistent",
    "run_once",
    "status",
    "last_run_at",
    "next_run_time",
    "last_error",
)


__all__ = [
    "CronJobAlreadyRunningError",
    "CronJobManager",
    "CronJobNotFoundError",
    "CronJobSchedulingError",
    "CronJobShuttingDownError",
    "CronScriptDefinitionError",
    "CronScriptNotAuthorizedError",
    "CronScriptValidationError",
]
