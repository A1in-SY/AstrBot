"""Exceptions for the script runtime.

``Http*Error``, ``SendError`` and ``StateError`` families are catchable by
scripts.  The hard failure classes (timeout, OOM, protocol corruption, host
cancellation) never cross into the script interpreter.
"""

from __future__ import annotations


class ScriptRuntimeError(Exception):
    """Base class for internal runtime errors (not script-visible)."""


class ScriptLanguageVersionError(ScriptRuntimeError):
    """Raised when a language version is not in the registry."""

    def __init__(self, language_version: str) -> None:
        self.language_version = language_version
        super().__init__(f"Unknown script language version: {language_version}")


class ScriptLimitsError(ScriptRuntimeError):
    """Raised when a limits snapshot is invalid."""


class ScriptProtocolError(ScriptRuntimeError):
    """Raised on IPC frame corruption or contract violations."""


class ScriptHostCancelled(ScriptRuntimeError):
    """Raised inside the worker when the host cancels the run."""


class ScriptInterrupted(ScriptRuntimeError):
    """Raised inside the worker when the hard deadline expires."""


# ---------------------------------------------------------------------------
# Script-catchable exception classes
# ---------------------------------------------------------------------------


class HttpError(Exception):
    """Base class for HTTP facade errors."""


class HttpInvalidRequestError(HttpError):
    """The request parameters were invalid (method, URL, headers, body...)."""


class HttpTimeoutError(HttpError):
    """The HTTP request exceeded the remaining run deadline."""


class HttpProxyError(HttpError):
    """Proxy configuration or connection failure."""


class HttpConnectionError(HttpError):
    """Connection-level failure (DNS, refused, reset...)."""


class HttpProtocolError(HttpError):
    """Protocol-level failure (invalid response, unsupported scheme...)."""


class HttpDecodeError(HttpError):
    """The response could not be decoded or parsed as JSON."""


class SendError(Exception):
    """The host could not deliver a message to the bound session."""


class SendTargetUnavailableError(SendError):
    """No matching platform/session adapter was available."""


class StateError(Exception):
    """Generic state mutation error."""


class StateNotJsonError(StateError):
    """The candidate state contains a non-JSON value."""


ALL_CATCHABLE = (
    Exception,
    ArithmeticError,
    ZeroDivisionError,
    OverflowError,
    LookupError,
    KeyError,
    IndexError,
    ValueError,
    TypeError,
    NameError,
    RuntimeError,
    HttpError,
    HttpInvalidRequestError,
    HttpTimeoutError,
    HttpProxyError,
    HttpConnectionError,
    HttpProtocolError,
    HttpDecodeError,
    SendError,
    SendTargetUnavailableError,
    StateError,
    StateNotJsonError,
)


def is_script_catchable(exc: BaseException) -> bool:
    """Return whether an exception may be handled by script ``except`` blocks."""
    return isinstance(exc, tuple(ALL_CATCHABLE)) and not isinstance(
        exc, ScriptRuntimeError
    )


BUILTIN_EXCEPTIONS: dict[str, type[BaseException]] = {
    "Exception": Exception,
    "ArithmeticError": ArithmeticError,
    "ZeroDivisionError": ZeroDivisionError,
    "OverflowError": OverflowError,
    "LookupError": LookupError,
    "KeyError": KeyError,
    "IndexError": IndexError,
    "ValueError": ValueError,
    "TypeError": TypeError,
    "NameError": NameError,
    "RuntimeError": RuntimeError,
}


__all__ = [
    "ALL_CATCHABLE",
    "BUILTIN_EXCEPTIONS",
    "HttpConnectionError",
    "HttpDecodeError",
    "HttpError",
    "HttpInvalidRequestError",
    "HttpProtocolError",
    "HttpProxyError",
    "HttpTimeoutError",
    "is_script_catchable",
    "ScriptHostCancelled",
    "ScriptInterrupted",
    "ScriptLanguageVersionError",
    "ScriptLimitsError",
    "ScriptProtocolError",
    "ScriptRuntimeError",
    "SendError",
    "SendTargetUnavailableError",
    "StateError",
    "StateNotJsonError",
]
