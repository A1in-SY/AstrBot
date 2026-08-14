from google import genai
from google.genai import types
from google.genai.errors import APIError

from astrbot import logger
from astrbot.core.trace.outbound import (
    OutboundCallRecorder,
    OutboundRequestSnapshot,
)

from ..entities import ProviderType
from ..provider import EmbeddingProvider
from ..register import register_provider_adapter


@register_provider_adapter(
    "gemini_embedding",
    "Google Gemini Embedding 提供商适配器",
    provider_type=ProviderType.EMBEDDING,
)
class GeminiEmbeddingProvider(EmbeddingProvider):
    _astrbot_deep_outbound = True

    def __init__(self, provider_config: dict, provider_settings: dict) -> None:
        super().__init__(provider_config, provider_settings)
        self.provider_config = provider_config
        self.provider_settings = provider_settings

        api_key: str = provider_config["embedding_api_key"]
        api_base: str = provider_config["embedding_api_base"]
        timeout: int = int(provider_config.get("timeout", 20))

        http_options = types.HttpOptions(timeout=timeout * 1000)
        if api_base:
            api_base = api_base.removesuffix("/")
            http_options.base_url = api_base
        proxy = provider_config.get("proxy", "")
        if proxy:
            http_options.async_client_args = {"proxy": proxy}
            logger.info(f"[Gemini Embedding] 使用代理: {proxy}")

        self.client = genai.Client(api_key=api_key, http_options=http_options).aio

        self.model = provider_config.get(
            "embedding_model",
            "gemini-embedding-exp-03-07",
        )

    async def get_embedding(self, text: str) -> list[float]:
        """获取文本的嵌入"""
        config = types.EmbedContentConfig(output_dimensionality=self.get_dim())
        recorder = OutboundCallRecorder(
            OutboundRequestSnapshot(
                api_family="gemini.models.embed_content",
                sdk_operation="client.models.embed_content",
                base_url=self.provider_config.get("embedding_api_base"),
                resource_path="/models/{model}:embedContent",
                route_resolution="sdk_declared",
                timeout_seconds=self.provider_config.get("timeout", 20),
                proxy_configured=bool(self.provider_config.get("proxy")),
                parameters={
                    "model": self.model,
                    "contents": text,
                    "dimensions": self.get_dim(),
                },
            )
        )
        attempt_number = recorder.record_attempt()
        try:
            result = await self.client.models.embed_content(
                model=self.model,
                contents=text,
                config=config,
            )
            assert result.embeddings is not None
            assert result.embeddings[0].values is not None
            recorder.record_completed(result, attempt_number=attempt_number)
            return result.embeddings[0].values
        except APIError as e:
            recorder.record_failed(e, attempt_number=attempt_number)
            raise Exception(f"Gemini Embedding API请求失败: {e.message}")
        except BaseException as exc:
            recorder.record_failed(exc, attempt_number=attempt_number)
            raise

    async def get_embeddings(self, text: list[str]) -> list[list[float]]:
        """批量获取文本的嵌入"""
        contents = [types.Content(parts=[types.Part.from_text(text=s)]) for s in text]
        config = types.EmbedContentConfig(output_dimensionality=self.get_dim())
        recorder = OutboundCallRecorder(
            OutboundRequestSnapshot(
                api_family="gemini.models.embed_content",
                sdk_operation="client.models.embed_content",
                base_url=self.provider_config.get("embedding_api_base"),
                resource_path="/models/{model}:embedContent",
                route_resolution="sdk_declared",
                timeout_seconds=self.provider_config.get("timeout", 20),
                proxy_configured=bool(self.provider_config.get("proxy")),
                parameters={
                    "model": self.model,
                    "contents": contents,
                    "dimensions": self.get_dim(),
                },
            )
        )
        attempt_number = recorder.record_attempt()
        try:
            result = await self.client.models.embed_content(
                model=self.model,
                contents=contents,
                config=config,
            )
            assert result.embeddings is not None

            embeddings: list[list[float]] = []
            for embedding in result.embeddings:
                assert embedding.values is not None
                embeddings.append(embedding.values)
            recorder.record_completed(result, attempt_number=attempt_number)
            return embeddings
        except APIError as e:
            recorder.record_failed(e, attempt_number=attempt_number)
            raise Exception(f"Gemini Embedding API批量请求失败: {e.message}")
        except BaseException as exc:
            recorder.record_failed(exc, attempt_number=attempt_number)
            raise

    def get_dim(self) -> int:
        """获取向量的维度"""
        return int(self.provider_config.get("embedding_dimensions", 768))

    async def terminate(self):
        if self.client:
            await self.client.aclose()
