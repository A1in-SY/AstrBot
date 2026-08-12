"""CronService tests for script task endpoints."""

from __future__ import annotations

import asyncio
import tempfile

import pytest
import pytest_asyncio

from astrbot.core.cron.manager import CronJobManager
from astrbot.core.db.sqlite import SQLiteDatabase
from astrbot.dashboard.api.cron import _raise_cron_error
from astrbot.dashboard.responses import ApiError
from astrbot.dashboard.services.cron_service import CronService, CronServiceError

pytestmark = pytest.mark.asyncio


def _config() -> dict:
    return {
        "script_task": {
            "enabled": True,
            "allowed_umos": ["test:GroupMessage:g1"],
            "execution_timeout_seconds": 5,
            "max_source_bytes": 65536,
            "max_ast_nodes": 10000,
            "max_ast_depth": 100,
        }
    }


class FakeContext:
    def __init__(self) -> None:
        self.config = _config()
        self.sent: list[str] = []

    def get_config(self, umo=None):
        return self.config

    async def send_message(self, session, chain):
        self.sent.append(str(chain))
        return True


class FakeLifecycle:
    def __init__(self, manager):
        self.cron_manager = manager


@pytest_asyncio.fixture
async def service():
    db = SQLiteDatabase(tempfile.mktemp(suffix=".db"))
    await db.initialize()
    manager = CronJobManager(db)
    context = FakeContext()
    await manager.start(context)
    svc = CronService(FakeLifecycle(manager))
    yield svc, manager, db, context
    await manager.shutdown()
    await db.engine.dispose()


async def test_validate_script_endpoint(service):
    svc, *_ = service
    result = await svc.validate_script("x = 1", "astrbot-python-subset/v1")
    assert result["valid"] is True
    bad = await svc.validate_script("def x(:\n", "astrbot-python-subset/v1")
    assert bad["valid"] is False
    assert bad["total_diagnostics"] >= 1


async def test_script_languages_endpoint(service):
    svc, *_ = service
    data = svc.script_languages()
    assert data["default_language_version"] == "astrbot-python-subset/v1"
    assert data["versions"][0]["language_version"] == "astrbot-python-subset/v1"


async def test_create_script_and_run(service):
    svc, _, db, context = service
    created = await svc.create_job(
        {
            "job_type": "script",
            "name": "gold",
            "bound_umo": "test:GroupMessage:g1",
            "cron_expression": "*/5 * * * *",
            "source": (
                "price = 3888\n"
                "if price < 3900:\n"
                "    await ctx.send_text('low')\n"
                "    ctx.state['last'] = str(price)\n"
            ),
        }
    )
    job_id = created["job_id"]
    assert created["job_type"] == "script"
    await svc.run_job_now(job_id)
    detail = await svc.get_job(job_id)
    for _ in range(60):
        if detail["status"] in {"completed", "failed"}:
            break
        await asyncio.sleep(0.05)
        detail = await svc.get_job(job_id)
    assert detail["status"] == "completed"
    assert detail["script"]["state"] == {"last": "3888"}


async def test_create_script_invalid_returns_422_validation(service):
    svc, *_ = service
    with pytest.raises(CronServiceError) as excinfo:
        await svc.create_job(
            {
                "job_type": "script",
                "bound_umo": "test:GroupMessage:g1",
                "cron_expression": "* * * * *",
                "source": "bad syntax <<<",
            }
        )
    assert excinfo.value.status_code == 422
    assert excinfo.value.code == "SCRIPT_VALIDATION_FAILED"
    assert excinfo.value.data["validation"]["valid"] is False


async def test_cron_service_error_string_is_only_message():
    error = CronServiceError(
        "plain message",
        422,
        "STABLE_CODE",
        data={"validation": {"valid": False}},
    )
    assert str(error) == "plain message"
    assert repr(str(error)) == "'plain message'"


