from astrbot.core.trace.outbound import (
    OutboundCallRecorder,
    OutboundRequestSnapshot,
    record_outbound_result_attributes,
    split_configured_endpoint,
)

from ..entities import ProviderType
from ..provider import STTProvider
from ..register import register_provider_adapter
from .mimo_api_common import (
    DEFAULT_MIMO_API_BASE,
    DEFAULT_MIMO_STT_MODEL,
    DEFAULT_MIMO_STT_SYSTEM_PROMPT,
    DEFAULT_MIMO_STT_USER_PROMPT,
    MiMoAPIError,
    build_api_url,
    build_headers,
    cleanup_files,
    create_http_client,
    normalize_timeout,
    prepare_audio_input,
)


@register_provider_adapter(
    "mimo_stt_api",
    "MiMo STT API",
    provider_type=ProviderType.SPEECH_TO_TEXT,
)
class ProviderMiMoSTTAPI(STTProvider):
    _astrbot_deep_outbound = True

    def __init__(
        self,
        provider_config: dict,
        provider_settings: dict,
    ) -> None:
        super().__init__(provider_config, provider_settings)
        self.chosen_api_key = provider_config.get("api_key", "")
        self.api_base = provider_config.get("api_base", DEFAULT_MIMO_API_BASE)
        self.proxy = provider_config.get("proxy", "")
        self.timeout = normalize_timeout(provider_config.get("timeout", 20))
        self.set_model(provider_config.get("model", DEFAULT_MIMO_STT_MODEL))
        self.client = create_http_client(self.timeout, self.proxy)

    def _is_asr_model(self) -> bool:
        return "asr" in (self.model_name or "").lower()

    def _build_messages(self, audio_data_url: str) -> list[dict]:
        audio_content = {
            "type": "input_audio",
            "input_audio": {
                "data": audio_data_url,
            },
        }
        if self._is_asr_model():
            # Dedicated ASR models (speech-recognition docs) take bare audio.
            return [
                {
                    "role": "user",
                    "content": [audio_content],
                },
            ]
        # Multimodal models such as mimo-v2.5 (audio-understanding docs)
        # require a text instruction alongside the audio, otherwise the API
        # rejects the request.
        return [
            {
                "role": "system",
                "content": DEFAULT_MIMO_STT_SYSTEM_PROMPT,
            },
            {
                "role": "user",
                "content": [
                    audio_content,
                    {
                        "type": "text",
                        "text": DEFAULT_MIMO_STT_USER_PROMPT,
                    },
                ],
            },
        ]

    async def get_text(self, audio_url: str) -> str:
        audio_data_url, cleanup_paths = await prepare_audio_input(audio_url)
        payload = {
            "model": self.model_name,
            "messages": self._build_messages(audio_data_url),
            "max_completion_tokens": 1024,
        }
        request_url = build_api_url(self.api_base)
        route_base, route_path = split_configured_endpoint(
            request_url,
            dynamic_path_template="/chat/completions",
            static_paths=("/chat/completions", "/v1/chat/completions"),
        )
        recorder = OutboundCallRecorder(
            OutboundRequestSnapshot(
                api_family="mimo.chat.completions",
                sdk_operation="httpx.AsyncClient.post",
                base_url=route_base,
                resource_path=route_path,
                route_resolution="constructed",
                timeout_seconds=self.timeout,
                proxy_configured=bool(self.proxy),
                parameters=payload,
            )
        )
        attempt_number = recorder.record_attempt()

        try:
            response = await self.client.post(
                request_url,
                headers=build_headers(self.chosen_api_key),
                json=payload,
            )
            try:
                response.raise_for_status()
            except Exception as exc:
                error_text = response.text[:1024]
                raise MiMoAPIError(
                    f"MiMo STT API request failed: HTTP {response.status_code}, response: {error_text}"
                ) from exc
            recorder.record_completed(response, attempt_number=attempt_number)

            data = response.json()
            record_outbound_result_attributes(
                recognized_language=data.get("language"),
                server_duration_seconds=data.get("duration"),
            )
            choices = data.get("choices") or []
            first_choice = choices[0] if choices else {}
            message = (first_choice or {}).get("message") or {}
            content = message.get("content") or message.get("reasoning_content") or ""
            if not isinstance(content, str) or not content.strip():
                raise MiMoAPIError("MiMo STT API returned empty transcription")
            return content.strip()
        except BaseException as exc:
            recorder.record_failed(exc, attempt_number=attempt_number)
            raise
        finally:
            cleanup_files(cleanup_paths)

    async def terminate(self):
        if self.client:
            await self.client.aclose()
