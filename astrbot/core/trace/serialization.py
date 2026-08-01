"""Known-domain serializers for durable Trace artifacts.

The Trace runtime must never invoke arbitrary ``to_dict``/``model_dump`` hooks
from platform SDK objects.  This module therefore serializes only the Core
objects it explicitly knows and turns opaque values into small type markers.
"""

from __future__ import annotations

import hashlib
import math
import re
from enum import Enum
from pathlib import Path
from typing import Any

from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.provider.entities import LLMResponse, TokenUsage

_DATA_URI_RE = re.compile(r"^data:([^;,]+)?(?:;[^,]*)?;base64,", re.IGNORECASE)
_BASE64_RE = re.compile(r"^[A-Za-z0-9+/=_-]+$")
_MAX_CONTAINER_DEPTH = 32


def normalize_trace_value(value: Any, *, _depth: int = 0) -> Any:
    """Return a JSON-compatible snapshot without serializing opaque objects.

    Known inline media encodings are represented by a hash and metadata rather
    than copied into the trace.  The function intentionally does not read file
    paths, resolve URLs, or invoke custom object serialization methods.
    """

    if _depth >= _MAX_CONTAINER_DEPTH:
        return {"truncated": True, "reason": "max_depth"}
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return _normalize_string(value)
    if isinstance(value, bytes):
        return {
            "binary_ref": {
                "sha256": hashlib.sha256(value).hexdigest(),
                "size": len(value),
            }
        }
    if isinstance(value, Path):
        return {"file_ref": str(value)}
    if isinstance(value, Enum):
        return normalize_trace_value(value.value, _depth=_depth + 1)
    if isinstance(value, dict):
        return {
            str(key): normalize_trace_value(item, _depth=_depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [normalize_trace_value(item, _depth=_depth + 1) for item in value]
    return {"opaque_type": type(value).__name__}


def tool_set_manifest(tool_set: ToolSet | None) -> list[dict[str, Any]] | None:
    """Serialize Core FunctionTool definitions without handler objects."""

    if tool_set is None:
        return None
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "parameters": normalize_trace_value(tool.parameters),
            "active": tool.active,
            "is_background_task": tool.is_background_task,
            "origin_module": tool.handler_module_path,
        }
        for tool in tool_set.tools
    ]


def function_tool_manifest(tool: FunctionTool) -> dict[str, Any]:
    """Serialize one Core FunctionTool definition without its callable."""

    return {
        "name": tool.name,
        "description": tool.description,
        "parameters": normalize_trace_value(tool.parameters),
        "active": tool.active,
        "is_background_task": tool.is_background_task,
        "origin_module": tool.handler_module_path,
    }


def message_chain_manifest(chain: MessageChain | None) -> dict[str, Any] | None:
    """Serialize a MessageChain by inspecting known component data only."""

    if chain is None:
        return None
    components: list[dict[str, Any]] = []
    for component in chain.chain:
        raw_data = {
            key: value
            for key, value in vars(component).items()
            if not key.startswith("_") and key != "type"
        }
        component_type = getattr(component, "type", type(component).__name__)
        components.append(
            {
                "type": normalize_trace_value(component_type),
                "data": normalize_trace_value(raw_data),
            }
        )
    return {
        "type": chain.type,
        "use_t2i": chain.use_t2i_,
        "use_markdown": chain.use_markdown_,
        "components": components,
    }


