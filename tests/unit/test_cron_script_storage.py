"""Storage tests for cron_script_tasks and script CAS state."""

from __future__ import annotations

import tempfile

import pytest
import pytest_asyncio

from astrbot.core.db.sqlite import SQLiteDatabase
from astrbot.script_runtime.validator import compute_source_hash

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db():
    database = SQLiteDatabase(tempfile.mktemp(suffix=".db"))
    await database.initialize()
    yield database
    await database.engine.dispose()


def _script_fields(**overrides):
    fields = {
        "name": "gold",
        "cron_expression": "*/5 * * * *",
        "timezone": "Asia/Shanghai",
        "payload": {},
        "description": None,
        "enabled": True,
        "run_once": False,
        "job_id": "job-1",
        "source": "x = 1",
        "source_hash": compute_source_hash("x = 1"),
        "language_version": "astrbot-python-subset/v1",
        "bound_umo": "test:GroupMessage:g1",
        "creator_sender_id": "s1",
    }
    fields.update(overrides)
    return fields


async def test_foreign_keys_enabled_on_every_connection(db):
    async with db.get_db() as session:
        from sqlalchemy import text

        result = await session.execute(text("PRAGMA foreign_keys"))
        assert result.scalar() == 1


async def test_create_and_get(db):
    aggregate = await db.create_script_cron_job(**_script_fields())
    assert aggregate.job.job_type == "script"
    assert aggregate.script.state == {}
    fetched = await db.get_script_cron_job("job-1")
    assert fetched.script.source == "x = 1"


async def test_delete_cascades(db):
    await db.create_script_cron_job(**_script_fields())
    await db.delete_cron_job("job-1")
    assert await db.get_script_cron_job("job-1") is None


async def test_update_preserves_state(db):
    await db.create_script_cron_job(**_script_fields())
    await db.commit_script_cron_state(
        "job-1",
        {"n": 1},
        expected_source_hash=compute_source_hash("x = 1"),
        expected_bound_umo="test:GroupMessage:g1",
        expected_language_version="astrbot-python-subset/v1",
    )
    updated = await db.update_script_cron_job(
        "job-1",
        name="gold2",
        source="y = 2",
        source_hash=compute_source_hash("y = 2"),
    )
    assert updated.job.name == "gold2"
    assert updated.script.state == {"n": 1}


async def test_create_child_failure_rolls_back_parent(db):
    with pytest.raises(Exception):
        await db.create_script_cron_job(**{**_script_fields(), "bound_umo": None})
    assert await db.get_cron_job("job-1") is None


async def test_summary_does_not_load_source(db):
    await db.create_script_cron_job(**_script_fields())
    summaries = await db.list_script_cron_job_summaries()
    assert len(summaries) == 1
    summary = summaries[0]
    assert summary.bound_umo == "test:GroupMessage:g1"
    assert not hasattr(summary, "source")


@pytest.mark.parametrize(
    "mutation",
    [
        {"expected_source_hash": "deadbeef"},
        {"expected_bound_umo": "other:GroupMessage:g9"},
        {"expected_language_version": "astrbot-python-subset/v9"},
    ],
)
async def test_state_cas_mismatch_false(db, mutation):
    await db.create_script_cron_job(**_script_fields())
    kwargs = {
        "expected_source_hash": compute_source_hash("x = 1"),
        "expected_bound_umo": "test:GroupMessage:g1",
        "expected_language_version": "astrbot-python-subset/v1",
    }
    kwargs.update(mutation)
    ok = await db.commit_script_cron_state("job-1", {"n": 1}, **kwargs)
    assert ok is False
    fetched = await db.get_script_cron_job("job-1")
    assert fetched.script.state == {}


async def test_state_cas_true_and_reset(db):
    await db.create_script_cron_job(**_script_fields())
    ok = await db.commit_script_cron_state(
        "job-1",
        {"n": 1},
        expected_source_hash=compute_source_hash("x = 1"),
        expected_bound_umo="test:GroupMessage:g1",
        expected_language_version="astrbot-python-subset/v1",
    )
    assert ok is True
    assert (await db.reset_script_cron_state("job-1")) is True
    fetched = await db.get_script_cron_job("job-1")
    assert fetched.script.state == {}


async def test_mark_running_interrupted(db):
    await db.create_cron_job(
        name="a",
        job_type="basic",
        cron_expression="* * * * *",
        status="running",
    )
    await db.create_cron_job(
        name="b",
        job_type="active_agent",
        cron_expression="* * * * *",
        status="scheduled",
    )
    count = await db.mark_running_cron_jobs_interrupted(
        "Interrupted by AstrBot restart before completion."
    )
    assert count == 1
    jobs = {j.name: j.status for j in await db.list_cron_jobs()}
    assert jobs["a"] == "failed"
    assert jobs["b"] == "scheduled"
