from collections import Counter
from dataclasses import dataclass
from typing import Any, Generic, Literal

import mcp

from astrbot.core.agent.tool import FunctionTool
from astrbot.core.provider.entities import LLMResponse

from .run_context import ContextWrapper, TContext

AGENT_LLM_HOOKS_API_VERSION = 1


@dataclass(frozen=True, slots=True)
class AgentLLMCallRequestInfo:
    """Provider dispatch details for one logical Agent LLM request.

    ``latest_user_text`` contains only caller-supplied text from the last
    user-role message that was sent to the provider. It deliberately excludes
    system, assistant, tool, thinking, image, audio, and injected extra-user
    content.
    """

    call_kind: Literal[
        "main",
        "fallback",
        "skills_like_requery",
        "skills_like_repair",
        "context_compression",
    ]
    provider_id: str | None
    request_model: str | None
    latest_user_text: str | None


@dataclass(frozen=True, slots=True)
class AgentLLMCallResult:
    """Terminal state for one logical Agent-to-Provider LLM request.

    The response and exception fields retain the original objects returned or
    raised by the provider so hook implementations can inspect provider-specific
    metadata without an additional conversion layer.
    """

    elapsed_seconds: float
    response: LLMResponse | None
    exception: BaseException | None
    cancelled: bool
    request_info: AgentLLMCallRequestInfo | None = None


def _build_agent_llm_call_request_info(
    *,
    call_kind: Literal[
        "main",
        "fallback",
        "skills_like_requery",
        "skills_like_repair",
        "context_compression",
    ],
    provider: Any,
    contexts: list[Any],
    explicit_model: str | None = None,
    excluded_user_text_parts: list[str] | None = None,
) -> AgentLLMCallRequestInfo:
    """Build safe request metadata from the exact provider dispatch context.

    Args:
        call_kind: The runner path that initiated the provider request.
        provider: The provider that will receive the request.
        contexts: Sanitized messages passed to that provider.
        explicit_model: The model argument supplied for this dispatch, if any.
        excluded_user_text_parts: Text blocks injected by the caller rather than
            supplied as the user's prompt.

    Returns:
        Immutable metadata for lifecycle hooks without serializing raw messages.
    """
    provider_id: str | None = None
    provider_config = getattr(provider, "provider_config", None)
    if isinstance(provider_config, dict):
        configured_id = provider_config.get("id")
        if isinstance(configured_id, str) and configured_id:
            provider_id = configured_id

    request_model = explicit_model if isinstance(explicit_model, str) else None
    if not request_model:
        try:
            current_model = provider.get_model()
        except Exception:  # noqa: BLE001 - metadata must not affect dispatch.
            current_model = None
        if isinstance(current_model, str) and current_model:
            request_model = current_model

    excluded_text_counts = Counter(
        text for text in excluded_user_text_parts or [] if text
    )
    latest_user_text: str | None = None
    for context in reversed(contexts):
        if isinstance(context, dict):
            role = context.get("role")
            content = context.get("content")
        else:
            role = getattr(context, "role", None)
            content = getattr(context, "content", None)
        if role != "user":
            continue

        if isinstance(content, str):
            latest_user_text = None if excluded_text_counts.get(content, 0) else content
            break
        if not isinstance(content, list):
            break

        text_parts_reversed: list[str] = []
        for part in reversed(content):
            if isinstance(part, dict):
                part_type = part.get("type")
                text = part.get("text")
            else:
                part_type = getattr(part, "type", None)
                text = getattr(part, "text", None)
            if part_type == "text" and isinstance(text, str):
                if excluded_text_counts.get(text, 0):
                    excluded_text_counts[text] -= 1
                    continue
                text_parts_reversed.append(text)
        latest_user_text = (
            "".join(reversed(text_parts_reversed)) if text_parts_reversed else None
        )
        break

    return AgentLLMCallRequestInfo(
        call_kind=call_kind,
        provider_id=provider_id,
        request_model=request_model,
        latest_user_text=latest_user_text,
    )


class BaseAgentRunHooks(Generic[TContext]):
    async def on_agent_begin(self, run_context: ContextWrapper[TContext]) -> None: ...
    async def on_llm_start(
        self,
        run_context: ContextWrapper[TContext],
        round_index: int,
    ) -> None:
        """Run immediately before one logical Agent-to-Provider LLM request."""
        ...

    async def on_llm_end(
        self,
        run_context: ContextWrapper[TContext],
        round_index: int,
        result: AgentLLMCallResult,
    ) -> None:
        """Run exactly once when one logical Agent-to-Provider LLM request ends."""
        ...

    async def on_tool_start(
        self,
        run_context: ContextWrapper[TContext],
        tool: FunctionTool,
        tool_args: dict | None,
    ) -> None: ...
    async def on_tool_end(
        self,
        run_context: ContextWrapper[TContext],
        tool: FunctionTool,
        tool_args: dict | None,
        tool_result: mcp.types.CallToolResult | None,
    ) -> None: ...
    async def on_agent_done(
        self,
        run_context: ContextWrapper[TContext],
        llm_response: LLMResponse,
    ) -> None: ...
