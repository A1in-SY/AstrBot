import asyncio
import base64
import json
import os
import traceback
import uuid

import aiohttp

from astrbot import logger
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
    "volcengine_tts",
    "火山引擎 TTS",
    provider_type=ProviderType.TEXT_TO_SPEECH,
)
class ProviderVolcengineTTS(TTSProvider):
    _astrbot_deep_outbound = True

    def __init__(self, provider_config: dict, provider_settings: dict) -> None:
        super().__init__(provider_config, provider_settings)
        self.api_key = provider_config.get("api_key", "")
        self.appid = provider_config.get("appid", "")
        self.cluster = provider_config.get("volcengine_cluster", "")
        self.voice_type = provider_config.get("volcengine_voice_type", "")
        self.speed_ratio = provider_config.get("volcengine_speed_ratio", 1.0)
        self.api_base = provider_config.get(
            "api_base",
            "https://openspeech.bytedance.com/api/v1/tts",
        )
        self.timeout = provider_config.get("timeout", 20)

    def _build_request_payload(self, text: str) -> dict:
        return {
            "app": {
                "appid": self.appid,
                "token": self.api_key,
                "cluster": self.cluster,
            },
            "user": {"uid": str(uuid.uuid4())},
            "audio": {
                "voice_type": self.voice_type,
                "encoding": "mp3",
                "speed_ratio": self.speed_ratio,
                "volume_ratio": 1.0,
                "pitch_ratio": 1.0,
            },
            "request": {
                "reqid": str(uuid.uuid4()),
                "text": text,
                "text_type": "plain",
                "operation": "query",
                "with_frontend": 1,
                "frontend_type": "unitTson",
            },
        }

    async def get_audio(self, text: str) -> str:
        """异步方法获取语音文件路径"""
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer; {self.api_key}",
        }

        payload = self._build_request_payload(text)
        route_base, route_path = split_configured_endpoint(
            self.api_base,
            dynamic_path_template="/api/v1/tts",
            static_paths=("/api/v1/tts",),
        )
        recorder = OutboundCallRecorder(
            OutboundRequestSnapshot(
                api_family="volcengine.tts",
                sdk_operation="aiohttp.ClientSession.post",
                base_url=route_base,
                resource_path=route_path,
                route_resolution="constructed",
                timeout_seconds=self.timeout,
                parameters={
                    "model": self.cluster,
                    "text": text,
                    "voice": self.voice_type,
                    "speed": self.speed_ratio,
                    "response_format": "mp3",
                },
            )
        )
        attempt_number = recorder.record_attempt()

        logger.debug(f"请求头: {headers}")
        logger.debug(f"请求 URL: {self.api_base}")
        logger.debug(f"请求体: {json.dumps(payload, ensure_ascii=False)[:100]}...")

        try:
            async with (
                aiohttp.ClientSession() as session,
                session.post(
                    self.api_base,
                    data=json.dumps(payload),
                    headers=headers,
                    timeout=self.timeout,
                ) as response,
            ):
                logger.debug(f"响应状态码: {response.status}")

                response_text = await response.text()
                logger.debug(f"响应内容: {response_text[:200]}...")

                if response.status == 200:
                    resp_data = json.loads(response_text)

                    if "data" in resp_data:
                        audio_data = base64.b64decode(resp_data["data"])

                        temp_dir = get_astrbot_temp_path()
                        os.makedirs(temp_dir, exist_ok=True)
                        file_path = os.path.join(
                            temp_dir,
                            f"volcengine_tts_{generate_timestamp_id()}.mp3",
                        )

                        loop = asyncio.get_running_loop()
                        await loop.run_in_executor(
                            None,
                            lambda: open(file_path, "wb").write(audio_data),
                        )

                        recorder.record_completed(
                            response, attempt_number=attempt_number
                        )
                        record_outbound_result_attributes(audio_bytes=len(audio_data))
                        return file_path
                    error_msg = resp_data.get("message", "未知错误")
                    error = Exception(f"火山引擎 TTS API 返回错误: {error_msg}")
                    recorder.record_failed(
                        error,
                        attempt_number=attempt_number,
                        status_code=response.status,
                    )
                    raise error
                error = Exception(
                    f"火山引擎 TTS API 请求失败: {response.status}, {response_text}",
                )
                recorder.record_failed(
                    error,
                    attempt_number=attempt_number,
                    status_code=response.status,
                )
                raise error

        except Exception as e:
            recorder.record_failed(e, attempt_number=attempt_number)
            error_details = traceback.format_exc()
            logger.debug(f"火山引擎 TTS 异常详情: {error_details}")
            raise Exception(f"火山引擎 TTS 异常: {e!s}")
