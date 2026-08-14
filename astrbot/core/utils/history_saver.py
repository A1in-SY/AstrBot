import json

from astrbot import logger
from astrbot.core.conversation_mgr import ConversationManager
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.trace.history_instrumentation import agent_history_persist_context


async def persist_agent_history(
    conversation_manager: ConversationManager,
    *,
    event: AstrMessageEvent,
    req: ProviderRequest,
    summary_note: str,
) -> None:
    """Persist agent interaction into conversation history."""
    if not req or not req.conversation:
        return

    history = []
    try:
        history = json.loads(req.conversation.history or "[]")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to parse conversation history: %s", exc)
    history.append({"role": "user", "content": "Output your last task result below."})
    history.append({"role": "assistant", "content": summary_note})
    with agent_history_persist_context("background_agent_result"):
        await conversation_manager.update_conversation(
            event.unified_msg_origin,
            req.conversation.cid,
            history=history,
        )
