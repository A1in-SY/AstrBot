"""本地 Agent 模式的 AstrBot 插件调用 Stage"""

import asyncio
import traceback
from collections.abc import AsyncGenerator
from typing import Any

from astrbot.core import logger
from astrbot.core.message.message_event_result import MessageEventResult
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.star.star import star_map
from astrbot.core.star.star_handler import EventType, StarHandlerMetadata

from ...context import PipelineContext, call_event_hook, call_handler
from ...context_utils import iterate_async_generator_with_trace_span
from ..stage import Stage


class StarRequestSubStage(Stage):
    async def initialize(self, ctx: PipelineContext) -> None:
        self.prompt_prefix = ctx.astrbot_config["provider_settings"]["prompt_prefix"]
        self.identifier = ctx.astrbot_config["provider_settings"]["identifier"]
        self.ctx = ctx

    async def process(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        trace_service = getattr(self.ctx, "trace_service", None)
        activated_handlers: list[StarHandlerMetadata] = event.get_extra(
            "activated_handlers",
        )
        handlers_parsed_params: dict[str, dict[str, Any]] = event.get_extra(
            "handlers_parsed_params",
        )
        if not handlers_parsed_params:
            handlers_parsed_params = {}

        for invocation_index, handler in enumerate(activated_handlers, start=1):
            if event.is_stopped():
                break
            params = handlers_parsed_params.get(handler.handler_full_name, {})
            md = star_map.get(handler.handler_module_path)
            if not md:
                logger.warning(
                    f"Cannot find plugin for given handler module path: {handler.handler_module_path}",
                )
                continue
            logger.debug(f"plugin -> {md.name} - {handler.handler_name}")
            trace_span = (
                trace_service.start_span(
                    "plugin.handler",
                    kind="plugin",
                    source="plugin",
                    plugin_id=md.plugin_id,
                    materialize=False,
                    attributes={
                        "handler": handler.handler_name,
                        "event_type": handler.event_type.name,
                        "priority": handler.extras_configs.get("priority", 0),
                        "invocation_index": invocation_index,
                        "event_stopped_before": event.is_stopped(),
                        "result_present_before": event.get_result() is not None,
                    },
                )
                if trace_service is not None
                else None
            )
            try:
                if trace_span is None:
                    wrapper = call_handler(event, handler.handler, **params)
                    traced_wrapper = iterate_async_generator_with_trace_span(
                        wrapper,
                        None,
                    )
                    try:
                        async for ret in traced_wrapper:
                            yield ret
                    finally:
                        await traced_wrapper.aclose()
                else:
                    result_before = event.get_result()
                    yield_count = 0
                    terminal_error: BaseException | None = None
                    wrapper = call_handler(event, handler.handler, **params)
                    traced_wrapper = iterate_async_generator_with_trace_span(
                        wrapper,
                        trace_span,
                    )
                    try:
                        async for ret in traced_wrapper:
                            yield_count += 1
                            yield ret
                    except BaseException as exc:
                        terminal_error = exc
                        raise
                    finally:
                        close_error: BaseException | None = None
                        raise_close_error = False
                        try:
                            await traced_wrapper.aclose()
                        except BaseException as exc:
                            close_error = exc
                            if terminal_error is None:
                                terminal_error = exc
                                raise_close_error = True
                            else:
                                trace_span.set_attributes(
                                    close_exception_type=type(exc).__name__,
                                )
                        finally:
                            result_after = event.get_result()
                            if result_before is result_after:
                                result_mutation = "unchanged"
                            elif result_before is None:
                                result_mutation = "set"
                            elif result_after is None:
                                result_mutation = "cleared"
                            else:
                                result_mutation = "replaced"
                            trace_span.set_attributes(
                                yield_count=yield_count,
                                event_stopped_after=event.is_stopped(),
                                result_present_after=result_after is not None,
                                result_mutation=result_mutation,
                            )
                            if terminal_error is None:
                                trace_span.add_event(
                                    "plugin.handler.completed",
                                    result_mutation=result_mutation,
                                )
                                trace_span.finish()
                            elif isinstance(terminal_error, asyncio.CancelledError):
                                trace_span.finish(
                                    status="cancelled",
                                    outcome="cancelled",
                                )
                            elif isinstance(terminal_error, GeneratorExit):
                                trace_span.finish(
                                    status="cancelled",
                                    outcome="generator_closed",
                                )
                            else:
                                trace_span.set_attributes(
                                    exception_type=type(terminal_error).__name__,
                                )
                                trace_span.finish(
                                    status="error",
                                    outcome="exception",
                                )
                        if raise_close_error and close_error is not None:
                            raise close_error
                if event.is_stopped():
                    break
                event.clear_result()  # 清除上一个 handler 的结果
            except Exception as e:
                if trace_service is not None:
                    trace_service.materialize()
                traceback_text = traceback.format_exc()
                logger.error(traceback_text)
                logger.error(f"Star {handler.handler_full_name} handle error: {e}")

                await call_event_hook(
                    event,
                    EventType.OnPluginErrorEvent,
                    md.name,
                    handler.handler_name,
                    e,
                    traceback_text,
                )

                if not event.is_stopped() and event.is_at_or_wake_command:
                    ret = f":(\n\n在调用插件 {md.name} 的处理函数 {handler.handler_name} 时出现异常：{e}"
                    event.set_result(MessageEventResult().message(ret))
                    yield
                    event.clear_result()

                event.stop_event()
