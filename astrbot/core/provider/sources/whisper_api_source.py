from pathlib import Path

from openai import NOT_GIVEN, AsyncOpenAI

from astrbot.core.trace.outbound import (
    OutboundCallRecorder,
    OutboundRequestSnapshot,
    record_outbound_result_attributes,
)
from astrbot.core.utils.media_utils import MediaResolver

from ..entities import ProviderType
from ..provider import STTProvider
from ..register import register_provider_adapter


@register_provider_adapter(
    "openai_whisper_api",
    "OpenAI Whisper API",
    provider_type=ProviderType.SPEECH_TO_TEXT,
)
class ProviderOpenAIWhisperAPI(STTProvider):
    _astrbot_deep_outbound = True

    def __init__(
        self,
        provider_config: dict,
        provider_settings: dict,
    ) -> None:
        super().__init__(provider_config, provider_settings)
        self.chosen_api_key = provider_config.get("api_key", "")

        self.client = AsyncOpenAI(
            api_key=self.chosen_api_key,
            base_url=provider_config.get("api_base"),
            timeout=provider_config.get("timeout", NOT_GIVEN),
        )

        self.set_model(provider_config["model"])

    async def get_text(self, audio_url: str) -> str:
        """Only supports mp3, mp4, mpeg, m4a, wav, webm"""
        async with MediaResolver(
            audio_url,
            media_type="audio",
            default_suffix=".wav",
        ).as_path(target_format="wav") as audio:
            with audio.open("rb") as audio_file:
                try:
                    audio_bytes = Path(audio_file.name).stat().st_size
                except (OSError, TypeError, ValueError):
                    audio_bytes = None
                recorder = OutboundCallRecorder(
                    OutboundRequestSnapshot(
                        api_family="openai.audio.transcriptions",
                        sdk_operation="client.audio.transcriptions.create",
                        base_url=(
                            str(base_url)
                            if (base_url := getattr(self.client, "base_url", None))
                            is not None
                            else self.provider_config.get("api_base")
                        ),
                        resource_path="/audio/transcriptions",
                        route_resolution="sdk_declared",
                        timeout_seconds=self.provider_config.get("timeout"),
                        parameters={
                            "model": self.model_name,
                            "audio": audio_file,
                        },
                        input_summary={
                            "audio_source_type": "resolved_media",
                            "audio_bytes": audio_bytes,
                        },
                    )
                )
                attempt_number = recorder.record_attempt()
                try:
                    result = await self.client.audio.transcriptions.create(
                        model=self.model_name,
                        file=("audio.wav", audio_file),
                    )
                except BaseException as exc:
                    recorder.record_failed(exc, attempt_number=attempt_number)
                    raise
                recorder.record_completed(result, attempt_number=attempt_number)
                record_outbound_result_attributes(
                    recognized_language=getattr(result, "language", None),
                    server_duration_seconds=getattr(result, "duration", None),
                )
        return result.text

    async def terminate(self):
        if self.client:
            await self.client.close()
