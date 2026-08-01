"""Core execution tracing for AstrBot.

The package exports only the stable plugin-facing primitives.  Storage layout,
database schema, and automatic instrumentation remain implementation details.
"""

from .models import (
    TERMINAL_TRACE_STATUSES,
    TRACE_STATUSES,
    new_span_id,
    new_trace_id,
)
from .service import (
    NOOP_PLUGIN_TRACER,
    NoopTraceSpan,
    PluginTracer,
    TraceService,
    TraceSpan,
)

__all__ = [
    "TERMINAL_TRACE_STATUSES",
    "TRACE_STATUSES",
    "NOOP_PLUGIN_TRACER",
    "NoopTraceSpan",
    "PluginTracer",
    "TraceService",
    "TraceSpan",
    "new_span_id",
    "new_trace_id",
]
