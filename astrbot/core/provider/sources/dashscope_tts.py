import asyncio
import base64
import logging
import os

import aiohttp
import dashscope
from dashscope.audio.tts_v2 import AudioFormat, SpeechSynthesizer

try:
    from dashscope.aigc.multimodal_conversation import MultiModalConversation
except (
    ImportError
):  # pragma: no cover - older dashscope versions without Qwen TTS support
    MultiModalConversation = None

from astrbot.core.trace.outbound import (
    OutboundCallRecorder,
    OutboundRequestSnapshot,
    record_outbound_result_attributes,
    split_configured_endpoint,
)
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
from astrbot.core.utils.datetime_utils import generate_timestamp_id

from ..entities import ProviderType
from ..provider import TTSProvider
from ..register import register_provider_adapter


@register_provider_adapter(
    "dashscope_tts",
    "Dashscope TTS API",
    provider_type=ProviderType.TEXT_TO_SPEECH,
)
class ProviderDashscopeTTSAPI(TTSProvider):
    _astrbot_deep_outbound = True

    def __init__(
        self,
        provider_config: dict,
        provider_settings: dict,
    ) -> None:
        super().__init__(provider_config, provider_settings)
        self.chosen_api_key: str = provider_config.get("api_key", "")
        self.voice: str = provider_config.get("dashscope_tts_voice", "loongstella")
        self.set_model(provider_config["model"])
        self.timeout_ms = float(provider_config.get("timeout", 20)) * 1000
        dashscope.api_key = self.chosen_api_key

    async def get_audio(self, text: str) -> str:
        model = self.get_model()
        if not model:
            raise RuntimeError("Dashscope TTS model is not configured.")

        temp_dir = get_astrbot_temp_path()
        os.makedirs(temp_dir, exist_ok=True)

        if self._is_qwen_tts_model(model):
            recorder = OutboundCallRecorder(
                OutboundRequestSnapshot(
                    api_family="dashscope.multimodal_generation",
                    sdk_operation="MultiModalConversation.call",
                    base_url="https://dashscope.aliyuncs.com/api/v1",
                    resource_path="/services/aigc/multimodal-generation/generation",
                    route_resolution="sdk_declared",
                    timeout_seconds=self.timeout_ms / 1000,
                    parameters={
                        "model": model,
                        "text": text,
                        "voice": self.voice or "Cherry",
                    },
                )
            )
            attempt_number = recorder.record_attempt()
            try:
                audio_bytes, ext, response = await self._synthesize_with_qwen_tts(
                    model,
                    text,
                    recorder,
                    attempt_number,
                )
            except BaseException as exc:
                recorder.record_failed(exc, attempt_number=attempt_number)
                raise
        else:
            recorder = OutboundCallRecorder(
                OutboundRequestSnapshot(
                    api_family="dashscope.speech_synthesis",
                    sdk_operation="SpeechSynthesizer.call",
                    base_url="https://dashscope.aliyuncs.com",
                    resource_path="/{sdk_speech_synthesis_path}",
                    route_resolution="sdk_declared",
                    timeout_seconds=self.timeout_ms / 1000,
                    parameters={
                        "model": model,
                        "text": text,
                        "voice": self.voice,
                        "response_format": "wav",
                        "sample_rate": 24000,
                    },
                )
            )
            attempt_number = recorder.record_attempt()
            try:
                audio_bytes, ext, response = await self._synthesize_with_cosyvoice(
                    model, text
                )
            except BaseException as exc:
                recorder.record_failed(exc, attempt_number=attempt_number)
                raise
            recorder.record_completed(response, attempt_number=attempt_number)

        if not audio_bytes:
            raise RuntimeError(
                "Audio synthesis failed, returned empty content. The model may not be supported or the service is unavailable.",
            )

        path = os.path.join(temp_dir, f"dashscope_tts_{generate_timestamp_id()}{ext}")
        with open(path, "wb") as f:
            f.write(audio_bytes)
        record_outbound_result_attributes(audio_bytes=len(audio_bytes))
        return path

    def _call_qwen_tts(self, model: str, text: str):
        if MultiModalConversation is None:
            raise RuntimeError(
                "dashscope SDK missing MultiModalConversation. Please upgrade the dashscope package to use Qwen TTS models.",
            )

        kwargs = {
            "model": model,
            "messages": None,
            "api_key": self.chosen_api_key,
            "voice": self.voice or "Cherry",
            "text": text,
        }
        if not self.voice:
            logging.warning(
                "No voice specified for Qwen TTS model, using default 'Cherry'.",
            )
        return MultiModalConversation.call(**kwargs)

    async def _synthesize_with_qwen_tts(
        self,
        model: str,
        text: str,
        recorder: OutboundCallRecorder,
        attempt_number: int,
    ) -> tuple[bytes | None, str, object]:
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(None, self._call_qwen_tts, model, text)
        recorder.record_completed(response, attempt_number=attempt_number)
        audio_bytes = await self._extract_audio_from_response(response)
        if not audio_bytes:
            raise RuntimeError(
                f"Audio synthesis failed for model '{model}'. {response}",
            )
        ext = ".wav"
        return audio_bytes, ext, response

    async def _extract_audio_from_response(self, response) -> bytes | None:
        output = getattr(response, "output", None)
        audio_obj = getattr(output, "audio", None) if output is not None else None
        if not audio_obj:
            return None

        data_b64 = getattr(audio_obj, "data", None)
        if data_b64:
            try:
                return base64.b64decode(data_b64)
            except (ValueError, TypeError):
                logging.exception("Failed to decode base64 audio data.")
                return None

        url = getattr(audio_obj, "url", None)
        if url:
            return await self._download_audio_from_url(url)
        return None

    async def _download_audio_from_url(self, url: str) -> bytes | None:
        if not url:
            return None
        timeout = max(self.timeout_ms / 1000, 1) if self.timeout_ms else 20
        route_base, route_path = split_configured_endpoint(
            url,
            dynamic_path_template="/{generated_audio_path}",
        )
        recorder = OutboundCallRecorder(
            OutboundRequestSnapshot(
                api_family="dashscope.audio.download",
                sdk_operation="aiohttp.ClientSession.get",
                http_method="GET",
                base_url=route_base,
                resource_path=route_path,
                route_resolution="constructed",
                timeout_seconds=timeout,
            )
        )
        attempt_number = recorder.record_attempt()
        try:
            async with (
                aiohttp.ClientSession() as session,
                session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as response,
            ):
                audio_bytes = await response.read()
                recorder.record_completed(response, attempt_number=attempt_number)
                return audio_bytes
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            recorder.record_failed(e, attempt_number=attempt_number)
            logging.exception(f"Failed to download audio from URL {url}: {e}")
            return None

    async def _synthesize_with_cosyvoice(
        self,
        model: str,
        text: str,
    ) -> tuple[bytes | None, str, object]:
        synthesizer = SpeechSynthesizer(
            model=model,
            voice=self.voice,
            format=AudioFormat.WAV_24000HZ_MONO_16BIT,
        )
        loop = asyncio.get_running_loop()
        audio_bytes = await loop.run_in_executor(
            None,
            synthesizer.call,
            text,
            self.timeout_ms,
        )
        if not audio_bytes:
            resp = synthesizer.get_response()
            if resp and isinstance(resp, dict):
                raise RuntimeError(
                    f"Audio synthesis failed for model '{model}'. {resp}".strip(),
                )
        return audio_bytes, ".wav", synthesizer.get_response()

    def _is_qwen_tts_model(self, model: str) -> bool:
        model_lower = model.lower()
        return "tts" in model_lower and model_lower.startswith("qwen")
