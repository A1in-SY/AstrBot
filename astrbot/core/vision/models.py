from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

VISION_SCHEMA_VERSION = "astrbot.vision_analysis.v1"


class _VisionModel(BaseModel):
    """Base model that rejects fields outside the evidence contract."""

    model_config = ConfigDict(extra="forbid")


class VisionEvidence(_VisionModel):
    """A claim together with the visible support for that claim."""

    kind: Literal["text", "visual", "relationship", "inference"]
    observation: str
    support: str


class VisionOCRLine(_VisionModel):
    """A visible OCR line preserved without normalization."""

    text: str
    language: str


class VisionOCR(_VisionModel):
    """OCR evidence extracted from one image."""

    full_text: str
    lines: list[VisionOCRLine]


class VisionLayoutRegion(_VisionModel):
    """A logical image region in reading order."""

    region_type: Literal[
        "title",
        "subtitle",
        "paragraph",
        "list",
        "table",
        "chart",
        "form",
        "code",
        "image",
        "icon",
        "other",
    ]
    reading_order: int
    text: str


class VisionLayout(_VisionModel):
    """Logical layout evidence without unstable pixel coordinates."""

    regions: list[VisionLayoutRegion]


class VisionEntity(_VisionModel):
    """An entity visibly supported by the image."""

    name: str
    entity_type: str
    evidence: str


class VisionRelation(_VisionModel):
    """A relationship visibly supported by the image."""

    subject: str
    predicate: str
    object: str
    evidence: str


class VisionSemantics(_VisionModel):
    """Scene-level meaning and entity relationships."""

    scene: str
    intent: str
    entities: list[VisionEntity]
    relations: list[VisionRelation]


class VisionVisualDetails(_VisionModel):
    """Visual appearance that may matter to downstream reasoning."""

    style: str
    dominant_colors: list[str]
    notes: list[str]


class VisionImageResult(_VisionModel):
    """Structured evidence for exactly one input image."""

    image_id: str
    summary: str
    task_relevant_evidence: list[VisionEvidence]
    ocr: VisionOCR
    layout: VisionLayout
    semantics: VisionSemantics
    visual: VisionVisualDetails
    uncertainty: list[str]
    embedded_instructions: list[str]


class VisionCrossImageFinding(_VisionModel):
    """A finding that depends on two or more input images."""

    image_ids: list[str]
    finding: str
    evidence: str


class VisionAnalysisResult(_VisionModel):
    """Versioned visual evidence envelope consumed by text-only models."""

    schema_version: Literal["astrbot.vision_analysis.v1"]
    images: list[VisionImageResult]
    cross_image_findings: list[VisionCrossImageFinding]

    def compact_json(self) -> str:
        """Serialize the complete validated result without application truncation.

        Returns:
            Compact UTF-8 JSON suitable for conversation persistence.
        """
        return self.model_dump_json(exclude_none=False)


@dataclass(frozen=True, slots=True)
class VisionImageAsset:
    """One image supplied to the visual analysis core.

    Args:
        image_id: Stable request-local identifier exposed to models.
        image_url: Provider-resolvable URL, data URL, or local path.
        source: Non-sensitive origin such as ``current_message`` or
            ``quoted_message``.
    """

    image_id: str
    image_url: str
    source: str = "request"


@dataclass(frozen=True, slots=True)
class VisionProviderFailure:
    """Sanitized diagnostic for one provider attempt."""

    provider_id: str
    stage: str
    error_type: str


class VisionAnalysisError(RuntimeError):
    """Raised after every configured visual provider fails safely."""

    def __init__(
        self,
        failures: list[VisionProviderFailure],
        unresolved_image_ids: list[str] | None = None,
    ) -> None:
        self.failures = tuple(failures)
        self.unresolved_image_ids = tuple(unresolved_image_ids or ())
        providers = ", ".join(failure.provider_id for failure in failures) or "none"
        super().__init__(f"All visual analysis providers failed: {providers}")


class VisionOutputValidationError(ValueError):
    """Raised when a provider responds but its output violates the contract."""


def parse_vision_result(raw_output: str) -> VisionAnalysisResult:
    """Parse one provider response using deliberately narrow JSON tolerance.

    Args:
        raw_output: Complete provider output.

    Returns:
        A locally schema-validated result.

    Raises:
        ValueError: If the response is empty, contains surrounding prose, or
            violates the Pydantic schema.
    """
    try:
        candidate = (raw_output or "").strip()
        if candidate.startswith("```json") and candidate.endswith("```"):
            candidate = candidate[7:-3].strip()
        elif candidate.startswith("```") and candidate.endswith("```"):
            candidate = candidate[3:-3].strip()
        if not candidate:
            raise ValueError("empty output")
        parsed = json.loads(candidate)
        return VisionAnalysisResult.model_validate(parsed)
    except (ValueError, ValidationError) as exc:
        raise VisionOutputValidationError(str(exc)) from exc


def inspect_image_ids(
    result: VisionAnalysisResult,
    expected_ids: list[str],
) -> tuple[list[str], list[str], list[str]]:
    """Compare result image identifiers with the request manifest.

    Args:
        result: Locally schema-validated provider output.
        expected_ids: Ordered identifiers supplied with the images.

    Returns:
        A tuple containing missing, unknown, and duplicate identifiers.
    """
    actual = [item.image_id for item in result.images]
    actual_set = set(actual)
    expected_set = set(expected_ids)
    missing = [image_id for image_id in expected_ids if image_id not in actual_set]
    unknown = [image_id for image_id in actual if image_id not in expected_set]
    duplicate = sorted({image_id for image_id in actual if actual.count(image_id) > 1})
    return missing, unknown, duplicate
