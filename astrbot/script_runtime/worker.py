"""One-shot worker for script task validation and execution.

Entry points:

- ``python -m astrbot.script_runtime.worker``
- ``<astrbot executable> --internal-script-task-worker`` (frozen builds)

The worker talks only the length-prefixed JSON protocol on stdin/stdout.  It
must never import ``astrbot.core`` (enforced by tests): it only needs this
package, ``astrbot.script_runtime`` and the standard library.
"""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime, timezone
from typing import Any

from astrbot.script_runtime import spec
from astrbot.script_runtime.errors import (
    ALL_CATCHABLE,
    ScriptHostCancelled,
    ScriptInterrupted,
    ScriptLanguageVersionError,
    ScriptLimitsError,
    ScriptProtocolError,
    SendError,
)
from astrbot.script_runtime.http import ScriptHttpClient
from astrbot.script_runtime.interpreter import Interpreter, RunFacade
from astrbot.script_runtime.protocol import (
    PROTOCOL_VERSION,
    read_frame_blocking,
    validate_common_fields,
    write_frame_blocking,
)
from astrbot.script_runtime.state import AtomicState
from astrbot.script_runtime.stdlib import Stdlib
from astrbot.script_runtime.validator import compute_source_hash, validate_source
from astrbot.script_runtime.values import SafeValue


def _write(payload: dict[str, Any]) -> None:
    write_frame_blocking(sys.stdout.buffer, payload)


def _reply_ok(payload: dict[str, Any]) -> None:
    _write({"protocol_version": PROTOCOL_VERSION, "type": "ok", **payload})


def _handle_validate(request: dict[str, Any]) -> int:
    language_version = request.get("language_version")
    source = request.get("source")
    limits = request.get("limits")
    if not isinstance(source, str):
        _write(
            {
                "protocol_version": PROTOCOL_VERSION,
                "type": "error",
                "error_code": "SCRIPT_PROTOCOL_ERROR",
                "message": "source must be a string",
            }
        )
        return 1
    try:
        if language_version not in spec.SUPPORTED_LANGUAGE_VERSIONS:
            raise ScriptLanguageVersionError(str(language_version))
        result = validate_source(
            source, language_version=language_version, limits=limits
        )
        _write(
            {
                "protocol_version": PROTOCOL_VERSION,
                "type": "validation_result",
                "result": result.to_dict(),
            }
        )
        return 0
    except ScriptLanguageVersionError as exc:
        _write(
            {
                "protocol_version": PROTOCOL_VERSION,
                "type": "error",
                "error_code": "SCRIPT_LANGUAGE_VERSION_UNKNOWN",
                "message": str(exc),
            }
        )
        return 1
    except ScriptLimitsError as exc:
        _write(
            {
                "protocol_version": PROTOCOL_VERSION,
                "type": "error",
                "error_code": "SCRIPT_INVALID_LIMITS",
                "message": str(exc),
            }
        )
        return 1
    except Exception as exc:  # noqa: BLE001
        _write(
            {
                "protocol_version": PROTOCOL_VERSION,
                "type": "error",
                "error_code": "SCRIPT_WORKER_CRASHED",
                "message": str(exc),
            }
        )
        return 1


