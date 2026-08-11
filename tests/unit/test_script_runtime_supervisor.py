"""Supervisor worker lifecycle tests."""

from __future__ import annotations

import subprocess
import sys

import pytest

from astrbot.core.cron.script_supervisor import (
    ScriptRunRequest,
    ScriptSupervisor,
    build_proxy_snapshot,
)
from astrbot.script_runtime.validator import compute_source_hash


def _request(source: str, *, timeout: int = 5) -> ScriptRunRequest:
    return ScriptRunRequest(
        job_id="job-1",
        run_id="run-1",
        source=source,
        source_hash=compute_source_hash(source),
        language_version="astrbot-python-subset/v1",
        initial_state={},
        run_started_at="2026-08-12T08:00:00+08:00",
        run_timezone="Asia/Shanghai",
        limits={
            "execution_timeout_seconds": timeout,
            "max_source_bytes": 65536,
            "max_ast_nodes": 10000,
            "max_ast_depth": 100,
        },
        proxy_snapshot=None,
    )


@pytest.mark.asyncio
async def test_validate_ok_and_invalid():
    supervisor = ScriptSupervisor()
    ok = await supervisor.validate("x = 1 + 2")
    assert ok.valid
    bad = await supervisor.validate("def x(:\n")
    assert not bad.valid
    assert bad.total_diagnostics >= 1
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_execute_send_and_state():
    supervisor = ScriptSupervisor()
    sent: list[str] = []

    async def send_handler(text: str) -> None:
        sent.append(text)

    result = await supervisor.execute(
        _request("await ctx.send_text('hello')\nctx.state['n'] = 1\n"),
        send_handler=send_handler,
    )
    assert result.success
    assert result.state == {"n": 1}
    assert sent == ["hello"]
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_script_exception_reported():
    supervisor = ScriptSupervisor()

    async def send_handler(text: str) -> None:
        pass

    result = await supervisor.execute(
        _request("raise ValueError('boom')\n"),
        send_handler=send_handler,
    )
    assert not result.success
    assert result.error_code == "UNCAUGHT_SCRIPT_EXCEPTION"
    assert "boom" in (result.message or "")
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_hard_deadline_kills_worker():
    supervisor = ScriptSupervisor()

    async def send_handler(text: str) -> None:
        pass

    result = await supervisor.execute(
        _request("while True:\n    pass\n", timeout=1),
        send_handler=send_handler,
    )
    assert not result.success
    assert result.error_code == "SCRIPT_EXECUTION_TIMEOUT"
    assert supervisor._procs == set()
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_send_failure_maps_to_send_error():
    supervisor = ScriptSupervisor()

    async def send_handler(text: str) -> None:
        raise RuntimeError("platform down")

    result = await supervisor.execute(
        _request("await ctx.send_text('x')\n"),
        send_handler=send_handler,
    )
    assert not result.success
    assert result.error_type == "SendError"
    await supervisor.shutdown()


def test_proxy_snapshot():
    assert build_proxy_snapshot({"http_proxy": "http://p:8080"}) == {
        "http": "http://p:8080",
        "https": "http://p:8080",
        "no_proxy": "",
    }
    assert build_proxy_snapshot({"no_proxy": ["10.*"]}) == {
        "http": "",
        "https": "",
        "no_proxy": "10.*",
    }
    assert build_proxy_snapshot({}) is None


def test_worker_never_imports_astrbot_core():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import astrbot.script_runtime.worker as w; "
                "import sys; "
                "print('astrbot.core' in sys.modules)"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"
