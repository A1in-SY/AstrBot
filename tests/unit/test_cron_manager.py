"""Tests for CronJobManager with script task support."""

from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from astrbot.core.cron.manager import (
    CronJobAlreadyRunningError,
    CronJobManager,
    CronJobSchedulingError,
    CronScriptNotAuthorizedError,
    CronScriptValidationError,
    _normalize_crontab_day_of_week,
)
from astrbot.core.db.sqlite import SQLiteDatabase
from astrbot.script_runtime.validator import compute_source_hash

pytestmark = pytest.mark.asyncio


def _script_config(allowed: list[str] | None = None) -> dict:
    return {
        "script_task": {
            "enabled": True,
            "allowed_umos": allowed
            if allowed is not None
            else ["test:GroupMessage:g1"],
            "execution_timeout_seconds": 5,
            "max_source_bytes": 65536,
            "max_ast_nodes": 10000,
            "max_ast_depth": 100,
        }
    }


class FakeContext:
    def __init__(self, config: dict | None = None) -> None:
        self.config = config or _script_config()
        self.sent: list[str] = []

    def get_config(self, umo: str | None = None) -> dict:
        return self.config

    async def send_message(self, session, chain) -> bool:
        self.sent.append(str(chain))
        return True


@pytest_asyncio.fixture
async def db():
    database = SQLiteDatabase(tempfile.mktemp(suffix=".db"))
    await database.initialize()
    yield database
    await database.engine.dispose()


@pytest.fixture
def context():
    return FakeContext()


@pytest_asyncio.fixture
async def manager(db, context):
    mgr = CronJobManager(db)
    await mgr.start(context)
    yield mgr
    await mgr.shutdown()


def _source(send_text: str = "hello") -> str:
    return (
        "price = 3899\n"
        "if price < 3900:\n"
        f"    await ctx.send_text('{send_text}')\n"
        "    ctx.state['last'] = str(price)\n"
    )


async def _add_script(mgr, *, bound_umo: str = "test:GroupMessage:g1", **kwargs):
    return await mgr.add_script_job(
        name=kwargs.get("name", "gold"),
        cron_expression=kwargs.get("cron_expression", "*/5 * * * *"),
        source=kwargs.get("source", _source()),
        bound_umo=bound_umo,
        timezone=kwargs.get("timezone", "Asia/Shanghai"),
        enabled=kwargs.get("enabled", True),
        run_once=kwargs.get("run_once", False),
        run_at=kwargs.get("run_at"),
        creator_sender_id=kwargs.get("creator_sender_id", "s1"),
    )


