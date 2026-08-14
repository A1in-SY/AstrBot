import asyncio
import os
from http import HTTPStatus

from dashscope import MultiModalEmbedding, TextEmbedding

from astrbot import logger
from astrbot.core.trace.outbound import (
    OutboundCallRecorder,
    OutboundRequestSnapshot,
    record_outbound_response_summary,
)

from ..entities import ProviderType
from ..provider import EmbeddingProvider
from ..register import register_provider_adapter

_DEFAULT_API_BASE = "https://dashscope.aliyuncs.com/api/v1"
_DEFAULT_MODEL = "text-embedding-v4"


@register_provider_adapter(
    "dashscope_embedding",
    "阿里云百炼(DashScope) Embedding 提供商适配器",
    provider_type=ProviderType.EMBEDDING,
)
class DashScopeEmbeddingProvider(EmbeddingProvider):
    """DashScope (Aliyun Bailian) embedding provider via the native protocol.

    Routes text embedding models (text-embedding-*) through TextEmbedding and
    multimodal embedding models (qwen*-vl-embedding, multimodal-embedding-*,
    tongyi-embedding-vision-*) through MultiModalEmbedding, so that models
    unavailable in the OpenAI-compatible mode can be used.
    """

    _astrbot_deep_outbound = True

    def __init__(self, provider_config: dict, provider_settings: dict) -> None:
        super().__init__(provider_config, provider_settings)
        self.provider_config = provider_config

        self.api_key = provider_config.get("embedding_api_key") or os.getenv(
            "DASHSCOPE_API_KEY", ""
        )
        if not self.api_key:
            raise ValueError("阿里云百炼(DashScope) Embedding API Key 不能为空。")

        self.base_url = provider_config.get("embedding_api_base", _DEFAULT_API_BASE)
        self.model = provider_config.get("embedding_model", _DEFAULT_MODEL)

        provider_id = provider_config.get("id", "unknown_id")
        logger.info(
            f"[DashScope Embedding] {provider_id} Initialized via native SDK, "
            f"base_url={self.base_url}, model={self.model}"
        )
        self.set_model(self.model)

    async def get_embedding(self, text: str) -> list[float]:
        """Get the embedding vector for a single text."""
        embeddings = await self.get_embeddings([text])
        return embeddings[0] if embeddings else []

    async def get_embeddings(self, text: list[str]) -> list[list[float]]:
        """Get the embedding vectors for a batch of texts via the dashscope SDK.

        Multimodal models (e.g. qwen3-vl-embedding) use the
        multimodal-embedding endpoint and accept text wrapped in content dicts;
        text models use the text-embedding endpoint directly.
        """
        if not text:
            return []

        is_multimodal = (
            "vl-embedding" in self.model
            or self.model.startswith("multimodal-embedding")
            or self.model.startswith("tongyi-embedding-vision")
        )

        kwargs: dict = {"base_address": self.base_url}
        if "embedding_dimensions" in self.provider_config:
            try:
                dimensions = int(self.provider_config["embedding_dimensions"])
                if dimensions > 0:
                    kwargs["dimension"] = dimensions
            except (ValueError, TypeError):
                logger.warning(
                    f"embedding_dimensions in embedding configs is not a valid integer: "
                    f"'{self.provider_config['embedding_dimensions']}', ignored."
                )

        # The dashscope SDK is synchronous; run it in a worker thread.
        # base_address is passed per-call to avoid racing on the module-level
        # dashscope.base_http_api_url global under concurrent usage.
        def _call():
            if is_multimodal:
                return MultiModalEmbedding.call(
                    model=self.model,
                    input=[{"text": t} for t in text],
                    api_key=self.api_key,
                    **kwargs,
                )
            return TextEmbedding.call(
                model=self.model,
                input=text,
                api_key=self.api_key,
                **kwargs,
            )

        task = "multimodal-embedding" if is_multimodal else "text-embedding"
        recorder = OutboundCallRecorder(
            OutboundRequestSnapshot(
                api_family="dashscope.embedding",
                sdk_operation=(
                    "MultiModalEmbedding.call"
                    if is_multimodal
                    else "TextEmbedding.call"
                ),
                base_url=self.base_url,
                resource_path=f"/services/embeddings/{task}/{task}",
                route_resolution="sdk_declared",
                parameters={
                    "model": self.model,
                    "input": text,
                    "dimension": kwargs.get("dimension"),
                },
                transformations=(
                    ("wrap_multimodal_text_inputs",) if is_multimodal else ()
                ),
            )
        )
        attempt_number = recorder.record_attempt()
        try:
            resp = await asyncio.to_thread(_call)
        except BaseException as exc:
            recorder.record_failed(exc, attempt_number=attempt_number)
            raise

        if resp.status_code != HTTPStatus.OK:
            request_url = (
                self.base_url.rstrip("/") + f"/services/embeddings/{task}/{task}"
            )
            request_id = getattr(resp, "request_id", "") or ""
            error = Exception(
                f"DashScope Embedding API request failed (HTTP {resp.status_code}): "
                f"{resp.code or '(no code)'} - {resp.message or '(no message)'}"
                f" [url={request_url}]"
                + (f" [request_id={request_id}]" if request_id else "")
            )
            recorder.record_failed(
                error,
                attempt_number=attempt_number,
                status_code=int(resp.status_code),
            )
            raise error

        recorder.record_completed(resp, attempt_number=attempt_number)
        record_outbound_response_summary(usage=getattr(resp, "usage", None))

        embeddings = resp.output.get("embeddings", []) if resp.output else []
        if not embeddings:
            raise Exception(f"[DashScope Embedding] No embeddings returned: {resp}")

        # Text embedding uses text_index; multimodal uses index.
        return [
            item["embedding"]
            for item in sorted(
                embeddings, key=lambda x: x.get("text_index", x.get("index", 0))
            )
        ]

    def get_dim(self) -> int:
        """Get the configured embedding dimension."""
        if "embedding_dimensions" in self.provider_config:
            try:
                return int(self.provider_config["embedding_dimensions"])
            except (ValueError, TypeError):
                logger.warning(
                    f"embedding_dimensions in embedding configs is not a valid integer: "
                    f"'{self.provider_config['embedding_dimensions']}', ignored."
                )
        return 0
