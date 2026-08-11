"""Parent-process supervisor for one-shot script task workers.

The supervisor owns worker process lifecycle, the length-prefixed JSON
protocol, hard deadlines, stderr draining, proxy environment snapshots and
shutdown cleanup.  It never imports ``astrbot.script_runtime.worker`` (the
worker must stay isolated); it only talks to it over pipes.
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from typing import Any

from astrbot import logger
from astrbot.script_runtime import spec
from astrbot.script_runtime.diagnostics import ValidationResult
from astrbot.script_runtime.protocol import (
    PROTOCOL_VERSION,
    decode_frame,
    encode_frame,
    validate_common_fields,
)

_PROXY_ENV_NAMES = (
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
)


@dataclass(frozen=True)
class ScriptRunRequest:
    job_id: str
    run_id: str
    source: str
    source_hash: str
    language_version: str
    initial_state: dict[str, Any]
    run_started_at: str
    run_timezone: str
    limits: dict[str, int]
    proxy_snapshot: dict[str, Any] | None


@dataclass(frozen=True)
class ScriptRunResult:
    success: bool
    state: dict[str, Any] | None = None
    error_code: str | None = None
    error_type: str | None = None
    message: str | None = None
    worker_stderr_tail: str = ""

    @classmethod
    def failed(
        cls,
        error_code: str,
        message: str | None,
        *,
        error_type: str | None = None,
        stderr_tail: str = "",
    ) -> ScriptRunResult:
        return cls(
            success=False,
            error_code=error_code,
            error_type=error_type,
            message=message,
            worker_stderr_tail=stderr_tail,
        )


def build_proxy_snapshot(config: dict[str, Any] | None) -> dict[str, Any] | None:
    """Build the proxy snapshot from the AstrBot global config."""
    if not config:
        return None
    proxy_url = str(config.get("http_proxy") or "").strip()
    no_proxy = config.get("no_proxy") or []
    if not isinstance(no_proxy, list):
        no_proxy = []
    no_proxy_str = ",".join(str(item).strip() for item in no_proxy if str(item).strip())
    if not proxy_url and not no_proxy_str:
        return None
    return {
        "http": proxy_url,
        "https": proxy_url,
        "no_proxy": no_proxy_str,
    }


def _worker_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--internal-script-task-worker"]
    return [sys.executable, "-m", "astrbot.script_runtime.worker"]


def _worker_env(proxy_snapshot: dict[str, Any] | None) -> dict[str, str]:
    env = dict(os.environ)
    for name in _PROXY_ENV_NAMES:
        env.pop(name, None)
    if proxy_snapshot:
        proxy_url = proxy_snapshot.get("https") or proxy_snapshot.get("http")
        if proxy_url:
            env["HTTP_PROXY"] = proxy_url
            env["HTTPS_PROXY"] = proxy_url
            env["ALL_PROXY"] = proxy_url
        no_proxy = proxy_snapshot.get("no_proxy")
        if no_proxy:
            env["NO_PROXY"] = no_proxy
    return env


class ScriptSupervisor:
    """Manages validation and execution workers."""

    def __init__(self) -> None:
        self._procs: set[asyncio.subprocess.Process] = set()
        self._shutting_down = False

    async def validate(
        self,
        source: str,
        *,
        language_version: str = spec.DEFAULT_LANGUAGE_VERSION,
        limits: dict[str, int] | None = None,
    ) -> ValidationResult:
        """Run a one-shot validation worker and return the result."""
        request = {
            "protocol_version": PROTOCOL_VERSION,
            "type": "validate",
            "language_version": language_version,
            "source": source,
            "limits": dict(limits or {}),
        }
        proc, writer = await self._spawn()
        try:
            writer.write(encode_frame(request))
            await writer.drain()
            frame = await proc.stdout.readexactly(8)
            length = int.from_bytes(frame, "big")
            body = await proc.stdout.readexactly(length)
            reply = decode_frame(frame + body)
            validate_common_fields(reply)
            if reply.get("type") == "validation_result":
                result = reply.get("result")
                if not isinstance(result, dict):
                    return ValidationResult.invalid(
                        [], language_version=language_version
                    )
                return _validation_result_from_dict(result, language_version)
            if reply.get("type") == "error":
                raise _ValidationWorkerError(
                    str(
                        reply.get("message")
                        or reply.get("error_code")
                        or "validation worker error"
                    )
                )
            raise _ValidationWorkerError("unexpected validation worker reply")
        finally:
            await self._finish_proc(proc, writer)

    async def execute(
        self,
        request: ScriptRunRequest,
        *,
        send_handler: Any,
    ) -> ScriptRunResult:
        """Execute a script task in a fresh worker with a hard deadline."""
        timeout = request.limits.get("execution_timeout_seconds", 30)
        proc, writer = await self._spawn(
            proxy_snapshot=request.proxy_snapshot,
        )
        stderr_tail: list[str] = []
        drain_task = asyncio.create_task(self._drain_stderr(proc, stderr_tail))
        try:
            execute_frame = {
                "protocol_version": PROTOCOL_VERSION,
                "type": "execute",
                "run_id": request.run_id,
                "job_id": request.job_id,
                "source": request.source,
                "source_hash": request.source_hash,
                "language_version": request.language_version,
                "state": request.initial_state,
                "run_started_at": request.run_started_at,
                "run_timezone": request.run_timezone,
                "limits": request.limits,
            }
            writer.write(encode_frame(execute_frame))
            await writer.drain()
            result = await asyncio.wait_for(
                self._run_loop(proc, request.run_id, send_handler),
                timeout=timeout,
            )
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                self._kill(proc)
                return ScriptRunResult.failed(
                    "SCRIPT_WORKER_CRASHED",
                    "worker did not exit after terminal frame",
                    stderr_tail="".join(stderr_tail),
                )
            if proc.returncode != 0:
                return ScriptRunResult.failed(
                    "SCRIPT_WORKER_CRASHED",
                    f"worker exited with code {proc.returncode}",
                    stderr_tail="".join(stderr_tail),
                )
            return result
        except asyncio.TimeoutError:
            self._kill(proc)
            return ScriptRunResult.failed(
                "SCRIPT_EXECUTION_TIMEOUT",
                f"script exceeded the {timeout}s hard deadline",
                stderr_tail="".join(stderr_tail),
            )
        except asyncio.IncompleteReadError:
            self._kill(proc)
            return ScriptRunResult.failed(
                "SCRIPT_WORKER_CRASHED",
                "worker exited before completing the run",
                stderr_tail="".join(stderr_tail),
            )
        except asyncio.CancelledError:
            self._kill(proc)
            raise
        except Exception as exc:  # noqa: BLE001
            self._kill(proc)
            logger.error("Script task worker failed: %s", exc)
            return ScriptRunResult.failed(
                "SCRIPT_WORKER_PROTOCOL_ERROR",
                str(exc),
                stderr_tail="".join(stderr_tail),
            )
        finally:
            if not drain_task.done():
                drain_task.cancel()
            await self._finish_proc(proc, writer)

    async def _run_loop(
        self,
        proc: asyncio.subprocess.Process,
        run_id: str,
        send_handler: Any,
    ) -> ScriptRunResult:
        while True:
            header = await proc.stdout.readexactly(8)
            length = int.from_bytes(header, "big")
            body = await proc.stdout.readexactly(length)
            frame = decode_frame(header + body)
            validate_common_fields(frame, expected_run_id=run_id)
            frame_type = frame.get("type")
            if frame_type == "send_text":
                request_id = frame.get("request_id")
                text = frame.get("text")
                if not isinstance(request_id, int) or not isinstance(text, str):
                    raise ValueError("invalid send_text frame")
                try:
                    await send_handler(text)
                    ok = True
                    error: str | None = None
                except Exception as exc:  # noqa: BLE001
                    ok = False
                    error = str(exc)
                ack = {
                    "protocol_version": PROTOCOL_VERSION,
                    "type": "ack",
                    "run_id": run_id,
                    "request_id": request_id,
                    "ok": ok,
                    "error": error,
                }
                await self._send_ack(proc, ack)
                continue
            if frame_type == "completed":
                state = frame.get("state")
                if not isinstance(state, dict):
                    return ScriptRunResult.failed(
                        "SCRIPT_RESULT_INVALID",
                        "completed frame state must be an object",
                    )
                return ScriptRunResult(success=True, state=state)
            if frame_type == "failed":
                return ScriptRunResult.failed(
                    str(frame.get("error_code") or "UNCAUGHT_SCRIPT_EXCEPTION"),
                    str(frame.get("message")),
                    error_type=frame.get("error_type"),
                )
            if frame_type == "error":
                return ScriptRunResult.failed(
                    str(frame.get("error_code") or "SCRIPT_WORKER_PROTOCOL_ERROR"),
                    str(frame.get("message")),
                )
            raise ValueError(f"unexpected frame type {frame_type!r}")

    async def _send_ack(
        self, proc: asyncio.subprocess.Process, ack: dict[str, Any]
    ) -> None:
        assert proc.stdin is not None
        proc.stdin.write(encode_frame(ack))
        await proc.stdin.drain()

    async def _spawn(
        self,
        *,
        proxy_snapshot: dict[str, Any] | None = None,
    ) -> tuple[asyncio.subprocess.Process, asyncio.StreamWriter]:
        if self._shutting_down:
            raise RuntimeError("script supervisor is shutting down")
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = getattr(
                __import__("subprocess"), "CREATE_NO_WINDOW", 0
            )
        proc = await asyncio.create_subprocess_exec(
            *_worker_command(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_worker_env(proxy_snapshot),
            **kwargs,
        )
        self._procs.add(proc)
        assert proc.stdin is not None
        return proc, proc.stdin

    async def _drain_stderr(
        self,
        proc: asyncio.subprocess.Process,
        tail: list[str],
    ) -> None:
        assert proc.stderr is not None
        buffer = ""
        try:
            while True:
                chunk = await proc.stderr.read(4096)
                if not chunk:
                    break
                buffer += chunk.decode("utf-8", errors="replace")
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    tail.append(line + "\n")
                    if len(tail) > 20:
                        tail.pop(0)
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            return

    @staticmethod
    def _kill(proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    async def _finish_proc(
        self,
        proc: asyncio.subprocess.Process,
        writer: asyncio.StreamWriter,
    ) -> None:
        self._procs.discard(proc)
        if proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                self._kill(proc)
                try:
                    await proc.wait()
                except Exception:  # noqa: BLE001
                    pass
        try:
            if not writer.is_closing():
                writer.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass

    async def shutdown(self) -> None:
        self._shutting_down = True
        procs = list(self._procs)
        for proc in procs:
            self._kill(proc)
        for proc in procs:
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                pass
        self._procs.clear()


class _ValidationWorkerError(Exception):
    pass


def _validation_result_from_dict(
    data: dict[str, Any],
    language_version: str,
) -> ValidationResult:
    from astrbot.script_runtime.diagnostics import Diagnostic, DiagnosticOccurrence

    diagnostics: list[Diagnostic] = []
    for raw in data.get("diagnostics") or []:
        if not isinstance(raw, dict):
            continue
        occurrences = []
        for occ in raw.get("occurrences") or []:
            if not isinstance(occ, dict):
                continue
            occurrences.append(
                DiagnosticOccurrence(
                    line=int(occ.get("line", 1)),
                    column=int(occ.get("column", 1)),
                    end_line=int(occ.get("end_line", occ.get("line", 1))),
                    end_column=int(occ.get("end_column", occ.get("column", 1))),
                )
            )
        diagnostics.append(
            Diagnostic(
                code=str(raw.get("code") or "UNKNOWN"),
                severity=str(raw.get("severity") or "error"),
                message=str(raw.get("message") or "validation error"),
                hint=raw.get("hint"),
                occurrences=occurrences,
                occurrence_count=int(raw.get("occurrence_count", len(occurrences))),
                suppressed_diagnostics=int(raw.get("suppressed_diagnostics", 0)),
            )
        )
    return ValidationResult(
        valid=bool(data.get("valid")),
        language_version=str(data.get("language_version") or language_version),
        diagnostics=diagnostics,
        total_diagnostics=len(diagnostics),
        truncated=bool(data.get("truncated")),
    )


__all__ = [
    "ScriptRunRequest",
    "ScriptRunResult",
    "ScriptSupervisor",
    "build_proxy_snapshot",
]
