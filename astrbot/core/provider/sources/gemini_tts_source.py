import os
import wave

from google import genai
from google.genai import types

from astrbot import logger
from astrbot.core.trace.outbound import (
    OutboundCallRecorder,
    OutboundRequestSnapshot,
    record_outbound_result_attributes,
)
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
from astrbot.core.utils.datetime_utils import generate_timestamp_id

from ..entities import ProviderType
from ..provider import TTSProvider
from ..register import register_provider_adapter


@register_provider_adapter(
    "gemini_tts",
    "Gemini TTS API",
    provider_type=ProviderType.TEXT_TO_SPEECH,
)
class ProviderGeminiTTSAPI(TTSProvider):
    _astrbot_deep_outbound = True

    def __init__(
        self,
        provider_config: dict,
        provider_settings: dict,
    ) -> None:
        super().__init__(provider_config, provider_settings)
        api_key: str = provider_config.get("gemini_tts_api_key", "")
        api_base: str | None = provider_config.get("gemini_tts_api_base")
        timeout: int = int(provider_config.get("gemini_tts_timeout", 20))
        http_options = types.HttpOptions(timeout=timeout * 1000)

        if api_base:
            api_base = api_base.removesuffix("/")
            http_options.base_url = api_base
        proxy = provider_config.get("proxy", "")
        if proxy:
            http_options.async_client_args = {"proxy": proxy}
            logger.info(f"[Gemini TTS] 使用代理: {proxy}")

        self.client = genai.Client(api_key=api_key, http_options=http_options).aio
        self.model: str = provider_config.get(
            "gemini_tts_model",
            "gemini-2.5-flash-preview-tts",
        )
        self.prefix: str | None = provider_config.get(
            "gemini_tts_prefix",
        )
        self.voice_name: str = provider_config.get("gemini_tts_voice_name", "Leda")

    async def get_audio(self, text: str) -> str:
        temp_dir = get_astrbot_temp_path()
        path = os.path.join(temp_dir, f"gemini_tts_{generate_timestamp_id()}.wav")
        prompt = f"{self.prefix}: {text}" if self.prefix else text
        config = types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=self.voice_name,
                    ),
                ),
            ),
        )
        recorder = OutboundCallRecorder(
            OutboundRequestSnapshot(
                api_family="gemini.models.generate_content",
                sdk_operation="client.models.generate_content",
                base_url=self.provider_config.get("gemini_tts_api_base"),
                resource_path="/models/{model}:generateContent",
                route_resolution="sdk_declared",
                timeout_seconds=self.provider_config.get("gemini_tts_timeout", 20),
                proxy_configured=bool(self.provider_config.get("proxy")),
                parameters={
                    "model": self.model,
                    "contents": prompt,
                    "response_modalities": ["AUDIO"],
                    "voice": self.voice_name,
                    "sample_rate": 24000,
                },
            )
        )
        attempt_number = recorder.record_attempt()
        try:
            response = await self.client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=config,
            )
        except BaseException as exc:
            recorder.record_failed(exc, attempt_number=attempt_number)
            raise
        recorder.record_completed(response, attempt_number=attempt_number)

        # 不想看类型检查报错
        if (
            not response.candidates
            or not response.candidates[0].content
            or not response.candidates[0].content.parts
            or not response.candidates[0].content.parts[0].inline_data
            or not response.candidates[0].content.parts[0].inline_data.data
        ):
            raise Exception("No audio content returned from Gemini TTS API.")

        audio_data = response.candidates[0].content.parts[0].inline_data.data
        with wave.open(path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(24000)
            wf.writeframes(audio_data)

        record_outbound_result_attributes(audio_bytes=len(audio_data))

        return path

    async def terminate(self):
        if self.client:
            await self.client.aclose()
