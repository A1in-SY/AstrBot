import json
from datetime import datetime
from typing import Any

from pydantic import Field
from pydantic.dataclasses import dataclass

from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.cron.manager import CronJobSchedulingError
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType
from astrbot.core.tools.registry import builtin_tool
from astrbot.script_runtime import spec

_CRON_TOOL_CONFIG = {
    "provider_settings.proactive_capability.add_cron_tools": True,
}

_SCRIPT_TOOL_CONFIG = {
    "provider_settings.proactive_capability.add_cron_tools": True,
}


def _extract_job_session(job: Any) -> str | None:
    payload = getattr(job, "payload", None)
    if not isinstance(payload, dict):
        return None
    session = payload.get("session")
    return str(session) if session is not None else None


def _extract_job_sender(job: Any) -> str | None:
    payload = getattr(job, "payload", None)
    if not isinstance(payload, dict):
        return None
    sender_id = payload.get("sender_id")
    return str(sender_id) if sender_id is not None else None


def _job_manageable_by(job: Any, current_umo: str, current_sender_id: str) -> bool:
    """Check whether the current caller may manage a cron job.

    Group-chat jobs belong to the group session, so any member of the same
    group can manage them. Private-chat jobs remain bound to the creator.

    Args:
        job: The cron job to check.
        current_umo: The unified message origin of the current event.
        current_sender_id: The sender ID of the current event.

    Returns:
        True if the caller may manage the job.
    """
    job_session = _extract_job_session(job)
    if not job_session:
        return False
    is_group_job = False
    try:
        is_group_job = (
            MessageSession.from_str(job_session).message_type
            == MessageType.GROUP_MESSAGE
        )
    except ValueError:
        is_group_job = False
    if is_group_job:
        return job_session == current_umo
    return job_session == current_umo and _extract_job_sender(job) == current_sender_id


def _parse_run_at(run_at: Any) -> datetime | None:
    if run_at in (None, ""):
        return None
    return datetime.fromisoformat(str(run_at))


def _script_tool_authorized(context: AstrAgentContext) -> tuple[bool, str]:
    cfg = context.context.get_config()
    script_cfg = cfg.get("script_task") or {}
    if not isinstance(script_cfg, dict) or not script_cfg.get("enabled", False):
        return False, "script tasks are disabled"
    allowed = script_cfg.get("allowed_umos") or []
    if not isinstance(allowed, list) or context.event.unified_msg_origin not in allowed:
        return False, "this session is not in the script task allowlist"
    return True, ""


def _script_job_manageable_by(
    aggregate: Any,
    current_umo: str,
    current_sender_id: str,
) -> bool:
    script = aggregate.script
    try:
        is_group = (
            MessageSession.from_str(script.bound_umo).message_type
            == MessageType.GROUP_MESSAGE
        )
    except ValueError:
        is_group = False
    if is_group:
        return script.bound_umo == current_umo
    return (
        script.bound_umo == current_umo
        and script.creator_sender_id == current_sender_id
    )


