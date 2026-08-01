"""Focused behavior tests for the request-scoped Skill loader."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from astrbot.core.agent.runners.tool_loop_agent_runner import _trace_tool_operation
from astrbot.core.skills.load_skill_tool import LoadSkillTool
from astrbot.core.skills.skill_manager import SkillInfo


@pytest.mark.asyncio
async def test_load_skill_reads_frozen_local_inventory_and_records_definition(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    skill_path = tmp_path / "demo" / "SKILL.md"
    skill_path.parent.mkdir()
    skill_path.write_text("# Demo\nFollow these instructions.\n", encoding="utf-8")
    catalog = {
        "demo": SkillInfo(
            name="demo",
            description="Demo skill",
            path=str(skill_path),
            active=True,
        )
    }
    tool = LoadSkillTool(skills_by_name=catalog, runtime="local")
    catalog.clear()

    span = MagicMock()
    trace_service = MagicMock()
    trace_service.current_span.return_value = span
    monkeypatch.setattr(
        "astrbot.core.skills.load_skill_tool.current_trace_service",
        lambda: trace_service,
    )

    result = await tool.call(MagicMock(), name="demo")

    assert result == "# Demo\nFollow these instructions.\n"
    assert tool.trace_operation == "skill.load"
    span.record_text.assert_called_once_with(
        "skill_definition",
        result,
        metadata={"skill_name": "demo", "source_type": "local_only"},
    )
    trace_service.start_span.assert_not_called()


def test_load_skill_uses_the_exclusive_skill_trace_operation() -> None:
    tool = LoadSkillTool()

    assert _trace_tool_operation(tool) == "skill.load"


@pytest.mark.asyncio
async def test_load_skill_rejects_names_outside_frozen_inventory(tmp_path) -> None:
    skill_path = tmp_path / "allowed" / "SKILL.md"
    skill_path.parent.mkdir()
    skill_path.write_text("allowed", encoding="utf-8")
    tool = LoadSkillTool(
        skills_by_name={
            "allowed": SkillInfo(
                name="allowed",
                description="Allowed skill",
                path=str(skill_path),
                active=True,
            )
        },
        runtime="local",
    )

    result = await tool.call(MagicMock(), name="not-in-inventory")

    assert result == "Error: skill `not-in-inventory` is not available in this session."


@pytest.mark.asyncio
async def test_load_skill_reads_sandbox_catalog_via_filesystem_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    booter = MagicMock()
    booter.fs.read_file = AsyncMock(
        return_value={"success": True, "content": "# Sandbox skill\n"}
    )
    get_booter = AsyncMock(return_value=booter)
    monkeypatch.setattr(
        "astrbot.core.skills.load_skill_tool.get_booter",
        get_booter,
    )
    tool = LoadSkillTool(
        skills_by_name={
            "sandbox-demo": SkillInfo(
                name="sandbox-demo",
                description="Sandbox skill",
                path="/workspace/skills/sandbox-demo/SKILL.md",
                active=True,
                source_type="sandbox_only",
                local_exists=False,
                sandbox_exists=True,
            )
        },
        runtime="sandbox",
    )
    context = MagicMock()
    context.context.context = MagicMock()
    context.context.event.unified_msg_origin = "platform:private:session"

    result = await tool.call(context, name="sandbox-demo")

    assert result == "# Sandbox skill\n"
    get_booter.assert_awaited_once_with(
        context.context.context,
        "platform:private:session",
    )
    booter.fs.read_file.assert_awaited_once_with(
        "/workspace/skills/sandbox-demo/SKILL.md"
    )
