from dataclasses import dataclass
from typing import Generic

import mcp

from astrbot.core.agent.tool import FunctionTool
from astrbot.core.provider.entities import LLMResponse

from .run_context import ContextWrapper, TContext

AGENT_LLM_HOOKS_API_VERSION = 1


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
