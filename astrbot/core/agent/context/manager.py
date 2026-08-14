from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from astrbot import logger
from astrbot.core.trace.context import current_trace_service
from astrbot.core.trace.service import NoopTraceSpan

from ..message import Message
from .compressor import LLMSummaryCompressor, TruncateByTurnsCompressor
from .config import ContextConfig
from .token_counter import EstimateTokenCounter
from .truncator import ContextTruncator

if TYPE_CHECKING:
    from astrbot.core.provider.entities import LLMResponse


class ContextManager:
    """Context compression manager."""

    def __init__(
        self,
        config: ContextConfig,
        llm_request_executor: Callable[
            [Callable[[], Awaitable["LLMResponse"]], str],
            Awaitable["LLMResponse"],
        ]
        | None = None,
    ) -> None:
        """Initialize the context manager.

        There are two strategies to handle context limit reached:
        1. Truncate by turns: remove older messages by turns.
        2. LLM-based compression: use LLM to summarize old messages.

        Args:
            config: The context configuration.
            llm_request_executor: Optional trace-aware wrapper for LLM summary
                requests. The string argument identifies the logical call kind.
        """
        self.config = config

        self.token_counter = config.custom_token_counter or EstimateTokenCounter()
        self.truncator = ContextTruncator()

        if config.custom_compressor:
            self.compressor = config.custom_compressor
        elif config.llm_compress_provider:
            self.compressor = LLMSummaryCompressor(
                provider=config.llm_compress_provider,
                keep_recent_ratio=config.llm_compress_keep_recent_ratio,
                instruction_text=config.llm_compress_instruction,
                token_counter=self.token_counter,
                llm_request_executor=llm_request_executor,
            )
        else:
            self.compressor = TruncateByTurnsCompressor(
                truncate_turns=config.truncate_turns
            )

    async def process(
        self, messages: list[Message], trusted_token_usage: int = 0
    ) -> list[Message]:
        """Process the messages.

        Args:
            messages: The original message list.

        Returns:
            The processed message list.
        """
        try:
            result = messages

            # 1. 基于轮次的截断 (Enforce max turns)
            if self.config.enforce_max_turns != -1:
                result = self.truncator.truncate_by_turns(
                    result,
                    keep_most_recent_turns=self.config.enforce_max_turns,
                    drop_turns=self.config.truncate_turns,
                )

            # 2. 基于 token 的压缩
            if self.config.max_context_tokens > 0:
                total_tokens = self.token_counter.count_tokens(
                    result, trusted_token_usage
                )

                if self.compressor.should_compress(
                    result, total_tokens, self.config.max_context_tokens
                ):
                    result = await self._run_compression(result, total_tokens)

            return result
        except Exception as e:
            logger.error(f"Error during context processing: {e}", exc_info=True)
            return messages

    async def _run_compression(
        self, messages: list[Message], prev_tokens: int
    ) -> list[Message]:
        """
        Compress/truncate the messages.

        Args:
            messages: The original message list.
            prev_tokens: The token count before compression.

        Returns:
            The compressed/truncated message list.
        """
        logger.debug("Compress triggered, starting compression...")

        trace_service = current_trace_service()
        span = (
            trace_service.start_span(
                "agent.context.compress",
                kind="agent",
                attributes={
                    "trigger_reason": "token_threshold",
                    "compressor": type(self.compressor).__name__,
                    "messages_before": len(messages),
                    "tokens_before": prev_tokens,
                    "max_context_tokens": self.config.max_context_tokens,
                },
            )
            if trace_service is not None
            else NoopTraceSpan()
        )
        original_message_count = len(messages)
        original_messages = list(messages)
        with span:
            messages = await self.compressor(messages)

            # double check
            tokens_after_summary = self.token_counter.count_tokens(messages)

            # calculate compress rate
            compress_rate = (
                tokens_after_summary / self.config.max_context_tokens
            ) * 100
            logger.info(
                f"Compress completed."
                f" {prev_tokens} -> {tokens_after_summary} tokens,"
                f" compression rate: {compress_rate:.2f}%.",
            )

            fallback_truncation = self.compressor.should_compress(
                messages, tokens_after_summary, self.config.max_context_tokens
            )
            if fallback_truncation:
                logger.info(
                    "Context still exceeds max tokens after compression, applying halving truncation..."
                )
                messages = self.truncator.truncate_by_halving(messages)
                tokens_after_summary = self.token_counter.count_tokens(messages)
            final_message_ids = {id(message) for message in messages}
            retained_message_count = sum(
                1 for message in original_messages if id(message) in final_message_ids
            )
            summarized_ids = getattr(
                self.compressor,
                "_last_trace_summarized_message_ids",
                set(),
            )
            summary_message_id = getattr(
                self.compressor,
                "_last_trace_summary_message_id",
                None,
            )
            summarized_message_count = (
                sum(1 for message in original_messages if id(message) in summarized_ids)
                if summary_message_id in final_message_ids
                else 0
            )
            dropped_message_count = max(
                0,
                original_message_count
                - retained_message_count
                - summarized_message_count,
            )
            span.set_attributes(
                messages_after=len(messages),
                messages_removed=max(0, original_message_count - len(messages)),
                retained_message_count=retained_message_count,
                summarized_message_count=summarized_message_count,
                dropped_message_count=dropped_message_count,
                compression_model_call_count=int(
                    bool(
                        getattr(
                            self.compressor,
                            "_last_trace_model_call_attempted",
                            False,
                        )
                    )
                ),
                tokens_after=tokens_after_summary,
                retained_token_ratio=(
                    round(tokens_after_summary / prev_tokens, 6)
                    if prev_tokens > 0
                    else None
                ),
                fallback_truncation=fallback_truncation,
            )

        return messages
