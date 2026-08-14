from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import TypeVar

from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from astrbot import logger
from astrbot.core.trace.outbound import OutboundCallRecorder
from astrbot.core.utils.config_number import coerce_int_config
from astrbot.core.utils.network_utils import is_connection_error

T = TypeVar("T")

REQUEST_RETRY_ATTEMPTS = 5  # default value
REQUEST_RETRY_WAIT_MIN_S = 0.2
REQUEST_RETRY_WAIT_MAX_S = 30
REQUEST_RETRY_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504, 529}


def _get_status_code(error: BaseException) -> int | None:
    for attr in ("status_code", "status", "code"):
        value = getattr(error, attr, None)
        if isinstance(value, int):
            return value

    response = getattr(error, "response", None)
    if response is not None:
        status_code = getattr(response, "status_code", None)
        if isinstance(status_code, int):
            return status_code

    return None


def _is_retryable_provider_request_error(
    error: BaseException,
    *,
    retry_rate_limits: bool,
) -> bool:
    if is_connection_error(error):
        return True

    error_type_name = type(error).__name__
    if error_type_name in {"APIConnectionError", "APITimeoutError"}:
        return True

    status_code = _get_status_code(error)
    if status_code is None:
        return False

    if status_code == 429 and not retry_rate_limits:
        return False

    return status_code in REQUEST_RETRY_STATUS_CODES or 500 <= status_code <= 599


def _log_retry(
    provider_label: str,
    retry_state: RetryCallState,
    max_attempts: int,
) -> None:
    error = retry_state.outcome.exception() if retry_state.outcome else None
    logger.warning(
        f"[{provider_label}] Request failed with retryable error; "
        f"retrying ({retry_state.attempt_number + 1}/{max_attempts}): "
        f"{error}"
    )


def _build_retrying(
    provider_label: str,
    *,
    retry_rate_limits: bool,
    max_attempts: int | None = None,
    recorder: OutboundCallRecorder | None = None,
) -> AsyncRetrying:
    max_attempts = coerce_int_config(
        max_attempts if max_attempts is not None else REQUEST_RETRY_ATTEMPTS,
        default=REQUEST_RETRY_ATTEMPTS,
        min_value=1,
        field_name="request_max_retries",
        source=provider_label,
    )

    def _before_sleep(retry_state: RetryCallState) -> None:
        _log_retry(provider_label, retry_state, max_attempts)
        if recorder is None or retry_state.outcome is None:
            return
        error = retry_state.outcome.exception()
        if error is None:
            return
        next_action = getattr(retry_state, "next_action", None)
        recorder.record_retry(
            error,
            attempt_number=recorder.last_attempt_number,
            next_attempt_number=retry_state.attempt_number + 1,
            backoff_seconds=getattr(next_action, "sleep", None),
        )

    return AsyncRetrying(
        retry=retry_if_exception(
            lambda error: _is_retryable_provider_request_error(
                error,
                retry_rate_limits=retry_rate_limits,
            )
        ),
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(
            multiplier=1,
            min=REQUEST_RETRY_WAIT_MIN_S,
            max=REQUEST_RETRY_WAIT_MAX_S,
        ),
        before_sleep=_before_sleep,
        reraise=True,
    )


async def retry_provider_request(
    provider_label: str,
    request_factory: Callable[[], Awaitable[T]],
    *,
    retry_rate_limits: bool = True,
    max_attempts: int | None = None,
    recorder: OutboundCallRecorder | None = None,
    _defer_completion: bool = False,
    _record_terminal_failure: bool = True,
) -> T:
    retrying = _build_retrying(
        provider_label,
        retry_rate_limits=retry_rate_limits,
        max_attempts=max_attempts,
        recorder=recorder,
    )
    effective_max_attempts = coerce_int_config(
        max_attempts if max_attempts is not None else REQUEST_RETRY_ATTEMPTS,
        default=REQUEST_RETRY_ATTEMPTS,
        min_value=1,
        field_name="request_max_retries",
        source=provider_label,
    )

    async for attempt in retrying:
        attempt_number = recorder.record_attempt() if recorder is not None else 0
        with attempt:
            try:
                result = await request_factory()
            except BaseException as error:
                if recorder is not None:
                    retryable = _is_retryable_provider_request_error(
                        error,
                        retry_rate_limits=retry_rate_limits,
                    )
                    terminal = (
                        not retryable
                        or attempt.retry_state.attempt_number >= effective_max_attempts
                    )
                    if terminal and _record_terminal_failure:
                        recorder.record_failed(
                            error,
                            attempt_number=attempt_number,
                            terminal=True,
                        )
                raise
            if recorder is not None and not _defer_completion:
                recorder.record_completed(result, attempt_number=attempt_number)
            return result

    raise RuntimeError("Provider request retry loop exited unexpectedly.")


@asynccontextmanager
async def retry_provider_request_context(
    provider_label: str,
    context_manager_factory: Callable[[], AbstractAsyncContextManager[T]],
    *,
    retry_rate_limits: bool = True,
    max_attempts: int | None = None,
    recorder: OutboundCallRecorder | None = None,
) -> AsyncIterator[T]:
    manager: AbstractAsyncContextManager[T] | None = None

    async def _enter_context() -> T:
        nonlocal manager
        manager = context_manager_factory()
        return await manager.__aenter__()

    value = await retry_provider_request(
        provider_label,
        _enter_context,
        retry_rate_limits=retry_rate_limits,
        max_attempts=max_attempts,
        recorder=recorder,
        _defer_completion=True,
    )

    if manager is None:
        raise RuntimeError("Provider request context was not created.")

    attempt_number = recorder.last_attempt_number if recorder is not None else 0
    try:
        yield value
    except BaseException as error:
        suppressed = await manager.__aexit__(type(error), error, error.__traceback__)
        if suppressed:
            if recorder is not None:
                recorder.record_completed(value, attempt_number=attempt_number)
            return
        if recorder is not None:
            recorder.record_failed(error, attempt_number=attempt_number)
        raise
    else:
        try:
            await manager.__aexit__(None, None, None)
        except BaseException as error:
            if recorder is not None:
                recorder.record_failed(error, attempt_number=attempt_number)
            raise
        if recorder is not None:
            recorder.record_completed(value, attempt_number=attempt_number)
