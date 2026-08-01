"""Deterministic cleanup helpers for nested async-generator bridges."""

from __future__ import annotations

import sys
import typing as T
from contextlib import asynccontextmanager

from astrbot import logger


async def close_async_generator(
    generator: T.AsyncGenerator[T.Any, None],
) -> None:
    """Close ``generator`` while preserving an exception already in flight."""

    active_error = sys.exception()
    try:
        await generator.aclose()
    except BaseException:
        if active_error is None:
            raise
        logger.exception(
            "Failed to close an async generator while another exception was active."
        )


@asynccontextmanager
async def closing_async_generator(
    generator: T.AsyncGenerator[T.Any, None],
) -> T.AsyncIterator[T.AsyncGenerator[T.Any, None]]:
    """Close a nested async generator without masking an active failure.

    ``async for`` does not close a suspended async generator when its consumer
    exits early. Closing it in a plain ``finally`` fixes the leak, but a second
    exception raised by cleanup would replace the original business exception
    or cancellation. Preserve that original failure and only propagate a close
    failure when cleanup itself is the sole failure.
    """

    try:
        yield generator
    except BaseException:
        await close_async_generator(generator)
        raise
    else:
        await close_async_generator(generator)