def _validation_error_text(validation: Any) -> str:
    diagnostics = validation.diagnostics[:50]
    return json.dumps(
        {
            "error": "SCRIPT_VALIDATION_FAILED",
            "total_diagnostics": validation.total_diagnostics,
            "truncated": validation.total_diagnostics > 50,
            "diagnostics": [diagnostic.to_dict() for diagnostic in diagnostics],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


@builtin_tool(config=_SCRIPT_TOOL_CONFIG)
@dataclass
class ScriptTaskTool(FunctionTool[AstrAgentContext]):
    name: str = "script_task"
    description: str = (
        "Create and manage deterministic script tasks that run without an LLM. "
        "Use script_task when the workflow is a fixed mechanical sequence such as "
        "fetching a price, comparing it against a threshold and sending a message. "
        "For tasks that need future reasoning, natural-language understanding, "
        "arbitrary tool calls, or context-dependent answers, use future_task "
        "instead. When unsure, use future_task. "
        "Actions: create, edit, delete, list, get."
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "edit", "delete", "list", "get"],
                    "description": "Action to perform. 'list' takes no parameters. 'get' and 'delete' require only 'job_id'. 'edit' requires 'job_id' plus the fields to change.",
                },
                "name": {
                    "type": "string",
                    "description": "Optional task label.",
                },
                "note": {
                    "type": "string",
                    "description": "Optional human-readable note about the task.",
                },
                "source": {
                    "type": "string",
                    "description": (
                        "The script source. " + spec.build_compact_contract()
                    ),
                },
                "cron_expression": {
                    "type": "string",
                    "description": "Cron expression for a recurring schedule, e.g. '0 8 * * *' or '0 23 * * mon-fri'. Prefer named weekdays like 'mon-fri' or 'sat,sun' over numeric ranges like '1-5'.",
                },
                "run_once": {
                    "type": "boolean",
                    "description": "Run only once and delete after execution. Use with run_at.",
                },
                "run_at": {
                    "type": "string",
                    "description": "ISO datetime for one-time execution, e.g. 2026-02-02T08:00:00+08:00.",
                },
                "job_id": {
                    "type": "string",
                    "description": "Task ID. Required for 'delete', 'get' and 'edit'.",
                },
            },
            "required": ["action"],
        }
    )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        cron_mgr = context.context.context.cron_manager
        if cron_mgr is None:
            return "error: cron manager is not available."
        authorized, reason = _script_tool_authorized(context.context)
        if not authorized:
            return f"error: {reason}."

        action = str(kwargs.get("action") or "").strip().lower()
        umo = context.context.event.unified_msg_origin
        sender_id = str(context.context.event.get_sender_id())
        if action == "create":
            source = kwargs.get("source")
            if not isinstance(source, str) or not source.strip():
                return "error: source is required when action=create."
            cron_expression = kwargs.get("cron_expression")
            run_at = kwargs.get("run_at")
            run_once = bool(kwargs.get("run_once", False))
            name = str(kwargs.get("name") or "").strip() or "script_task"
            note = str(kwargs.get("note") or "").strip()
            if run_once and not run_at:
                return "error: run_at is required when run_once=true."
            if (not run_once) and not cron_expression:
                return "error: cron_expression is required when run_once=false."
            if run_once and cron_expression:
                cron_expression = None
            try:
                run_at_dt = _parse_run_at(run_at)
            except Exception:
                return "error: run_at must be ISO datetime, e.g., 2026-02-02T08:00:00+08:00"
            validation = await cron_mgr.validate_script_source(source)
            if not validation.valid:
                return _validation_error_text(validation)
            try:
                aggregate = await cron_mgr.add_script_job(
                    name=name,
                    cron_expression=str(cron_expression) if cron_expression else None,
                    source=source,
                    bound_umo=umo,
                    description=note or None,
                    run_once=run_once,
                    run_at=run_at_dt,
                    creator_sender_id=sender_id,
                )
            except Exception as exc:  # noqa: BLE001
                return f"error: failed to create script task: {exc}"
            job = aggregate.job
            next_run = job.next_run_time or run_at_dt
            suffix = (
                f"one-time at {next_run}"
                if run_once
                else f"expression '{cron_expression}' (next {next_run})"
            )
            return f"Created script task {job.job_id} ({job.name}) {suffix}."

        if action in ("edit", "delete", "get"):
            job_id = kwargs.get("job_id")
            if not job_id:
                return "error: job_id is required for this action."
            aggregate = await cron_mgr.get_script_job(str(job_id))
            if aggregate is None:
                return f"error: script task {job_id} not found."
            if not _script_job_manageable_by(aggregate, umo, sender_id):
                return "error: you can only manage script tasks created by yourself in private chats or any member in the same group."
            if action == "delete":
                await cron_mgr.delete_job(str(job_id))
                return f"Deleted script task {job_id}."
            if action == "get":
                script = aggregate.script
                job = aggregate.job
                return (
                    f"{job.job_id} | {job.name} | type={job.job_type} | "
                    f"enabled={job.enabled} | run_once={job.run_once} | "
                    f"cron={job.cron_expression} | run_at={job.payload.get('run_at') if isinstance(job.payload, dict) else None} | "
                    f"next={job.next_run_time} | status={job.status} | "
                    f"language={script.language_version} | state_keys={list((script.state or {}).keys())} "
                    f"state_size={len(json.dumps(script.state or {}, ensure_ascii=False).encode('utf-8'))}\n"
                    f"source:\n{script.source}"
                )
            # edit
            if not any(
                key in kwargs
                for key in (
                    "name",
                    "note",
                    "source",
                    "run_once",
                    "cron_expression",
                    "run_at",
                )
            ):
                return "error: no editable fields were provided."
            job = aggregate.job
            script = aggregate.script
            payload = dict(job.payload) if isinstance(job.payload, dict) else {}
            updates: dict[str, Any] = {}
            if "name" in kwargs:
                name = str(kwargs.get("name") or "").strip()
                if not name:
                    return "error: name cannot be empty."
                updates["name"] = name
            if "note" in kwargs:
                note = str(kwargs.get("note") or "").strip()
                updates["description"] = note or None
            if "source" in kwargs:
                source = kwargs.get("source")
                if not isinstance(source, str) or not source.strip():
                    return "error: source cannot be empty."
                validation = await cron_mgr.validate_script_source(source)
                if not validation.valid:
                    return _validation_error_text(validation)
                updates["source"] = source
            run_once = (
                bool(kwargs["run_once"]) if "run_once" in kwargs else bool(job.run_once)
            )
            cron_expression = (
                str(kwargs.get("cron_expression") or "").strip()
                if "cron_expression" in kwargs
                else job.cron_expression
            )
            cron_expression = cron_expression or None
            current_run_at = payload.get("run_at")
            try:
                run_at_dt = (
                    _parse_run_at(kwargs.get("run_at"))
                    if "run_at" in kwargs
                    else _parse_run_at(current_run_at)
                )
            except Exception:
                return "error: run_at must be ISO datetime, e.g., 2026-02-02T08:00:00+08:00"
            if run_once:
                if run_at_dt is None:
                    return "error: run_at is required when run_once=true."
                cron_expression = None
                updates["payload"] = {**payload, "run_at": run_at_dt.isoformat()}
            else:
                if not cron_expression:
                    return "error: cron_expression is required when run_once=false."
                updates["payload"] = {**payload}
                updates["payload"].pop("run_at", None)
            updates["run_once"] = run_once
            updates["cron_expression"] = cron_expression
            try:
                updated = await cron_mgr.update_job(str(job_id), **updates)
            except Exception as exc:  # noqa: BLE001
                return f"error: failed to update script task: {exc}"
            if updated is None:
                return f"error: script task {job_id} not found."
            return f"Updated script task {job_id} ({updated.name})."

        if action == "list":
            jobs = await cron_mgr.list_jobs("script")
            lines = []
            for job in jobs:
                if not _job_manageable_by(job, umo, sender_id):
                    aggregate = await cron_mgr.get_script_job(job.job_id)
                    if aggregate is None or not _script_job_manageable_by(
                        aggregate, umo, sender_id
                    ):
                        continue
                lines.append(
                    f"{job.job_id} | {job.name} | script | run_once={job.run_once} | enabled={job.enabled} | next={job.next_run_time} | status={job.status}"
                )
            return "\n".join(lines) if lines else "No script tasks found."

        return "error: action must be one of create, edit, delete, list, or get."


