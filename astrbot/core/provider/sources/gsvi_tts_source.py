from pathlib import Path

import aiohttp

from astrbot.core.trace.outbound import (
    OutboundCallRecorder,
    OutboundRequestSnapshot,
    record_outbound_result_attributes,
    split_configured_endpoint,
    stable_identifier_hash,
)
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
from astrbot.core.utils.datetime_utils import generate_timestamp_id

from ..entities import ProviderType
from ..provider import TTSProvider
from ..register import register_provider_adapter


@register_provider_adapter(
    "gsvi_tts_api",
    "GSVI TTS API",
    provider_type=ProviderType.TEXT_TO_SPEECH,
)
class ProviderGSVITTS(TTSProvider):
    _astrbot_deep_outbound = True

    def __init__(
        self,
        provider_config: dict,
        provider_settings: dict,
    ) -> None:
        super().__init__(provider_config, provider_settings)
        self.api_key = provider_config.get("api_key", "")
        self.api_base = provider_config.get("api_base", "http://127.0.0.1:8000")
        self.api_base = self.api_base.removesuffix("/")
        self.version = provider_config.get("version", "v4")
        self.character = provider_config.get("character")
        self.prompt_text_lang = provider_config.get("prompt_text_lang", "中文")
        self.emotion = provider_config.get("emotion", "默认")
        self.text_lang = provider_config.get("text_lang", "中文")

    async def get_audio(self, text: str) -> str:
        temp_dir = get_astrbot_temp_path()
        path = Path(temp_dir) / f"gsvi_tts_{generate_timestamp_id()}.wav"
        url = f"{self.api_base}/infer_single"

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        data = {
            "dl_url": self.api_base,
            "version": self.version,
            "model_name": self.character,
            "prompt_text_lang": self.prompt_text_lang,
            "emotion": self.emotion,
            "text": text,
            "text_lang": self.text_lang,
        }
        synth_recorder = OutboundCallRecorder(
            OutboundRequestSnapshot(
                api_family="gsv.infer_single",
                sdk_operation="aiohttp.ClientSession.post",
                base_url=self.api_base,
                resource_path="/infer_single",
                route_resolution="constructed",
                parameters={**data, "model": self.character},
            )
        )
        synth_attempt = synth_recorder.record_attempt()

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=data, headers=headers) as response:
                if response.status == 200:
                    resp_json = await response.json()
                    msg = resp_json.get("msg")
                    audio_url = resp_json.get("audio_url")
                    if not msg or msg != "合成成功":
                        error = Exception(f"GSVI TTS API 合成失败: {msg}")
                        synth_recorder.record_failed(
                            error,
                            attempt_number=synth_attempt,
                            status_code=response.status,
                        )
                        raise error
                    synth_recorder.record_completed(
                        response,
                        attempt_number=synth_attempt,
                    )
                    download_base, download_path = split_configured_endpoint(
                        audio_url,
                        dynamic_path_template="/{generated_audio_path}",
                    )
                    download_recorder = OutboundCallRecorder(
                        OutboundRequestSnapshot(
                            api_family="gsv.audio.download",
                            sdk_operation="aiohttp.ClientSession.get",
                            http_method="GET",
                            base_url=download_base,
                            resource_path=download_path,
                            route_resolution="constructed",
                            input_summary={
                                "remote_resource_id_hash": stable_identifier_hash(
                                    audio_url
                                )
                            },
                        )
                    )
                    download_attempt = download_recorder.record_attempt()
                    async with session.get(audio_url) as audio_response:
                        if audio_response.status == 200:
                            audio_bytes = await audio_response.read()
                            with open(path, "wb") as f:
                                f.write(audio_bytes)
                            download_recorder.record_completed(
                                audio_response,
                                attempt_number=download_attempt,
                            )
                            record_outbound_result_attributes(
                                audio_bytes=len(audio_bytes)
                            )
                        else:
                            error_text = await audio_response.text()
                            error = Exception(
                                f"GSVI TTS API 下载音频失败，状态码: {audio_response.status}，错误: {error_text}",
                            )
                            download_recorder.record_failed(
                                error,
                                attempt_number=download_attempt,
                                status_code=audio_response.status,
                            )
                            raise error
                else:
                    error_text = await response.text()
                    error = Exception(
                        f"GSVI TTS API 请求失败，状态码: {response.status}，错误: {error_text}",
                    )
                    synth_recorder.record_failed(
                        error,
                        attempt_number=synth_attempt,
                        status_code=response.status,
                    )
                    raise error

        return str(path)
