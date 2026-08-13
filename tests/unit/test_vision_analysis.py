from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from astrbot.core.provider import Provider
from astrbot.core.vision import VisionAnalysisError, VisionImageAsset, analyze_images
from astrbot.core.vision.models import VisionAnalysisResult, parse_vision_result
from astrbot.core.vision.prompts import build_analysis_prompt


def _image_result(image_id: str) -> dict:
    return {
        "image_id": image_id,
        "summary": f"summary {image_id}",
        "task_relevant_evidence": [
            {
                "kind": "visual",
                "observation": "a visible object",
                "support": "center of the image",
            }
        ],
        "ocr": {"full_text": "", "lines": []},
        "layout": {"regions": []},
        "semantics": {
            "scene": "test scene",
            "intent": "",
            "entities": [],
            "relations": [],
        },
        "visual": {"style": "photo", "dominant_colors": [], "notes": []},
        "uncertainty": [],
        "embedded_instructions": [],
    }


def _result(*image_ids: str) -> str:
    return json.dumps(
        {
            "schema_version": "astrbot.vision_analysis.v1",
            "images": [_image_result(image_id) for image_id in image_ids],
            "cross_image_findings": [],
        }
    )


def _provider(
    provider_id: str,
    outputs: list[str | Exception],
    *,
    native: bool = False,
) -> MagicMock:
    provider = MagicMock(spec=Provider)
    provider.provider_config = {
        "id": provider_id,
        "modalities": ["text", "image"],
    }
    provider.supports_native_structured_output.return_value = native
    side_effect = []
    for output in outputs:
        if isinstance(output, Exception):
            side_effect.append(output)
        else:
            side_effect.append(MagicMock(completion_text=output))
    provider.text_chat = AsyncMock(side_effect=side_effect)
    return provider


def test_parse_vision_result_rejects_extra_fields() -> None:
    payload = json.loads(_result("image_1"))
    payload["images"][0]["unsupported"] = "not allowed"

    with pytest.raises(ValueError):
        parse_vision_result(json.dumps(payload))


def test_general_prompt_does_not_include_conversation_context() -> None:
    asset = VisionImageAsset("image_1", "/tmp/image.png", "group_context")

    prompt = build_analysis_prompt(
        [asset],
        mode="general",
        task_context="secret current task",
        quoted_context="secret quote",
        extra_focus="secret focus",
    )

    assert "secret current task" not in prompt
    assert "secret quote" not in prompt
    assert "secret focus" not in prompt
    assert "Do not assume a current question" in prompt


@pytest.mark.asyncio
async def test_analyze_images_repairs_invalid_schema_once() -> None:
    provider = _provider("vision", ["not json", _result("image_1")])

    result = await analyze_images(
        [VisionImageAsset("image_1", "/tmp/image.png")],
        [provider],
        mode="task",
        task_context="What is shown?",
    )

    assert isinstance(result, VisionAnalysisResult)
    assert provider.text_chat.await_count == 2
    retry_prompt = provider.text_chat.await_args_list[1].kwargs["prompt"]
    assert "previous response failed local validation" in retry_prompt


@pytest.mark.asyncio
async def test_analyze_images_does_not_schema_retry_transport_failure() -> None:
    primary = _provider("primary", [RuntimeError("network down")])
    fallback = _provider("fallback", [_result("image_1")])

    result = await analyze_images(
        [VisionImageAsset("image_1", "/tmp/image.png")],
        [primary, fallback],
        mode="general",
    )

    assert result.images[0].image_id == "image_1"
    assert primary.text_chat.await_count == 1
    assert fallback.text_chat.await_count == 1


@pytest.mark.asyncio
async def test_analyze_images_compensates_only_missing_images() -> None:
    provider = _provider(
        "vision",
        [_result("image_1"), _result("image_2")],
    )

    result = await analyze_images(
        [
            VisionImageAsset("image_1", "/tmp/one.png"),
            VisionImageAsset("image_2", "/tmp/two.png"),
        ],
        [provider],
        mode="task",
        task_context="Compare them",
    )

    assert [image.image_id for image in result.images] == ["image_1", "image_2"]
    assert provider.text_chat.await_count == 2
    assert provider.text_chat.await_args_list[1].kwargs["image_urls"] == [
        "/tmp/two.png"
    ]


@pytest.mark.asyncio
async def test_image_id_correction_is_the_only_schema_retry() -> None:
    provider = _provider(
        "vision",
        [_result("wrong_id"), _result("image_1")],
    )

    result = await analyze_images(
        [VisionImageAsset("image_1", "/tmp/image.png")],
        [provider],
        mode="general",
    )

    assert result.images[0].image_id == "image_1"
    assert provider.text_chat.await_count == 2


@pytest.mark.asyncio
async def test_strict_native_output_skips_unsupported_provider() -> None:
    unsupported = _provider("unsupported", [_result("image_1")], native=False)
    supported = _provider("supported", [_result("image_1")], native=True)

    result = await analyze_images(
        [VisionImageAsset("image_1", "/tmp/image.png")],
        [unsupported, supported],
        mode="general",
        native_structured_output=True,
    )

    assert result.images[0].image_id == "image_1"
    unsupported.text_chat.assert_not_awaited()
    assert supported.text_chat.await_args.kwargs["structured_output"].strict is True


@pytest.mark.asyncio
async def test_all_provider_failures_are_sanitized() -> None:
    provider = _provider("vision", [RuntimeError("secret upstream details")])

    with pytest.raises(VisionAnalysisError) as error:
        await analyze_images(
            [VisionImageAsset("image_1", "/tmp/image.png")],
            [provider],
            mode="general",
        )

    assert error.value.failures[0].provider_id == "vision"
    assert error.value.failures[0].error_type == "RuntimeError"
    assert "secret upstream details" not in str(error.value)
