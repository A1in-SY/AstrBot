from __future__ import annotations

import time
import uuid
from typing import Literal

from pydantic import ValidationError

from astrbot import logger
from astrbot.core.provider import Provider
from astrbot.core.provider.entities import StructuredOutputSpec

from .models import (
    VisionAnalysisError,
    VisionAnalysisResult,
    VisionImageAsset,
    VisionOutputValidationError,
    VisionProviderFailure,
    inspect_image_ids,
    parse_vision_result,
)
from .prompts import VISION_EVIDENCE_SYSTEM_PROMPT, build_analysis_prompt


def _validation_summary(exc: Exception) -> list[str]:
    """Produce bounded, non-sensitive errors for the correction prompt.

    Args:
        exc: Parsing, schema, or image-manifest validation failure.

    Returns:
        At most eight concise error descriptions.
    """
    cause = exc.__cause__ if isinstance(exc, VisionOutputValidationError) else exc
    if isinstance(cause, ValidationError):
        issues = []
        for error in cause.errors(include_url=False, include_input=False)[:8]:
            location = ".".join(str(part) for part in error.get("loc", ()))
            issues.append(f"{location or '<root>'}: {error.get('msg', 'invalid')}")
        return issues or ["response does not match the required schema"]
    if isinstance(exc, VisionOutputValidationError):
        return [str(exc)[:300] or "response violates the output contract"]
    return [exc.__class__.__name__]


async def _call_and_parse(
    provider: Provider,
    assets: list[VisionImageAsset],
    *,
    mode: Literal["task", "general"],
    task_context: str,
    quoted_context: str,
    extra_focus: str,
    native_structured_output: bool,
    request_max_retries: int | None,
    validation_errors: list[str] | None = None,
) -> VisionAnalysisResult:
    """Call one visual provider once and locally validate its response.

    Args:
        provider: Visual-capable chat provider.
        assets: Images included in this call.
        mode: Task-aware or generic analysis mode.
        task_context: Current user task in task-aware mode.
        quoted_context: Direct quoted-message text in task-aware mode.
        extra_focus: Optional configured focus in task-aware mode.
        native_structured_output: Whether to require upstream schema enforcement.
        request_max_retries: Transport retry attempts delegated to the provider.
        validation_errors: Errors included only during the correction retry.

    Returns:
        Locally schema-validated output. Image IDs are checked by the caller.

    Raises:
        Exception: If transport, output parsing, or schema validation fails.
    """
    prompt = build_analysis_prompt(
        assets,
        mode=mode,
        task_context=task_context,
        quoted_context=quoted_context,
        extra_focus=extra_focus,
        validation_errors=validation_errors,
    )
    kwargs = {}
    if native_structured_output:
        kwargs["structured_output"] = StructuredOutputSpec(
            name="astrbot_vision_analysis",
            description="Structured, evidence-grounded analysis of supplied images.",
            json_schema=VisionAnalysisResult.model_json_schema(),
            strict=True,
        )
    response = await provider.text_chat(
        prompt=prompt,
        system_prompt=VISION_EVIDENCE_SYSTEM_PROMPT,
        image_urls=[asset.image_url for asset in assets],
        session_id=uuid.uuid4().hex,
        request_max_retries=request_max_retries,
        require_image_input=True,
        persist=False,
        **kwargs,
    )
    return parse_vision_result(response.completion_text)


