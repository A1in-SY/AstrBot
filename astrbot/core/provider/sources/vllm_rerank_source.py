import aiohttp

from astrbot.core.trace.outbound import (
    OutboundCallRecorder,
    OutboundRequestSnapshot,
)

from ..entities import ProviderType, RerankResult
from ..provider import RerankProvider
from ..register import register_provider_adapter


@register_provider_adapter(
    "vllm_rerank",
    "VLLM Rerank 适配器",
    provider_type=ProviderType.RERANK,
)
class VLLMRerankProvider(RerankProvider):
    _astrbot_deep_outbound = True

    def __init__(self, provider_config: dict, provider_settings: dict) -> None:
        super().__init__(provider_config, provider_settings)
        self.provider_config = provider_config
        self.provider_settings = provider_settings
        self.auth_key = provider_config.get("rerank_api_key", "")
        self.base_url = provider_config.get("rerank_api_base", "http://127.0.0.1:8000")
        self.base_url = self.base_url.rstrip("/")
        self.api_suffix = provider_config.get("rerank_api_suffix", "/v1/rerank")
        if self.api_suffix is None:
            self.api_suffix = "/v1/rerank"
        if self.api_suffix and not self.api_suffix.startswith("/"):
            self.api_suffix = "/" + self.api_suffix
        self.timeout = provider_config.get("timeout", 20)
        self.model = provider_config.get("rerank_model", "BAAI/bge-reranker-base")

        h = {}
        if self.auth_key:
            h["Authorization"] = f"Bearer {self.auth_key}"
        self.client = aiohttp.ClientSession(
            headers=h,
            timeout=aiohttp.ClientTimeout(total=self.timeout),
        )

    async def rerank(
        self,
        query: str,
        documents: list[str],
        top_n: int | None = None,
    ) -> list[RerankResult]:
        if not documents:
            return []

        payload = {
            "query": query,
            "documents": documents,
            "model": self.model,
        }
        if top_n is not None:
            payload["top_n"] = top_n
        assert self.client is not None
        rerank_url = f"{self.base_url}{self.api_suffix}"
        recorder = OutboundCallRecorder(
            OutboundRequestSnapshot(
                api_family="vllm.rerank",
                sdk_operation="aiohttp.ClientSession.post",
                base_url=self.base_url,
                resource_path=self.api_suffix,
                route_resolution="constructed",
                timeout_seconds=getattr(self, "timeout", None),
                parameters=payload,
            )
        )
        attempt_number = recorder.record_attempt()
        try:
            async with self.client.post(
                rerank_url,
                json=payload,
            ) as response:
                response.raise_for_status()
                response_data = await response.json()
                if not isinstance(response_data, dict):
                    raise ValueError("Rerank API response must be a JSON object")

                results = response_data.get("results")
                if not isinstance(results, list) or not results:
                    raise ValueError(
                        "Rerank API response must contain a non-empty 'results' list"
                    )

                try:
                    parsed = [
                        RerankResult(
                            index=result["index"],
                            relevance_score=result["relevance_score"],
                        )
                        for result in results
                    ]
                except (KeyError, TypeError) as exc:
                    raise ValueError("Rerank API returned invalid result data") from exc
                recorder.record_completed(response, attempt_number=attempt_number)
                return parsed
        except BaseException as exc:
            recorder.record_failed(exc, attempt_number=attempt_number)
            raise

    async def terminate(self) -> None:
        """关闭客户端会话"""
        if self.client:
            await self.client.close()
            self.client = None
