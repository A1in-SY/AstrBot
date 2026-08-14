import os

import httpx
from openai import NOT_GIVEN, AsyncOpenAI

from astrbot import logger
from astrbot.core.trace.outbound import (
    OutboundCallRecorder,
    OutboundRequestSnapshot,
    record_outbound_first_chunk,
    record_outbound_result_attributes,
)
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
from astrbot.core.utils.datetime_utils import generate_timestamp_id

from ..entities import ProviderType
from ..provider import TTSProvider
from ..register import register_provider_adapter


@register_provider_adapter(
    "openai_tts_api",
    "OpenAI TTS API",
    provider_type=ProviderType.TEXT_TO_SPEECH,
)
class ProviderOpenAITTSAPI(TTSProvider):
    _astrbot_deep_outbound = True

    def __init__(
        self,
        provider_config: dict,
        provider_settings: dict,
    ) -> None:
        super().__init__(provider_config, provider_settings)
        self.chosen_api_key = provider_config.get("api_key", "")
        self.voice = provider_config.get("openai-tts-voice", "alloy")

        timeout = provider_config.get("timeout", NOT_GIVEN)
        if isinstance(timeout, str):
            timeout = int(timeout)

        proxy = provider_config.get("proxy", "")
        http_client = None
        if proxy:
            logger.info(f"[OpenAI TTS] 使用代理: {proxy}")
            http_client = httpx.AsyncClient(proxy=proxy)
        self.client = AsyncOpenAI(
            api_key=self.chosen_api_key,
            base_url=provider_config.get("api_base"),
            timeout=timeout,
            http_client=http_client,
        )

        self.set_model(provider_config.get("model", ""))

    async def get_audio(self, text: str) -> str:
        temp_dir = get_astrbot_temp_path()
        path = os.path.join(temp_dir, f"openai_tts_api_{generate_timestamp_id()}.wav")
        parameters = {
            "model": self.model_name,
            "voice": self.voice,
            "response_format": "wav",
            "input": text,
        }
        recorder = OutboundCallRecorder(
            OutboundRequestSnapshot(
                api_family="openai.audio.speech",
                sdk_operation="client.audio.speech.with_streaming_response.create",
                base_url=str(self.client.base_url),
                resource_path="/audio/speech",
                route_resolution="sdk_declared",
                streaming=True,
                timeout_seconds=self.provider_config.get("timeout"),
                proxy_configured=bool(self.provider_config.get("proxy")),
                parameters=parameters,
            )
        )
        attempt_number = recorder.record_attempt()
        audio_bytes = 0
        audio_chunk_count = 0
        try:
            async with self.client.audio.speech.with_streaming_response.create(
                **parameters
            ) as response:
                with open(path, "wb") as f:
                    async for chunk in response.iter_bytes(chunk_size=1024):
                        if chunk:
                            record_outbound_first_chunk(response)
                            audio_chunk_count += 1
                            audio_bytes += len(chunk)
                        f.write(chunk)
                recorder.record_completed(response, attempt_number=attempt_number)
        except BaseException as exc:
            recorder.record_failed(exc, attempt_number=attempt_number)
            raise
        record_outbound_result_attributes(
            audio_bytes=audio_bytes,
            audio_chunk_count=audio_chunk_count,
        )
        return path

    async def terminate(self):
        if self.client:
            await self.client.close()