async def _wait_for_job_status(db, job_id: str, statuses: set[str], timeout=3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        job = await db.get_cron_job(job_id)
        if job is None or job.status in statuses:
            return job
        if asyncio.get_running_loop().time() >= deadline:
            return job
        await asyncio.sleep(0.05)


class TestInit:
    @pytest.mark.asyncio
    async def test_init(self, db):
        mgr = CronJobManager(db)
        assert mgr._basic_handlers == {}
        assert mgr._started is False
        assert mgr._db_synced is False


class TestStart:
    async def test_start_marks_stale_running_interrupted(self, db, context):
        await db.create_cron_job(
            name="stale",
            job_type="basic",
            cron_expression="* * * * *",
            status="running",
        )
        mgr = CronJobManager(db)
        with patch.object(
            mgr.db,
            "mark_running_cron_jobs_interrupted",
            wraps=mgr.db.mark_running_cron_jobs_interrupted,
        ) as marked:
            await mgr.start(context)
            marked.assert_awaited_once()
        jobs = await db.list_cron_jobs()
        job = await db.get_cron_job(jobs[0].job_id)
        assert job.status == "failed"
        assert "Interrupted by AstrBot restart" in (job.last_error or "")
        await mgr.shutdown()

    async def test_start_idempotent(self, db, context):
        mgr = CronJobManager(db)
        await mgr.start(context)
        await mgr.start(context)
        assert mgr._db_synced is True
        await mgr.shutdown()

    async def test_start_resyncs_after_shutdown(self, db, context):
        mgr = CronJobManager(db)
        await mgr.start(context)
        await mgr.shutdown()
        await mgr.start(context)
        assert mgr._db_synced is True
        await mgr.shutdown()

    async def test_start_marks_missing_script_child_failed(self, db, context):
        job = await db.create_cron_job(
            name="orphan",
            job_type="script",
            cron_expression="*/5 * * * *",
            enabled=True,
        )
        mgr = CronJobManager(db)
        await mgr.start(context)
        updated = await db.get_cron_job(job.job_id)
        assert updated.status == "failed"
        assert updated.next_run_time is None
        await mgr.shutdown()

    async def test_start_unknown_language_version_blocked(self, db, context):
        job_id = "job-unknown-version"
        await db.create_script_cron_job(
            name="bad",
            cron_expression="*/5 * * * *",
            timezone=None,
            payload={},
            description=None,
            enabled=True,
            run_once=False,
            job_id=job_id,
            source="x = 1",
            source_hash=compute_source_hash("x = 1"),
            language_version="astrbot-python-subset/v999",
            bound_umo="test:GroupMessage:g1",
            creator_sender_id=None,
        )
        mgr = CronJobManager(db)
        await mgr.start(context)
        updated = await db.get_cron_job(job_id)
        assert updated.status == "unsupported_language_version"
        assert updated.next_run_time is None
        await mgr.shutdown()


class TestCreate:
    async def test_add_basic_job(self, db, context):
        mgr = CronJobManager(db)
        await mgr.start(context)
        job = await mgr.add_basic_job(
            name="basic",
            cron_expression="0 9 * * *",
            handler=MagicMock(),
            enabled=True,
        )
        assert mgr.scheduler.get_job(job.job_id) is not None
        await mgr.shutdown()

    async def test_add_script_job_creates_both_rows(self, manager, db):
        aggregate = await _add_script(manager)
        job = await db.get_cron_job(aggregate.job.job_id)
        script = await db.get_script_cron_job(aggregate.job.job_id)
        assert job.job_type == "script"
        assert script.script.bound_umo == "test:GroupMessage:g1"
        assert manager.scheduler.get_job(aggregate.job.job_id) is not None

    async def test_add_script_job_invalid_source_raises(self, manager):
        with pytest.raises(CronScriptValidationError):
            await manager.add_script_job(
                name="bad",
                cron_expression="*/5 * * * *",
                source="def x(:\n",
                bound_umo="test:GroupMessage:g1",
            )

    async def test_add_script_job_schedule_failure_removes_rows(self, manager, db):
        with patch.object(manager.scheduler, "add_job", side_effect=ValueError("boom")):
            with pytest.raises(CronJobSchedulingError):
                await _add_script(manager)
        jobs = await db.list_cron_jobs("script")
        assert jobs == []


class TestRunScript:
    async def test_run_now_sends_and_commits_state(self, manager, db, context):
        aggregate = await _add_script(manager)
        await manager.run_job_now(aggregate.job.job_id)
        job = await _wait_for_job_status(
            db, aggregate.job.job_id, {"completed", "failed"}
        )
        script = await db.get_script_cron_job(aggregate.job.job_id)
        assert job.status == "completed"
        assert script.script.state == {"last": "3899"}
        assert context.sent and "hello" in context.sent[0]

    async def test_run_now_conflict_rejected(self, manager):
        slow = "await ctx.send_text('x')\nwhile True:\n    pass\n"
        aggregate = await manager.add_script_job(
            name="slow",
            cron_expression="*/5 * * * *",
            source=slow,
            bound_umo="test:GroupMessage:g1",
        )
        task = asyncio.create_task(manager.run_job_now(aggregate.job.job_id))
        await asyncio.sleep(0.2)
        with pytest.raises(CronJobAlreadyRunningError):
            await manager.run_job_now(aggregate.job.job_id)
        await asyncio.sleep(5.5)
        await task

    async def test_scheduled_fire_overlap_skips(self, manager):
        aggregate = await manager.add_script_job(
            name="slow",
            cron_expression="*/5 * * * *",
            source="await ctx.send_text('x')\nwhile True:\n    pass\n",
            bound_umo="test:GroupMessage:g1",
        )
        task = asyncio.create_task(manager.run_job_now(aggregate.job.job_id))
        await asyncio.sleep(0.2)
        await manager._run_job(aggregate.job.job_id)
        await asyncio.sleep(0.2)
        assert len(manager.script_supervisor._procs) == 1
        await asyncio.sleep(5.5)
        await task

    async def test_different_jobs_run_concurrently(self, manager, db, context):
        agg1 = await _add_script(manager, name="a")
        agg2 = await _add_script(manager, name="b")
        await manager.run_job_now(agg1.job.job_id)
        await manager.run_job_now(agg2.job.job_id)
        await asyncio.sleep(0.5)
        jobs = {j.job_id: j.status for j in await db.list_cron_jobs("script")}
        assert jobs[agg1.job.job_id] == "completed"
        assert jobs[agg2.job.job_id] == "completed"

    async def test_scheduled_fire_allowlist_denied_skips(self, manager, db, context):
        context.config = _script_config(allowed=["other:GroupMessage:g9"])
        aggregate = await _add_script(manager)
        await manager._run_job(aggregate.job.job_id)
        await asyncio.sleep(0.2)
        job = await db.get_cron_job(aggregate.job.job_id)
        assert job.status == "scheduled"
        assert job.last_run_at is None
        assert context.sent == []

    async def test_manual_run_allowlist_denied_raises(self, manager):
        aggregate = await _add_script(manager)
        manager.ctx.config = _script_config(allowed=["other:GroupMessage:g9"])
        with pytest.raises(CronScriptNotAuthorizedError):
            await manager.run_job_now(aggregate.job.job_id)

    async def test_scheduled_run_once_deletes_after_run(self, manager, db):
        run_at = datetime.now(timezone.utc) + timedelta(days=1)
        aggregate = await _add_script(
            manager,
            run_once=True,
            run_at=run_at,
            cron_expression=None,
        )
        await manager._run_job(aggregate.job.job_id)
        await asyncio.sleep(0.3)
        assert await db.get_cron_job(aggregate.job.job_id) is None

    async def test_run_once_changed_to_recurring_during_run_is_not_deleted(
        self, manager, db, context
    ):
        run_at = datetime.now(timezone.utc) + timedelta(days=1)
        aggregate = await _add_script(
            manager,
            run_once=True,
            run_at=run_at,
            cron_expression=None,
            source="await ctx.send_text('continue')\n",
        )
        job_id = aggregate.job.job_id
        original = context.send_message

        async def send_message(session, chain):
            await manager.update_job(
                job_id,
                run_once=False,
                cron_expression="*/5 * * * *",
                payload={},
            )
            return await original(session, chain)

        context.send_message = send_message
        await manager._run_job(job_id)
        await asyncio.sleep(0.3)

        job = await db.get_cron_job(job_id)
        assert job is not None
        assert job.run_once is False
        assert job.cron_expression == "*/5 * * * *"

    async def test_manual_run_now_does_not_delete_run_once(self, manager, db):
        run_at = datetime.now(timezone.utc) + timedelta(days=1)
        aggregate = await _add_script(
            manager,
            run_once=True,
            run_at=run_at,
            cron_expression=None,
        )
        await manager.run_job_now(aggregate.job.job_id)
        await asyncio.sleep(0.3)
        job = await db.get_cron_job(aggregate.job.job_id)
        assert job is not None
        assert job.status == "completed"

    async def test_hash_mismatch_fails_without_worker(self, manager, db):
        aggregate = await _add_script(manager)
        await db.update_script_cron_job(
            aggregate.job.job_id,
            source_hash=compute_source_hash("different source"),
        )
        with patch.object(manager.script_supervisor, "execute") as execute:
            await manager.run_job_now(aggregate.job.job_id)
            await asyncio.sleep(0.2)
            execute.assert_not_awaited()
        job = await db.get_cron_job(aggregate.job.job_id)
        assert job.status == "failed"
        assert "hash" in (job.last_error or "")

    async def test_state_cas_false_still_completed(self, manager, db, context):
        aggregate = await manager.add_script_job(
            name="cas",
            cron_expression="*/5 * * * *",
            source="await ctx.send_text('x')\nctx.state['n'] = 1\n",
            bound_umo="test:GroupMessage:g1",
        )
        job_id = aggregate.job.job_id
        original = context.send_message

        async def send_message(session, chain):
            await db.update_script_cron_job(
                job_id,
                bound_umo="other:GroupMessage:g9",
            )
            return await original(session, chain)

        context.send_message = send_message
        await manager.run_job_now(job_id)
        await asyncio.sleep(0.4)
        job = await db.get_cron_job(aggregate.job.job_id)
        assert job.status == "completed"
        script = await db.get_script_cron_job(aggregate.job.job_id)
        assert script.script.state == {}

    async def test_timeout_marks_failed(self, manager, db):
        aggregate = await manager.add_script_job(
            name="spin",
            cron_expression="*/5 * * * *",
            source="while True:\n    pass\n",
            bound_umo="test:GroupMessage:g1",
        )
        await manager.run_job_now(aggregate.job.job_id)
        await asyncio.sleep(5.5)
        job = await db.get_cron_job(aggregate.job.job_id)
        assert job.status == "failed"
        assert "deadline" in (job.last_error or "")

    async def test_script_run_makes_no_llm_calls(self, manager, db):
        aggregate = await _add_script(manager)
        with patch.object(manager, "_woke_main_agent", new_callable=AsyncMock) as woke:
            await manager.run_job_now(aggregate.job.job_id)
            job = await _wait_for_job_status(
                db, aggregate.job.job_id, {"completed", "failed"}
            )
            woke.assert_not_awaited()
        assert job.status == "completed"

    async def test_gold_price_dedupe_second_run_does_not_resend(
        self, manager, db, context
    ):
        source = (
            "price = 3888\n"
            "if price < 3900:\n"
            "    last = ctx.state.get('last_alert_price')\n"
            "    if last != str(price):\n"
            "        await ctx.send_text('low')\n"
            "        ctx.state['last_alert_price'] = str(price)\n"
        )
        aggregate = await manager.add_script_job(
            name="gold",
            cron_expression="*/5 * * * *",
            source=source,
            bound_umo="test:GroupMessage:g1",
        )
        await manager.run_job_now(aggregate.job.job_id)
        await asyncio.sleep(0.3)
        await manager.run_job_now(aggregate.job.job_id)
        await asyncio.sleep(0.3)
        assert len(context.sent) == 1
        script = await db.get_script_cron_job(aggregate.job.job_id)
        assert script.script.state == {"last_alert_price": "3888"}


class TestUpdate:
    async def test_update_script_keeps_state(self, manager, db):
        aggregate = await _add_script(manager)
        await manager.run_job_now(aggregate.job.job_id)
        await asyncio.sleep(0.3)
        updated = await manager.update_job(aggregate.job.job_id, name="gold2")
        assert updated.name == "gold2"
        script = await db.get_script_cron_job(aggregate.job.job_id)
        assert script.script.state == {"last": "3899"}

    async def test_update_script_invalid_source_keeps_old(self, manager, db):
        aggregate = await _add_script(manager)
        with pytest.raises(CronScriptValidationError):
            await manager.update_job(aggregate.job.job_id, source="def broken(:\n")
        job = await db.get_cron_job(aggregate.job.job_id)
        assert job.name == "gold"
        assert manager.scheduler.get_job(aggregate.job.job_id) is not None

    async def test_update_schedule_failure_restores_old(self, manager, db):
        aggregate = await _add_script(manager)
        calls = {"n": 0}
        real_add_job = manager.scheduler.add_job

        def flaky_add_job(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("boom")
            return real_add_job(*args, **kwargs)

        with patch.object(manager.scheduler, "add_job", side_effect=flaky_add_job):
            with pytest.raises(CronJobSchedulingError):
                await manager.update_job(aggregate.job.job_id, name="gold2")
        job = await db.get_cron_job(aggregate.job.job_id)
        assert job.name == "gold"
        assert manager.scheduler.get_job(aggregate.job.job_id) is not None

    async def test_update_db_failure_restores_old_schedule(self, manager, db):
        aggregate = await _add_script(manager)
        original = db.update_script_cron_job

        async def failing_update(job_id: str, **kwargs):
            raise RuntimeError("db down")

        db.update_script_cron_job = failing_update
        with pytest.raises(RuntimeError):
            await manager.update_job(aggregate.job.job_id, name="gold2")
        db.update_script_cron_job = original
        job = await db.get_cron_job(aggregate.job.job_id)
        assert job.name == "gold"
        assert manager.scheduler.get_job(aggregate.job.job_id) is not None

    async def test_delete_restores_schedule_on_db_failure(self, manager, db):
        aggregate = await _add_script(manager)
        original = db.delete_cron_job

        async def failing_delete(job_id: str) -> None:
            raise RuntimeError("db down")

        db.delete_cron_job = failing_delete
        with pytest.raises(RuntimeError):
            await manager.delete_job(aggregate.job.job_id)
        db.delete_cron_job = original
        assert manager.scheduler.get_job(aggregate.job.job_id) is not None


class TestShutdown:
    async def test_shutdown_kills_workers(self, manager, db):
        aggregate = await manager.add_script_job(
            name="spin",
            cron_expression="*/5 * * * *",
            source="while True:\n    pass\n",
            bound_umo="test:GroupMessage:g1",
        )
        task = asyncio.create_task(manager.run_job_now(aggregate.job.job_id))
        await asyncio.sleep(0.2)
        await manager.shutdown()
        await asyncio.sleep(0.2)
        assert task.done()
        assert not manager.script_supervisor._procs

    async def test_shutdown_when_not_started(self, db, context):
        mgr = CronJobManager(db)
        await mgr.shutdown()


class TestNormalizeWeekday:
    async def test_numeric_weekday(self):
        assert _normalize_crontab_day_of_week("0") == "sun"
        assert _normalize_crontab_day_of_week("7") == "sun"
        assert _normalize_crontab_day_of_week("1-5") == "mon,tue,wed,thu,fri"


class TestNextRunTime:
    async def test_next_run_time_persisted(self, manager, db):
        aggregate = await _add_script(manager)
        job = await db.get_cron_job(aggregate.job.job_id)
        assert job.next_run_time is not None