@builtin_tool(config=_CRON_TOOL_CONFIG)
@dataclass
class FutureTaskTool(FunctionTool[AstrAgentContext]):
    name: str = "future_task"
    description: str = (
        "Manage future agent tasks that run through the LLM when triggered. "
        "Use this when the scheduled work needs future reasoning, natural-language "
        "understanding, arbitrary tool calls, or context-dependent answers. "
        "For deterministic mechanical workflows (fetch/compare/notify) that can run "
        "without an LLM, use script_task instead. When unsure, use future_task. "
        "Actions: create, edit, delete, list, get."
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "edit", "delete", "list", "get"],
                    "description": "Action to perform. 'list' takes no parameters. 'get' and 'delete' require only 'job_id'. 'edit' requires 'job_id' plus the fields to change.",
                },
                "name": {
                    "type": "string",
                    "description": "Optional task label.",
                },
                "cron_expression": {
                    "type": "string",
                    "description": "Cron expression for a recurring schedule, e.g. '0 8 * * *' or '0 23 * * mon-fri'. Prefer named weekdays like 'mon-fri' or 'sat,sun' over numeric ranges like '1-5'.",
                },
                "note": {
                    "type": "string",
                    "description": "Detailed instructions for your future agent to execute when it wakes.",
                },
                "run_once": {
                    "type": "boolean",
                    "description": "Run only once and delete after execution. Use with run_at.",
                },
                "run_at": {
                    "type": "string",
                    "description": "ISO datetime for one-time execution, e.g. 2026-02-02T08:00:00+08:00.",
                },
                "job_id": {
                    "type": "string",
                    "description": "Task ID. Required for 'delete' and 'edit'.",
                },
            },
            "required": ["action"],
        }
    )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        cron_mgr = context.context.context.cron_manager
        if cron_mgr is None:
            return "error: cron manager is not available."

        action = str(kwargs.get("action") or "").strip().lower()
        if action == "create":
            cron_expression = kwargs.get("cron_expression")
            run_at = kwargs.get("run_at")
            run_once = bool(kwargs.get("run_once", False))
            note = str(kwargs.get("note", "")).strip()
            name = str(kwargs.get("name") or "").strip() or "active_agent_task"

            if not note:
                return "error: note is required when action=create."
            if run_once and not run_at:
                return "error: run_at is required when run_once=true."
            if (not run_once) and not cron_expression:
                return "error: cron_expression is required when run_once=false."
            if run_once and cron_expression:
                cron_expression = None
            try:
                run_at_dt = _parse_run_at(run_at)
            except Exception:
                return "error: run_at must be ISO datetime, e.g., 2026-02-02T08:00:00+08:00"

            payload = {
                "session": context.context.event.unified_msg_origin,
                "sender_id": context.context.event.get_sender_id(),
                "note": note,
                "origin": "tool",
            }

            try:
                job = await cron_mgr.add_active_job(
                    name=name,
                    cron_expression=str(cron_expression) if cron_expression else None,
                    payload=payload,
                    description=note,
                    run_once=run_once,
                    run_at=run_at_dt,
                )
            except CronJobSchedulingError:
                return "error: failed to schedule task due to invalid configuration."
            next_run = job.next_run_time or run_at_dt
            suffix = (
                f"one-time at {next_run}"
                if run_once
                else f"expression '{cron_expression}' (next {next_run})"
            )
            return f"Scheduled future task {job.job_id} ({job.name}) {suffix}."

        current_umo = context.context.event.unified_msg_origin
        current_sender_id = str(context.context.event.get_sender_id())
        if action == "edit":
            job_id = kwargs.get("job_id")
            if not job_id:
                return "error: job_id is required when action=edit."
            if not any(
                key in kwargs
                for key in ("name", "note", "run_once", "cron_expression", "run_at")
            ):
                return "error: no editable fields were provided."

            job = await cron_mgr.db.get_cron_job(str(job_id))
            if not job:
                return f"error: cron job {job_id} not found."
            if job.job_type != "active_agent":
                return f"error: cron job {job_id} is not an agent task."
            if not _job_manageable_by(job, current_umo, current_sender_id):
                return "error: you can only edit your own future tasks."

            payload = dict(job.payload) if isinstance(job.payload, dict) else {}

            updates: dict[str, Any] = {}
            if "name" in kwargs:
                name = str(kwargs.get("name") or "").strip()
                if not name:
                    return "error: name cannot be empty when action=edit."
                updates["name"] = name

            if "note" in kwargs:
                note = str(kwargs.get("note") or "").strip()
                if not note:
                    return "error: note cannot be empty when action=edit."
                payload["note"] = note
                updates["description"] = note

            current_run_at = payload.get("run_at")
            run_once = (
                bool(kwargs["run_once"]) if "run_once" in kwargs else bool(job.run_once)
            )
            cron_expression = (
                str(kwargs.get("cron_expression") or "").strip()
                if "cron_expression" in kwargs
                else job.cron_expression
            )
            cron_expression = cron_expression or None

            try:
                run_at_dt = (
                    _parse_run_at(kwargs.get("run_at"))
                    if "run_at" in kwargs
                    else _parse_run_at(current_run_at)
                )
            except Exception:
                return "error: run_at must be ISO datetime, e.g., 2026-02-02T08:00:00+08:00"

            if run_once:
                if run_at_dt is None:
                    return "error: run_at is required when run_once=true."
                cron_expression = None
                payload["run_at"] = run_at_dt.isoformat()
            else:
                if not cron_expression:
                    return "error: cron_expression is required when run_once=false."
                payload.pop("run_at", None)

            updates["run_once"] = run_once
            updates["cron_expression"] = cron_expression
            updates["payload"] = payload

            try:
                job = await cron_mgr.update_job(str(job_id), **updates)
            except CronJobSchedulingError:
                return "error: failed to update task due to invalid configuration."
            if not job:
                return f"error: cron job {job_id} not found."
            return f"Updated future task {job.job_id} ({job.name})."

        if action == "delete":
            job_id = kwargs.get("job_id")
            if not job_id:
                return "error: job_id is required when action=delete."
            job = await cron_mgr.db.get_cron_job(str(job_id))
            if not job:
                return f"error: cron job {job_id} not found."
            if job.job_type != "active_agent":
                return f"error: cron job {job_id} is not an agent task."
            if not _job_manageable_by(job, current_umo, current_sender_id):
                return "error: you can only delete your own future tasks."
            await cron_mgr.delete_job(str(job_id))
            return f"Deleted cron job {job_id}."

        if action == "list":
            jobs = [
                job
                for job in await cron_mgr.list_jobs()
                if job.job_type == "active_agent"
                and _job_manageable_by(job, current_umo, current_sender_id)
            ]
            if not jobs:
                return "No cron jobs found."
            lines = []
            for j in jobs:
                lines.append(
                    f"{j.job_id} | {j.name} | {j.job_type} | run_once={getattr(j, 'run_once', False)} | enabled={j.enabled} | next={j.next_run_time}"
                )
            return "\n".join(lines)

        if action == "get":
            job_id = kwargs.get("job_id")
            if not job_id:
                return "error: job_id is required when action=get."
            job = await cron_mgr.db.get_cron_job(str(job_id))
            if not job:
                return f"error: cron job {job_id} not found."
            if job.job_type != "active_agent":
                return f"error: cron job {job_id} is not an agent task."
            if not _job_manageable_by(job, current_umo, current_sender_id):
                return "error: you can only get your own future tasks."
            payload = job.payload if isinstance(job.payload, dict) else {}
            note = payload.get("note") or job.description or ""
            return (
                f"{job.job_id} | {job.name} | type={job.job_type} | "
                f"enabled={job.enabled} | run_once={job.run_once} | "
                f"cron={job.cron_expression} | run_at={payload.get('run_at')} | "
                f"next={job.next_run_time} | status={job.status}\n"
                f"note: {note}"
            )

        return "error: action must be one of create, edit, delete, list, or get."


__all__ = [
    "FutureTaskTool",
    "ScriptTaskTool",
]