async def test_api_error_includes_code_and_preserves_details():
    service_error = CronServiceError(
        "script source failed static validation",
        422,
        "SCRIPT_VALIDATION_FAILED",
        data={"validation": {"valid": False}},
    )
    with pytest.raises(ApiError) as excinfo:
        _raise_cron_error(service_error)
    assert excinfo.value.message == "script source failed static validation"
    assert excinfo.value.status_code == 422
    assert excinfo.value.data == {
        "code": "SCRIPT_VALIDATION_FAILED",
        "validation": {"valid": False},
    }


async def test_api_error_without_details_still_includes_code():
    with pytest.raises(ApiError) as excinfo:
        _raise_cron_error(CronServiceError("conflict", 409, "JOB_ALREADY_RUNNING"))
    assert excinfo.value.data == {"code": "JOB_ALREADY_RUNNING"}


async def test_create_script_requires_bound_umo(service):
    svc, *_ = service
    with pytest.raises(CronServiceError) as excinfo:
        await svc.create_job(
            {
                "job_type": "script",
                "cron_expression": "* * * * *",
                "source": "x = 1",
            }
        )
    assert excinfo.value.code == "SCRIPT_BOUND_UMO_REQUIRED"


async def test_update_script_migration(service):
    svc, _, db, _ = service
    created = await svc.create_job(
        {
            "job_type": "script",
            "bound_umo": "test:GroupMessage:g1",
            "cron_expression": "*/5 * * * *",
            "source": "x = 1",
        }
    )
    job_id = created["job_id"]
    updated = await svc.update_job(job_id, {"name": "gold2"})
    assert updated["name"] == "gold2"


@pytest.mark.parametrize("entrypoint", ["create", "update", "validate"])
async def test_unknown_language_version_returns_stable_422(service, entrypoint):
    svc, *_ = service
    payload = {
        "job_type": "script",
        "bound_umo": "test:GroupMessage:g1",
        "cron_expression": "*/5 * * * *",
        "source": "x = 1",
        "language_version": "astrbot-python-subset/v999",
    }
    with pytest.raises(CronServiceError) as excinfo:
        if entrypoint == "create":
            await svc.create_job(payload)
        elif entrypoint == "update":
            created = await svc.create_job(
                {**payload, "language_version": "astrbot-python-subset/v1"}
            )
            await svc.update_job(
                created["job_id"],
                {"language_version": "astrbot-python-subset/v999"},
            )
        else:
            await svc.validate_script("x = 1", "astrbot-python-subset/v999")
    assert excinfo.value.status_code == 422
    assert excinfo.value.code == "SCRIPT_LANGUAGE_VERSION_UNKNOWN"


async def test_reset_state_endpoint(service):
    svc, _, _, _ = service
    created = await svc.create_job(
        {
            "job_type": "script",
            "bound_umo": "test:GroupMessage:g1",
            "cron_expression": "*/5 * * * *",
            "source": "ctx.state['a'] = 1\n",
        }
    )
    job_id = created["job_id"]
    detail = await svc.reset_script_state(job_id)
    assert detail["script"]["state"] == {}
    assert detail["script_summary"]["bound_umo"] == "test:GroupMessage:g1"
    assert detail["script_summary"]["execution_authorization"]["allowed"] is True


async def test_run_now_conflict_409(service):
    svc, manager, _, _ = service
    created = await svc.create_job(
        {
            "job_type": "script",
            "bound_umo": "test:GroupMessage:g1",
            "cron_expression": "*/5 * * * *",
            "source": "await ctx.send_text('x')\nwhile True:\n    pass\n",
        }
    )
    job_id = created["job_id"]
    task = asyncio.create_task(manager.run_job_now(job_id))
    await asyncio.sleep(0.2)
    with pytest.raises(CronServiceError) as excinfo:
        await svc.run_job_now(job_id)
    assert excinfo.value.status_code == 409
    assert excinfo.value.code == "JOB_ALREADY_RUNNING"
    await asyncio.sleep(5.5)
    await task
