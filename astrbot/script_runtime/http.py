"""HTTP facade for ``ctx.http.request`` inside the one-shot worker."""

from __future__ import annotations

import json as _json
import math
from typing import Any

import httpx

from astrbot.script_runtime import spec
from astrbot.script_runtime.errors import (
    HttpConnectionError,
    HttpDecodeError,
    HttpInvalidRequestError,
    HttpProtocolError,
    HttpProxyError,
    HttpTimeoutError,
)
from astrbot.script_runtime.state import validate_json_value
from astrbot.script_runtime.stdlib import wrap
from astrbot.script_runtime.values import SafeValue


class SafeHttpResponse:
    """Read-only response facade exposed to scripts."""

    __slots__ = ("status", "headers", "text", "url", "_payload")

    def __init__(
        self,
        *,
        status: int,
        headers: httpx.Headers,
        text: str,
        url: str,
        payload: Any,
    ) -> None:
        self.status = status
        self.headers = headers
        self.text = text
        self.url = url
        self._payload = payload

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<SafeHttpResponse status={self.status}>"


def response_json(response: SafeHttpResponse) -> SafeValue:
    try:
        value = _json.loads(response._payload)
    except (ValueError, TypeError) as exc:
        raise HttpDecodeError(f"response.json(): {exc}") from None
    try:
        return wrap(validate_json_value(value))
    except Exception as exc:
        raise HttpDecodeError(f"response.json(): {exc}") from None


