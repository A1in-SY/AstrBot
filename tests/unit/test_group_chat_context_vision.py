from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from astrbot.api.message_components import Image, Plain
from astrbot.api.provider import Provider
from astrbot.builtin_stars.astrbot.group_chat_context import GroupChatContext


def _result() -> str:
    images = []
    for image_id in ("image_1", "image_2"):
        images.append(
            {
                "image_id": image_id,
                "summary": image_id,
                "task_relevant_evidence": [],
                "ocr": {"full_text": "", "lines": []},
                "layout": {"regions": []},
                "semantics": {
                    "scene": "scene",
                    "intent": "",
                    "entities": [],
                    "relations": [],
                },
                "visual": {
                    "style": "photo",
                    "dominant_colors": [],
                    "notes": [],
                },
                "uncertainty": [],
                "embedded_instructions": [],
            }
        )
    return json.dumps(
        {
            "schema_version": "astrbot.vision_analysis.v1",
            "images": images,
            "cross_image_findings": [],
        }
    )


@pytest.mark.asyncio
async def test_group_context_batches_images_with_general_prompt() -> None:
    provider = MagicMock(spec=Provider)
    provider.provider_config = {
        "id": "vision",
        "modalities": ["text", "image"],
    }
    provider.text_chat = AsyncMock(return_value=MagicMock(completion_text=_result()))
    provider.supports_native_structured_output.return_value = False
    context = MagicMock()
    context.get_provider_by_id.return_value = provider
    group_context = GroupChatContext(MagicMock(), context)
    messages = [
        Plain("look"),
        Image(file="", url="https://example.com/one.png"),
        Image(file="", url="https://example.com/two.png"),
    ]
    event = MagicMock()
    event.message_obj = SimpleNamespace(
        sender=SimpleNamespace(nickname="Alice"),
        message=messages,
    )
    event.get_messages.return_value = messages

    formatted = await group_context._format_message(
        event,
        {
            "image_caption": True,
            "image_caption_provider_ids": ["vision"],
            "image_caption_native_structured_output": False,
            "request_max_retries": 5,
        },
    )

    assert provider.text_chat.await_count == 1
    assert provider.text_chat.await_args.kwargs["image_urls"] == [
        "https://example.com/one.png",
        "https://example.com/two.png",
    ]
    prompt = provider.text_chat.await_args.kwargs["prompt"]
    assert "Analysis mode: general" in prompt
    assert "<current_task>" not in prompt
    assert "Image Analysis schema=astrbot.vision_analysis.v1" in formatted