async def analyze_images(
    assets: list[VisionImageAsset],
    providers: list[Provider],
    *,
    mode: Literal["task", "general"],
    task_context: str = "",
    quoted_context: str = "",
    extra_focus: str = "",
    native_structured_output: bool = False,
    request_max_retries: int | None = None,
) -> VisionAnalysisResult:
    """Analyze images with schema validation and ordered provider fallback.

    A provider gets one correction retry for malformed output. A structurally valid
    batch that omits images is repaired with per-image calls for only the missing
    images. If any missing image remains unresolved, the next provider retries the
    complete batch so cross-image evidence never silently mixes partial failures.

    Args:
        assets: Ordered, uniquely identified images.
        providers: Ordered primary and fallback visual providers.
        mode: Task-aware main-Agent mode or generic group-context mode.
        task_context: Current user task, used only in task mode.
        quoted_context: Direct quote text, used only in task mode.
        extra_focus: Additional configured focus, used only in task mode.
        native_structured_output: Require provider-native strict JSON Schema output.
        request_max_retries: Transport retry attempts delegated per model request.

    Returns:
        Complete, locally validated visual analysis.

    Raises:
        ValueError: If assets are empty or have invalid identifiers.
        VisionAnalysisError: If every configured provider fails.
    """
    if not assets:
        raise ValueError("visual analysis requires at least one image")
    expected_ids = [asset.image_id for asset in assets]
    if any(not image_id for image_id in expected_ids) or len(set(expected_ids)) != len(
        expected_ids
    ):
        raise ValueError("visual image IDs must be non-empty and unique")

    failures: list[VisionProviderFailure] = []
    for provider in providers:
        provider_config = getattr(provider, "provider_config", {})
        if not isinstance(provider_config, dict):
            provider_config = {}
        provider_id = str(provider_config.get("id", "<unknown>"))
        if (
            native_structured_output
            and not provider.supports_native_structured_output()
        ):
            failures.append(
                VisionProviderFailure(
                    provider_id=provider_id,
                    stage="capability",
                    error_type="NativeStructuredOutputUnsupported",
                )
            )
            logger.warning(
                "Visual analysis provider %s skipped: native structured output is required.",
                provider_id,
            )
            continue
        modalities = provider_config.get("modalities")
        if isinstance(modalities, list) and modalities and "image" not in modalities:
            failures.append(
                VisionProviderFailure(
                    provider_id=provider_id,
                    stage="capability",
                    error_type="ImageInputUnsupported",
                )
            )
            logger.warning(
                "Visual analysis provider %s skipped: image input is not configured.",
                provider_id,
            )
            continue

        started = time.monotonic()
        try:
            correction_used = False
            try:
                batch_result = await _call_and_parse(
                    provider,
                    assets,
                    mode=mode,
                    task_context=task_context if mode == "task" else "",
                    quoted_context=quoted_context if mode == "task" else "",
                    extra_focus=extra_focus if mode == "task" else "",
                    native_structured_output=native_structured_output,
                    request_max_retries=request_max_retries,
                )
            except VisionOutputValidationError as exc:
                correction_used = True
                batch_result = await _call_and_parse(
                    provider,
                    assets,
                    mode=mode,
                    task_context=task_context if mode == "task" else "",
                    quoted_context=quoted_context if mode == "task" else "",
                    extra_focus=extra_focus if mode == "task" else "",
                    native_structured_output=native_structured_output,
                    request_max_retries=request_max_retries,
                    validation_errors=_validation_summary(exc),
                )
            missing, unknown, duplicate = inspect_image_ids(batch_result, expected_ids)

            if unknown or duplicate:
                if correction_used:
                    raise VisionOutputValidationError(
                        f"image_id mismatch after correction; missing={missing}, "
                        f"unknown={unknown}, duplicate={duplicate}"
                    )
                correction_used = True
                correction_error = VisionOutputValidationError(
                    f"image_id mismatch; missing={missing}, unknown={unknown}, "
                    f"duplicate={duplicate}"
                )
                batch_result = await _call_and_parse(
                    provider,
                    assets,
                    mode=mode,
                    task_context=task_context if mode == "task" else "",
                    quoted_context=quoted_context if mode == "task" else "",
                    extra_focus=extra_focus if mode == "task" else "",
                    native_structured_output=native_structured_output,
                    request_max_retries=request_max_retries,
                    validation_errors=_validation_summary(correction_error),
                )
                missing, unknown, duplicate = inspect_image_ids(
                    batch_result, expected_ids
                )
                if unknown or duplicate:
                    raise VisionOutputValidationError(
                        f"image_id mismatch after correction; missing={missing}, "
                        f"unknown={unknown}, duplicate={duplicate}"
                    )

            if not unknown and not duplicate and not missing:
                logger.info(
                    "Visual analysis succeeded provider=%s mode=%s images=%d duration_ms=%d schema=%s",
                    provider_id,
                    mode,
                    len(assets),
                    int((time.monotonic() - started) * 1000),
                    batch_result.schema_version,
                )
                return batch_result

            if missing:
                by_id = {item.image_id: item for item in batch_result.images}
                asset_by_id = {asset.image_id: asset for asset in assets}
                for image_id in missing:
                    try:
                        single_result = await _call_and_parse(
                            provider,
                            [asset_by_id[image_id]],
                            mode=mode,
                            task_context=task_context if mode == "task" else "",
                            quoted_context=quoted_context if mode == "task" else "",
                            extra_focus=extra_focus if mode == "task" else "",
                            native_structured_output=native_structured_output,
                            request_max_retries=request_max_retries,
                        )
                    except VisionOutputValidationError as exc:
                        if correction_used:
                            raise
                        correction_used = True
                        single_result = await _call_and_parse(
                            provider,
                            [asset_by_id[image_id]],
                            mode=mode,
                            task_context=task_context if mode == "task" else "",
                            quoted_context=(quoted_context if mode == "task" else ""),
                            extra_focus=extra_focus if mode == "task" else "",
                            native_structured_output=native_structured_output,
                            request_max_retries=request_max_retries,
                            validation_errors=_validation_summary(exc),
                        )

                    single_missing, single_unknown, single_duplicate = (
                        inspect_image_ids(single_result, [image_id])
                    )
                    if single_missing or single_unknown or single_duplicate:
                        id_error = VisionOutputValidationError(
                            "single-image compensation returned invalid image_id; "
                            f"missing={single_missing}, unknown={single_unknown}, "
                            f"duplicate={single_duplicate}"
                        )
                        if correction_used:
                            raise id_error
                        correction_used = True
                        single_result = await _call_and_parse(
                            provider,
                            [asset_by_id[image_id]],
                            mode=mode,
                            task_context=task_context if mode == "task" else "",
                            quoted_context=(quoted_context if mode == "task" else ""),
                            extra_focus=extra_focus if mode == "task" else "",
                            native_structured_output=native_structured_output,
                            request_max_retries=request_max_retries,
                            validation_errors=_validation_summary(id_error),
                        )
                        single_missing, single_unknown, single_duplicate = (
                            inspect_image_ids(single_result, [image_id])
                        )
                        if single_missing or single_unknown or single_duplicate:
                            raise VisionOutputValidationError(
                                "single-image compensation returned invalid image_id; "
                                f"missing={single_missing}, unknown={single_unknown}, "
                                f"duplicate={single_duplicate}"
                            )
                    by_id[image_id] = single_result.images[0]
                batch_result.images = [by_id[image_id] for image_id in expected_ids]

            final_missing, final_unknown, final_duplicate = inspect_image_ids(
                batch_result, expected_ids
            )
            if final_missing or final_unknown or final_duplicate:
                raise VisionOutputValidationError(
                    f"incomplete merged result; missing={final_missing}, "
                    f"unknown={final_unknown}, duplicate={final_duplicate}"
                )
            logger.info(
                "Visual analysis succeeded provider=%s mode=%s images=%d duration_ms=%d schema=%s",
                provider_id,
                mode,
                len(assets),
                int((time.monotonic() - started) * 1000),
                batch_result.schema_version,
            )
            return batch_result
        except Exception as exc:  # noqa: BLE001
            failures.append(
                VisionProviderFailure(
                    provider_id=provider_id,
                    stage="request_or_validation",
                    error_type=exc.__class__.__name__,
                )
            )
            logger.warning(
                "Visual analysis provider failed provider=%s mode=%s images=%d duration_ms=%d error_type=%s",
                provider_id,
                mode,
                len(assets),
                int((time.monotonic() - started) * 1000),
                exc.__class__.__name__,
            )

    raise VisionAnalysisError(failures, expected_ids)