class ScriptHttpClient:
    """One request per client lifecycle; closed after every call."""

    def __init__(
        self,
        *,
        proxy_snapshot: dict[str, Any] | None,
        remaining_seconds: Any,
    ) -> None:
        self.proxy_snapshot = proxy_snapshot
        self.remaining_seconds = remaining_seconds

    async def request(
        self, args: list[SafeValue], kwargs: dict[str, SafeValue]
    ) -> SafeValue:
        if len(args) != 2:
            raise HttpInvalidRequestError(
                "ctx.http.request requires method and url positional arguments"
            )
        method_sv, url_sv = args
        if method_sv.kind != spec.KIND_STR or url_sv.kind != spec.KIND_STR:
            raise HttpInvalidRequestError("method and url must be strings")
        method = method_sv.value.strip().upper()
        url = url_sv.value
        if not method:
            raise HttpInvalidRequestError("method must not be empty")
        if not url:
            raise HttpInvalidRequestError("url must not be empty")

        allowed_kwargs = {
            "params",
            "headers",
            "content",
            "json",
            "data",
            "timeout_seconds",
            "follow_redirects",
            "use_proxy",
        }
        unknown = set(kwargs) - allowed_kwargs
        if unknown:
            raise HttpInvalidRequestError(
                f"unexpected keyword arguments: {', '.join(sorted(unknown))}"
            )

        params = self._parse_params(kwargs.get("params"))
        headers = self._parse_headers(kwargs.get("headers"))
        content, json_value, data = self._parse_body(kwargs)
        follow_redirects = self._parse_bool(
            kwargs.get("follow_redirects"), "follow_redirects", default=False
        )
        use_proxy = self._parse_bool(kwargs.get("use_proxy"), "use_proxy", default=True)

        remaining = self.remaining_seconds()
        if remaining is not None and remaining <= 0:
            raise HttpTimeoutError("the run deadline has already expired")
        timeout_sv = kwargs.get("timeout_seconds")
        timeout_seconds: float | None = None
        if timeout_sv is not None:
            if timeout_sv.kind not in (spec.KIND_INT, spec.KIND_FLOAT):
                raise HttpInvalidRequestError("timeout_seconds must be a number")
            value = float(timeout_sv.value)
            if not math.isfinite(value) or value <= 0:
                raise HttpInvalidRequestError("timeout_seconds must be positive")
            if remaining is not None:
                value = min(value, max(remaining, 0.0))
            timeout_seconds = value
        elif remaining is not None:
            timeout_seconds = max(remaining, 0.0)

        client_kwargs: dict[str, Any] = {
            "follow_redirects": follow_redirects,
            "timeout": timeout_seconds,
        }
        if not use_proxy:
            client_kwargs["trust_env"] = False

        body = {"params": params} if params is not None else {}
        if headers is not None:
            body["headers"] = headers
        if content is not None:
            body["content"] = content
        if json_value is not None:
            body["json"] = json_value
        if data is not None:
            body["data"] = data

        try:
            async with httpx.AsyncClient(**client_kwargs) as client:
                response = await client.request(method, url, **body)
        except httpx.InvalidURL as exc:
            raise HttpInvalidRequestError(f"invalid URL: {exc}") from None
        except httpx.UnsupportedProtocol as exc:
            raise HttpInvalidRequestError(f"unsupported protocol: {exc}") from None
        except httpx.ProxyError as exc:
            raise HttpProxyError(f"proxy error: {exc}") from None
        except httpx.TimeoutException as exc:
            raise HttpTimeoutError(f"request timed out: {exc}") from None
        except httpx.ConnectError as exc:
            raise HttpConnectionError(f"connection failed: {exc}") from None
        except httpx.RemoteProtocolError as exc:
            raise HttpProtocolError(f"protocol error: {exc}") from None
        except httpx.RequestError as exc:
            raise HttpConnectionError(f"request failed: {exc}") from None
        except httpx.HTTPError as exc:
            raise HttpProtocolError(f"http error: {exc}") from None

        try:
            text = response.text
        except Exception as exc:
            raise HttpDecodeError(f"cannot decode response body: {exc}") from None
        safe = SafeHttpResponse(
            status=response.status_code,
            headers=response.headers,
            text=text,
            url=str(response.url),
            payload=response.content,
        )
        return SafeValue(spec.KIND_HTTP_RESPONSE, safe)

    @staticmethod
    def _parse_params(sv: SafeValue | None) -> Any:
        if sv is None:
            return None
        if sv.kind == spec.KIND_DICT:
            return {str(k): v for k, v in sv.value.items()}
        if sv.kind in (spec.KIND_LIST, spec.KIND_TUPLE):
            pairs = []
            for item in sv.value:
                if not isinstance(item, (list, tuple)) or len(item) != 2:
                    raise HttpInvalidRequestError(
                        "params list must contain (name, value) pairs"
                    )
                pairs.append((str(item[0]), item[1]))
            return pairs
        raise HttpInvalidRequestError("params must be a dict or list of pairs")

    @staticmethod
    def _parse_headers(sv: SafeValue | None) -> dict[str, str] | None:
        if sv is None:
            return None
        if sv.kind != spec.KIND_DICT:
            raise HttpInvalidRequestError("headers must be a dict")
        headers: dict[str, str] = {}
        for key, value in sv.value.items():
            if not isinstance(value, str):
                raise HttpInvalidRequestError("header values must be strings")
            headers[str(key)] = value
        return headers

    @staticmethod
    def _parse_body(kwargs: dict[str, SafeValue]) -> tuple[Any, Any, Any]:
        present = [name for name in ("content", "json", "data") if name in kwargs]
        if len(present) > 1:
            raise HttpInvalidRequestError(
                "only one of content, json, data may be provided"
            )
        if not present:
            return None, None, None
        name = present[0]
        sv = kwargs[name]
        if name == "content":
            if sv.kind not in (spec.KIND_STR, spec.KIND_BYTES):
                raise HttpInvalidRequestError("content must be str or bytes")
            return sv.value, None, None
        if name == "json":
            try:
                return None, validate_json_value(sv.value), None
            except Exception as exc:
                raise HttpInvalidRequestError(f"json body is invalid: {exc}") from None
        if sv.kind == spec.KIND_DICT:
            return None, None, {str(k): v for k, v in sv.value.items()}
        if sv.kind in (spec.KIND_LIST, spec.KIND_TUPLE):
            pairs = []
            for item in sv.value:
                if not isinstance(item, (list, tuple)) or len(item) != 2:
                    raise HttpInvalidRequestError(
                        "data list must contain (name, value) pairs"
                    )
                pairs.append((str(item[0]), item[1]))
            return None, None, pairs
        raise HttpInvalidRequestError("data must be a dict or list of pairs")

    @staticmethod
    def _parse_bool(sv: SafeValue | None, name: str, *, default: bool) -> bool:
        if sv is None:
            return default
        if sv.kind != spec.KIND_BOOL:
            raise HttpInvalidRequestError(f"{name} must be a bool")
        return sv.value


__all__ = ["SafeHttpResponse", "ScriptHttpClient", "response_json"]
