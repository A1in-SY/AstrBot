"""Restricted Python runtime for non-agent script tasks.

This package is intentionally isolated from ``astrbot.core``.  The host
process and the one-shot worker both use it, but the worker never imports
AstrBot core modules.
"""

from astrbot.script_runtime.spec import (
    DEFAULT_LANGUAGE_VERSION,
    LANGUAGE_VERSION,
    SUPPORTED_LANGUAGE_VERSIONS,
    build_compact_contract,
    build_reference_doc,
    coerce_limits,
)
from astrbot.script_runtime.validator import (
    compute_source_hash,
    validate_source,
)

__all__ = [
    "DEFAULT_LANGUAGE_VERSION",
    "LANGUAGE_VERSION",
    "SUPPORTED_LANGUAGE_VERSIONS",
    "build_compact_contract",
    "build_reference_doc",
    "coerce_limits",
    "compute_source_hash",
    "validate_source",
]