def provider_request_manifest(
    method: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    """Build a normalized semantic request manifest for a Provider operation."""

    values = dict(kwargs)
    tool_set = values.pop("func_tool", None)
    if not isinstance(tool_set, ToolSet):
        tool_set_manifest_value: list[dict[str, Any]] | None = None
        if tool_set is not None:
            tool_set_manifest_value = [{"opaque_type": type(tool_set).__name__}]
    else:
        tool_set_manifest_value = tool_set_manifest(tool_set)
    return {
        "method": method,
        "args": normalize_trace_value(list(args)),
        "kwargs": normalize_trace_value(values),
        "tools": tool_set_manifest_value,
    }


def provider_response_manifest(result: Any) -> dict[str, Any]:
    """Build the semantic final response manifest for a Provider operation."""

    if isinstance(result, LLMResponse):
        return {
            "response_type": "LLMResponse",
            "role": result.role,
            "completion_text": normalize_trace_value(result.completion_text),
            "result_chain": message_chain_manifest(result.result_chain),
            "tools_call_name": normalize_trace_value(result.tools_call_name),
            "tools_call_args": normalize_trace_value(result.tools_call_args),
            "tools_call_ids": normalize_trace_value(result.tools_call_ids),
            "tools_call_extra_content": normalize_trace_value(
                result.tools_call_extra_content
            ),
            "reasoning_content": normalize_trace_value(result.reasoning_content),
            "reasoning_signature": normalize_trace_value(result.reasoning_signature),
            "response_id": result.id,
            "usage": token_usage_manifest(result.usage),
            "is_chunk": result.is_chunk,
        }
    return {
        "response_type": type(result).__name__,
        "value": normalize_trace_value(result),
    }


def token_usage_manifest(usage: TokenUsage | None) -> dict[str, int] | None:
    """Return only explicitly reported usage fields, never estimates as usage."""

    if usage is None:
        return None
    return {
        "input_other": usage.input_other,
        "input_cached": usage.input_cached,
        "output": usage.output,
        "input": usage.input,
        "total": usage.total,
    }


def call_tool_result_manifest(result: Any) -> dict[str, Any]:
    """Serialize the normalized MCP-style result visible to the Agent."""

    if result is None:
        return {"result_type": "none"}
    content = getattr(result, "content", None)
    if not isinstance(content, list):
        return {
            "result_type": type(result).__name__,
            "value": normalize_trace_value(result),
        }
    normalized_content: list[dict[str, Any]] = []
    for item in content:
        item_type = getattr(item, "type", type(item).__name__)
        if item_type == "text":
            normalized_content.append(
                {
                    "type": "text",
                    "text": normalize_trace_value(getattr(item, "text", None)),
                }
            )
            continue
        if item_type == "image":
            normalized_content.append(
                {
                    "type": "image",
                    "data": _known_encoded_media_reference(
                        getattr(item, "data", None),
                        media_type=getattr(item, "mimeType", None),
                        encoding="mcp_image_base64",
                    ),
                    "mime_type": normalize_trace_value(getattr(item, "mimeType", None)),
                }
            )
            continue
        resource = getattr(item, "resource", None)
        if resource is not None:
            normalized_content.append(
                {
                    "type": "resource",
                    "resource_type": type(resource).__name__,
                    "uri": normalize_trace_value(getattr(resource, "uri", None)),
                    "text": normalize_trace_value(getattr(resource, "text", None)),
                    "blob": _known_encoded_media_reference(
                        getattr(resource, "blob", None),
                        media_type=getattr(resource, "mimeType", None),
                        encoding="mcp_resource_blob",
                    ),
                    "mime_type": normalize_trace_value(
                        getattr(resource, "mimeType", None)
                    ),
                }
            )
            continue
        normalized_content.append(
            {
                "type": normalize_trace_value(item_type),
                "value": {"opaque_type": type(item).__name__},
            }
        )
    return {
        "result_type": type(result).__name__,
        "is_error": bool(getattr(result, "isError", False)),
        "content": normalized_content,
    }


def _normalize_string(value: str) -> str | dict[str, Any]:
    data_uri_match = _DATA_URI_RE.match(value)
    if data_uri_match:
        return _encoded_media_reference(
            value,
            media_type=data_uri_match.group(1) or "application/octet-stream",
            encoding="data_uri_base64",
        )
    if value.startswith("base64://"):
        return _encoded_media_reference(
            value,
            media_type="application/octet-stream",
            encoding="base64_uri",
        )
    if len(value) >= 4096 and _BASE64_RE.fullmatch(value):
        return _encoded_media_reference(
            value,
            media_type="application/octet-stream",
            encoding="probable_base64",
        )
    return value


def _encoded_media_reference(
    value: str,
    *,
    media_type: str,
    encoding: str,
) -> dict[str, Any]:
    return {
        "media_ref": {
            "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
            "encoded_size": len(value),
            "media_type": media_type,
            "encoding": encoding,
        }
    }


def _known_encoded_media_reference(
    value: Any,
    *,
    media_type: str | None,
    encoding: str,
) -> Any:
    """Represent a known MCP media field by reference regardless of its size."""

    if not isinstance(value, str):
        return normalize_trace_value(value)
    return _encoded_media_reference(
        value,
        media_type=media_type or "application/octet-stream",
        encoding=encoding,
    )
