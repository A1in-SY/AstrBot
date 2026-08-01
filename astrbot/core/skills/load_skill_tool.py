"""Request-scoped FunctionTool for loading a declared Skill definition."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

from pydantic import Field
from pydantic.dataclasses import dataclass

from astrbot.core import logger
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.computer.computer_client import get_booter
from astrbot.core.trace.context import current_trace_service

from .skill_manager import SkillInfo


@dataclass
class LoadSkillTool(FunctionTool[AstrAgentContext]):
    """Load the complete SKILL.md for one skill in the request inventory."""

    name: str = "load_skill"
    description: str = (
        "Load the complete SKILL.md instructions for one available skill. "
        "Use the exact skill name from the Skills inventory before following it."
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Exact name of the skill to load.",
                }
            },
            "required": ["name"],
            "additionalProperties": False,
        }
    )
    skills_by_name: dict[str, SkillInfo] = Field(
        default_factory=dict,
        exclude=True,
        repr=False,
    )
    runtime: str = Field(default="local", exclude=True, repr=False)
    trace_operation: str = Field(default="skill.load", exclude=True, repr=False)

    def __post_init__(self) -> None:
        """Copy the request inventory so later catalog updates cannot alter it."""

        self.skills_by_name = {
            skill.name: replace(skill) for skill in self.skills_by_name.values()
        }

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        name: str,
    ) -> ToolExecResult:
        """Load an inventory member without accepting an arbitrary filesystem path.

        Args:
            context: The current Agent execution context.
            name: Exact skill name from this request's frozen inventory.

        Returns:
            The complete Skill definition, or a user-visible error message.
        """
        skill = self.skills_by_name.get(name)
        if skill is None:
            return f"Error: skill `{name}` is not available in this session."

        try:
            if self.runtime == "local":
                content = await asyncio.to_thread(
                    Path(skill.path).read_text,
                    encoding="utf-8",
                )
            elif self.runtime == "sandbox":
                booter = await get_booter(
                    context.context.context,
                    context.context.event.unified_msg_origin,
                )
                result = await booter.fs.read_file(skill.path)
                if not isinstance(result, dict) or not result.get("success", True):
                    detail = (
                        str(result.get("error", ""))
                        if isinstance(result, dict)
                        else "invalid sandbox response"
                    )
                    raise RuntimeError(detail or "sandbox read failed")
                content = result.get("content")
                if not isinstance(content, str):
                    raise RuntimeError("sandbox returned non-text skill content")
            else:
                return "Error: Skill loading requires the local or sandbox runtime."
        except Exception as exc:  # noqa: BLE001 - return a normal tool result
            logger.warning("Failed to load skill %s: %s", skill.name, exc)
            return f"Error: failed to load skill `{skill.name}`."

        try:
            trace_service = current_trace_service()
            if trace_service is not None:
                trace_service.current_span().record_text(
                    "skill_definition",
                    content,
                    metadata={
                        "skill_name": skill.name,
                        "source_type": skill.source_type,
                    },
                )
        except Exception:  # noqa: BLE001 - tracing must never affect tool output
            pass
        return content