async def _handle_execute(request: dict[str, Any]) -> int:
    run_id = request.get("run_id")
    language_version = request.get("language_version")
    source = request.get("source")
    source_hash = request.get("source_hash")
    initial_state = request.get("state") or {}
    limits = request.get("limits") or {}
    run_started_at = request.get("run_started_at")
    run_timezone = request.get("run_timezone") or ""
    proxy = request.get("proxy")

    if not isinstance(run_id, str) or not run_id:
        raise ScriptProtocolError("run_id must be a non-empty string")
    if not isinstance(source, str):
        raise ScriptProtocolError("source must be a string")
    if language_version not in spec.SUPPORTED_LANGUAGE_VERSIONS:
        raise ScriptLanguageVersionError(str(language_version))
    if not isinstance(source_hash, str) or compute_source_hash(source) != source_hash:
        _write(
            {
                "protocol_version": PROTOCOL_VERSION,
                "type": "failed",
                "run_id": run_id,
                "error_code": "SCRIPT_SOURCE_HASH_MISMATCH",
                "message": "source hash does not match the persisted definition",
            }
        )
        return 1

    validation = validate_source(
        source, language_version=language_version, limits=limits
    )
    if not validation.valid:
        _write(
            {
                "protocol_version": PROTOCOL_VERSION,
                "type": "failed",
                "run_id": run_id,
                "error_code": "SCRIPT_VALIDATION_FAILED",
                "message": "source failed static validation",
            }
        )
        return 1

    coerced_limits = spec.coerce_limits(limits)
    deadline_seconds = coerced_limits["execution_timeout_seconds"]
    started_monotonic = time.monotonic()
    deadline = started_monotonic + deadline_seconds

    def remaining() -> float:
        return max(deadline - time.monotonic(), 0.0)

    state = AtomicState(initial_state if isinstance(initial_state, dict) else {})
    http_client = ScriptHttpClient(proxy_snapshot=proxy, remaining_seconds=remaining)
    send_lock = asyncio.Lock()
    request_counter = 0

    async def send_text(
        args: list[SafeValue], kwargs: dict[str, SafeValue]
    ) -> SafeValue:
        nonlocal request_counter
        if kwargs or len(args) != 1:
            raise SendError("ctx.send_text expects exactly one string argument")
        text = args[0].value
        if not isinstance(text, str):
            raise TypeError("ctx.send_text expects a string")
        async with send_lock:
            request_counter += 1
            frame = {
                "protocol_version": PROTOCOL_VERSION,
                "type": "send_text",
                "run_id": run_id,
                "request_id": request_counter,
                "text": text,
            }
            await asyncio.to_thread(_write, frame)
            ack = await asyncio.to_thread(read_frame_blocking, sys.stdin.buffer)
            validate_common_fields(
                ack,
                expected_type="ack",
                expected_run_id=run_id,
            )
            if ack.get("request_id") != request_counter:
                raise ScriptProtocolError("ack request_id mismatch")
            if ack.get("ok") is False:
                raise SendError(str(ack.get("error") or "send failed"))
        return SafeValue(spec.KIND_NONE, None)

    run_facade = RunFacade(
        job_id=str(request.get("job_id") or ""),
        run_id=run_id,
        started_at=_parse_started_at(run_started_at),
        timezone=run_timezone,
    )
    stdlib = Stdlib(
        http_client=http_client,
        send_text=send_text,
        run_facade=run_facade,
        state=state,
    )
    interpreter = Interpreter(stdlib, deadline=deadline)
    try:
        await interpreter.run_module(source)
    except tuple(ALL_CATCHABLE) as exc:
        _write(
            {
                "protocol_version": PROTOCOL_VERSION,
                "type": "failed",
                "run_id": run_id,
                "error_code": "UNCAUGHT_SCRIPT_EXCEPTION",
                "error_type": type(exc).__name__,
                "message": str(exc),
            }
        )
        return 0
    except (ScriptInterrupted,) as exc:
        _write(
            {
                "protocol_version": PROTOCOL_VERSION,
                "type": "failed",
                "run_id": run_id,
                "error_code": "SCRIPT_EXECUTION_TIMEOUT",
                "message": str(exc),
            }
        )
        return 0
    except ScriptHostCancelled as exc:
        _write(
            {
                "protocol_version": PROTOCOL_VERSION,
                "type": "failed",
                "run_id": run_id,
                "error_code": "SCRIPT_HOST_CANCELLED",
                "message": str(exc),
            }
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        _write(
            {
                "protocol_version": PROTOCOL_VERSION,
                "type": "failed",
                "run_id": run_id,
                "error_code": "SCRIPT_WORKER_CRASHED",
                "message": str(exc),
            }
        )
        return 1

    try:
        final_state = state.snapshot()
    except Exception as exc:  # noqa: BLE001
        _write(
            {
                "protocol_version": PROTOCOL_VERSION,
                "type": "failed",
                "run_id": run_id,
                "error_code": "SCRIPT_RESULT_INVALID",
                "message": f"final state is not valid JSON: {exc}",
            }
        )
        return 1
    _write(
        {
            "protocol_version": PROTOCOL_VERSION,
            "type": "completed",
            "run_id": run_id,
            "state": final_state,
        }
    )
    return 0


def _parse_started_at(raw: Any) -> datetime:
    if isinstance(raw, str):
        try:
            value = datetime.fromisoformat(raw)
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def run() -> int:
    try:
        request = read_frame_blocking(sys.stdin.buffer)
    except ScriptProtocolError as exc:
        _write(
            {
                "protocol_version": PROTOCOL_VERSION,
                "type": "error",
                "error_code": "SCRIPT_PROTOCOL_ERROR",
                "message": str(exc),
            }
        )
        return 1
    validate_common_fields(request)
    frame_type = request.get("type")
    if frame_type == "validate":
        return _handle_validate(request)
    if frame_type == "execute":
        try:
            return asyncio.run(_handle_execute(request))
        except ScriptProtocolError as exc:
            _write(
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "type": "failed",
                    "run_id": request.get("run_id"),
                    "error_code": "SCRIPT_WORKER_PROTOCOL_ERROR",
                    "message": str(exc),
                }
            )
            return 1
        except Exception as exc:  # noqa: BLE001
            _write(
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "type": "failed",
                    "run_id": request.get("run_id"),
                    "error_code": "SCRIPT_WORKER_CRASHED",
                    "message": str(exc),
                }
            )
            return 1
    _write(
        {
            "protocol_version": PROTOCOL_VERSION,
            "type": "error",
            "error_code": "SCRIPT_PROTOCOL_ERROR",
            "message": f"unknown request type: {frame_type!r}",
        }
    )
    return 1


if __name__ == "__main__":
    sys.exit(run())


__all__ = ["run"]
