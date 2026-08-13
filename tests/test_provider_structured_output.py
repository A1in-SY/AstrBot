from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from astrbot.core.provider.entities import StructuredOutputSpec
from astrbot.core.provider.sources.anthropic_source import ProviderAnthropic
from astrbot.core.provider.sources.gemini_source import ProviderGoogleGenAI
from astrbot.core.provider.sources.openai_responses_source import (
    ProviderOpenAIResponses,
)
from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial

SCHEMA = {
    "type": "object",
    "properties": {"value": {"type": "string"}},
    "required": ["value"],
    "additionalProperties": False,
}
SPEC = StructuredOutputSpec(
    name="test_schema",
    description="Test schema",
    json_schema=SCHEMA,
)


def _openai_provider() -> ProviderOpenAIOfficial:
    return ProviderOpenAIOfficial(
        {
            "id": "openai",
            "type": "openai_chat_completion",
            "model": "gpt-test",
            "key": ["test-key"],
        },
        {},
    )


@pytest.mark.asyncio
async def test_openai_chat_maps_structured_output_to_response_format() -> None:
    provider = _openai_provider()
    provider._materialize_context_image_parts = AsyncMock(return_value=[])

    payload, _ = await provider._prepare_chat_payload(
        "test",
        structured_output=SPEC,
    )

    assert payload["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "test_schema",
            "description": "Test schema",
            "schema": SCHEMA,
            "strict": True,
        },
    }


@pytest.mark.asyncio
async def test_openai_responses_maps_structured_output_to_text_format() -> None:
    provider = ProviderOpenAIResponses(
        {
            "id": "responses",
            "provider": "openai",
            "type": "openai_responses",
            "model": "gpt-test",
            "key": ["test-key"],
            "api_base": "https://api.openai.com/v1",
        },
        {},
    )

    payload, _ = await provider._prepare_chat_payload(
        "test",
        structured_output=SPEC,
    )

    assert payload["text"]["format"] == {
        "type": "json_schema",
        "name": "test_schema",
        "description": "Test schema",
        "schema": SCHEMA,
        "strict": True,
    }


@pytest.mark.asyncio
async def test_anthropic_maps_structured_output_to_output_config() -> None:
    provider = ProviderAnthropic(
        {
            "id": "anthropic",
            "type": "anthropic_chat_completion",
            "model": "claude-test",
            "key": ["test-key"],
        },
        {},
    )
    provider._query = AsyncMock(return_value=object())

    await provider.text_chat("test", structured_output=SPEC)

    payload = provider._query.await_args.args[0]
    assert payload["output_config"] == {
        "format": {"type": "json_schema", "schema": SCHEMA}
    }


@pytest.mark.asyncio
async def test_gemini_maps_structured_output_to_generate_config() -> None:
    provider = ProviderGoogleGenAI.__new__(ProviderGoogleGenAI)
    provider.provider_settings = {}
    provider.provider_config = {
        "model": "gemini-test",
        "gm_thinking_config": {},
    }
    provider.model_name = "gemini-test"
    provider.safety_settings = []

    config = await provider._prepare_query_config(
        {"model": "gemini-test", "structured_output": SPEC},
    )

    assert config.response_mime_type == "application/json"
    assert config.response_json_schema == SCHEMA
