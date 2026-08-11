"""Single source of truth for the ``astrbot-python-subset`` language.

This module must stay import-light: it is used by the one-shot validation and
execution workers as well as by the AstrBot host process.  It must never
import ``astrbot.core``.

Version policy:

- ``v1`` is append-only once released.  Adding a member, a module, or a
  signature change requires a new language version (``v2``).
- Every validator rule, interpreter dispatch rule and generated documentation
  block is derived from this registry so they cannot drift apart.
"""

from __future__ import annotations

from typing import Any

# The canonical language version.  Short aliases such as "v1" are never
# accepted anywhere; this exact string is stored in the database, sent over
# IPC and shown in the Dashboard.
LANGUAGE_VERSION = "astrbot-python-subset/v1"
DEFAULT_LANGUAGE_VERSION = LANGUAGE_VERSION
SUPPORTED_LANGUAGE_VERSIONS = (LANGUAGE_VERSION,)

# Fixed grammar level.  v1 deliberately parses with CPython 3.10 grammar so
# the same source is accepted on every supported AstrBot Python runtime.
FEATURE_VERSION = (3, 10)
FEATURE_VERSION_INT = 10

# ---------------------------------------------------------------------------
# Default limits (mirrored by the global AstrBot config under script_task).
# ---------------------------------------------------------------------------

DEFAULT_LIMITS = {
    "execution_timeout_seconds": 30,
    "max_source_bytes": 65536,
    "max_ast_nodes": 10000,
    "max_ast_depth": 100,
}

LIMIT_KEYS = tuple(sorted(DEFAULT_LIMITS))


def coerce_limits(limits: dict[str, Any] | None) -> dict[str, int]:
    """Return a validated limits snapshot.

    Unknown keys are ignored; missing keys fall back to defaults; non-positive
    or non-numeric values are rejected.  Raises ``ValueError``.
    """
    coerced = dict(DEFAULT_LIMITS)
    if limits:
        for key, raw in limits.items():
            if key not in DEFAULT_LIMITS:
                continue
            try:
                value = int(raw)
            except (TypeError, ValueError):
                raise ValueError(f"limit {key} must be an integer") from None
            if value <= 0:
                raise ValueError(f"limit {key} must be positive")
            coerced[key] = value
    return coerced


# ---------------------------------------------------------------------------
# AST grammar subset
# ---------------------------------------------------------------------------

ALLOWED_STATEMENTS = frozenset(
    {
        "Import",
        "ImportFrom",
        "FunctionDef",
        "AsyncFunctionDef",
        "Assign",
        "AugAssign",
        "Expr",
        "If",
        "For",
        "While",
        "Break",
        "Continue",
        "Pass",
        "Return",
        "Try",
        "Raise",
    }
)

ALLOWED_EXPRESSIONS = frozenset(
    {
        "Constant",
        "Name",
        "List",
        "Tuple",
        "Set",
        "Dict",
        "BinOp",
        "UnaryOp",
        "BoolOp",
        "Compare",
        "IfExp",
        "Subscript",
        "Slice",
        "Attribute",
        "Call",
        "Await",
        "ListComp",
        "SetComp",
        "DictComp",
        "JoinedStr",
        "FormattedValue",
    }
)

ALLOWED_OPERATORS = frozenset(
    {
        "Add",
        "Sub",
        "Mult",
        "Div",
        "FloorDiv",
        "Mod",
        "Pow",
        "BitAnd",
        "BitOr",
        "BitXor",
        "LShift",
        "RShift",
        "UAdd",
        "USub",
        "Invert",
        "Not",
        "And",
        "Or",
        "Eq",
        "NotEq",
        "Lt",
        "LtE",
        "Gt",
        "GtE",
        "Is",
        "IsNot",
        "In",
        "NotIn",
    }
)

ALLOWED_STARTS_OF_STATEMENTS = ALLOWED_STATEMENTS

# These nodes are rejected explicitly even though their parent node type is
# allowed, so the diagnostics can point at the offending node.
FORBIDDEN_NODE_HINTS = {
    "AnnAssign": "NODE_NOT_ALLOWED",
    "NamedExpr": "NODE_NOT_ALLOWED",
    "Assert": "NODE_NOT_ALLOWED",
    "Delete": "NODE_NOT_ALLOWED",
    "With": "NODE_NOT_ALLOWED",
    "AsyncWith": "NODE_NOT_ALLOWED",
    "AsyncFor": "NODE_NOT_ALLOWED",
    "Match": "NODE_NOT_ALLOWED",
    "TryStar": "NODE_NOT_ALLOWED",
    "ClassDef": "NODE_NOT_ALLOWED",
    "Lambda": "NODE_NOT_ALLOWED",
    "Yield": "NODE_NOT_ALLOWED",
    "YieldFrom": "NODE_NOT_ALLOWED",
    "GeneratorExp": "NODE_NOT_ALLOWED",
    "Global": "NODE_NOT_ALLOWED",
    "Nonlocal": "NODE_NOT_ALLOWED",
    "TypeAlias": "NODE_NOT_ALLOWED",
    "Starred": "NODE_NOT_ALLOWED",
}

# ---------------------------------------------------------------------------
# Builtins
# ---------------------------------------------------------------------------

ALLOWED_BUILTINS = frozenset(
    {
        "abs",
        "all",
        "any",
        "bin",
        "bool",
        "bytes",
        "chr",
        "dict",
        "divmod",
        "enumerate",
        "float",
        "format",
        "hex",
        "int",
        "isinstance",
        "len",
        "list",
        "max",
        "min",
        "oct",
        "ord",
        "pow",
        "range",
        "repr",
        "reversed",
        "round",
        "set",
        "sorted",
        "str",
        "sum",
        "tuple",
        "zip",
    }
)

# Builtins that are deliberately absent from the script namespace.
FORBIDDEN_BUILTINS = frozenset(
    {
        "print",
        "object",
        "type",
        "super",
        "property",
        "map",
        "filter",
        "iter",
        "next",
        "open",
        "eval",
        "exec",
        "compile",
        "globals",
        "locals",
        "vars",
        "dir",
        "getattr",
        "setattr",
        "delattr",
        "hasattr",
        "__import__",
    }
)

# ---------------------------------------------------------------------------
# Exceptions the script may raise or catch.
# ---------------------------------------------------------------------------

ALLOWED_EXCEPTIONS = frozenset(
    {
        "Exception",
        "ArithmeticError",
        "ZeroDivisionError",
        "OverflowError",
        "LookupError",
        "KeyError",
        "IndexError",
        "ValueError",
        "TypeError",
        "NameError",
        "RuntimeError",
        "HttpError",
        "HttpInvalidRequestError",
        "HttpTimeoutError",
        "HttpProxyError",
        "HttpConnectionError",
        "HttpProtocolError",
        "HttpDecodeError",
        "SendError",
        "SendTargetUnavailableError",
        "StateError",
        "StateNotJsonError",
    }
)

ALLOWED_EXCEPTION_ALIASES = {
    # (module, member) -> canonical exception name exposed to the script.
    ("astrbot.script_runtime.errors", "HttpError"): "HttpError",
    (
        "astrbot.script_runtime.errors",
        "HttpInvalidRequestError",
    ): "HttpInvalidRequestError",
    ("astrbot.script_runtime.errors", "HttpTimeoutError"): "HttpTimeoutError",
    ("astrbot.script_runtime.errors", "HttpProxyError"): "HttpProxyError",
    ("astrbot.script_runtime.errors", "HttpConnectionError"): "HttpConnectionError",
    ("astrbot.script_runtime.errors", "HttpProtocolError"): "HttpProtocolError",
    ("astrbot.script_runtime.errors", "HttpDecodeError"): "HttpDecodeError",
    ("astrbot.script_runtime.errors", "SendError"): "SendError",
    (
        "astrbot.script_runtime.errors",
        "SendTargetUnavailableError",
    ): "SendTargetUnavailableError",
    ("astrbot.script_runtime.errors", "StateError"): "StateError",
    ("astrbot.script_runtime.errors", "StateNotJsonError"): "StateNotJsonError",
}

# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------

#: module name -> member -> member kind.  Member kinds are:
#:
#: - "const": scalar constant exposed as a plain value
#: - "callable": module-level function (allowed as a direct call target)
#: - "class": class object (allowed as a constructor call target)
#: - "value": module-level value (attribute access allowed, no calls)
#:
#: Dotted modules must be imported with an explicit alias.
MODULES: dict[str, dict[str, str]] = {
    "datetime": {
        "MINYEAR": "const",
        "MAXYEAR": "const",
        "date": "class",
        "datetime": "class",
        "time": "class",
        "timedelta": "class",
        "timezone": "class",
    },
    "time": {
        "time": "callable",
        "time_ns": "callable",
        "monotonic": "callable",
        "monotonic_ns": "callable",
        "perf_counter": "callable",
        "perf_counter_ns": "callable",
        "gmtime": "callable",
        "localtime": "callable",
        "mktime": "callable",
        "strftime": "callable",
        "strptime": "callable",
        "sleep": "callable",
    },
    "zoneinfo": {
        "ZoneInfo": "class",
    },
    "math": {
        "e": "const",
        "pi": "const",
        "tau": "const",
        "inf": "const",
        "nan": "const",
        "acos": "callable",
        "acosh": "callable",
        "asin": "callable",
        "asinh": "callable",
        "atan": "callable",
        "atan2": "callable",
        "atanh": "callable",
        "ceil": "callable",
        "comb": "callable",
        "copysign": "callable",
        "cos": "callable",
        "cosh": "callable",
        "degrees": "callable",
        "dist": "callable",
        "erf": "callable",
        "erfc": "callable",
        "exp": "callable",
        "expm1": "callable",
        "fabs": "callable",
        "factorial": "callable",
        "floor": "callable",
        "fmod": "callable",
        "frexp": "callable",
        "fsum": "callable",
        "gamma": "callable",
        "gcd": "callable",
        "hypot": "callable",
        "isclose": "callable",
        "isfinite": "callable",
        "isinf": "callable",
        "isnan": "callable",
        "isqrt": "callable",
        "lcm": "callable",
        "ldexp": "callable",
        "lgamma": "callable",
        "log": "callable",
        "log10": "callable",
        "log1p": "callable",
        "log2": "callable",
        "modf": "callable",
        "nextafter": "callable",
        "perm": "callable",
        "pow": "callable",
        "prod": "callable",
        "radians": "callable",
        "remainder": "callable",
        "sin": "callable",
        "sinh": "callable",
        "sqrt": "callable",
        "tan": "callable",
        "tanh": "callable",
        "trunc": "callable",
        "ulp": "callable",
    },
    "decimal": {
        "Decimal": "class",
        "ROUND_CEILING": "const",
        "ROUND_DOWN": "const",
        "ROUND_FLOOR": "const",
        "ROUND_HALF_DOWN": "const",
        "ROUND_HALF_EVEN": "const",
        "ROUND_HALF_UP": "const",
        "ROUND_UP": "const",
        "ROUND_05UP": "const",
    },
    "json": {
        "loads": "callable",
        "dumps": "callable",
    },
    "re": {
        "ASCII": "const",
        "IGNORECASE": "const",
        "MULTILINE": "const",
        "DOTALL": "const",
        "VERBOSE": "const",
        "compile": "callable",
        "escape": "callable",
        "fullmatch": "callable",
        "findall": "callable",
        "finditer": "callable",
        "match": "callable",
        "search": "callable",
        "split": "callable",
        "sub": "callable",
        "subn": "callable",
    },
    "base64": {
        "b64encode": "callable",
        "b64decode": "callable",
        "b16encode": "callable",
        "b16decode": "callable",
        "b32encode": "callable",
        "b32decode": "callable",
        "b32hexencode": "callable",
        "b32hexdecode": "callable",
        "b85encode": "callable",
        "b85decode": "callable",
        "a85encode": "callable",
        "a85decode": "callable",
        "encodebytes": "callable",
        "decodebytes": "callable",
    },
    "hashlib": {
        "md5": "callable",
        "sha1": "callable",
        "sha224": "callable",
        "sha256": "callable",
        "sha384": "callable",
        "sha512": "callable",
        "sha3_224": "callable",
        "sha3_256": "callable",
        "sha3_384": "callable",
        "sha3_512": "callable",
        "shake_128": "callable",
        "shake_256": "callable",
        "blake2b": "callable",
        "blake2s": "callable",
        "new": "callable",
        "algorithms_guaranteed": "const",
    },
    "hmac": {
        "new": "callable",
        "digest": "callable",
        "compare_digest": "callable",
    },
    "urllib.parse": {
        "urlparse": "callable",
        "urlsplit": "callable",
        "urlunparse": "callable",
        "urlunsplit": "callable",
        "urljoin": "callable",
        "urldefrag": "callable",
        "urlencode": "callable",
        "parse_qs": "callable",
        "parse_qsl": "callable",
        "quote": "callable",
        "quote_plus": "callable",
        "quote_from_bytes": "callable",
        "unquote": "callable",
        "unquote_plus": "callable",
        "unquote_to_bytes": "callable",
    },
}

MODULE_NAMES = frozenset(MODULES)

# Members that must never be added to a module even if the underlying Python
# module grows them (guards against future accidental expansion).
FORBIDDEN_MODULE_MEMBERS: dict[str, frozenset[str]] = {
    "hashlib": frozenset(
        {
            "file_digest",
            "algorithms_available",
            "pbkdf2_hmac",
            "scrypt",
            "compare_digest",
        }
    ),
    "re": frozenset({"template"}),
}

# ---------------------------------------------------------------------------
# Value-kind method dispatch tables
# ---------------------------------------------------------------------------

KIND_STR = "str"
KIND_BYTES = "bytes"
KIND_LIST = "list"
KIND_DICT = "dict"
KIND_SET = "set"
KIND_TUPLE = "tuple"
KIND_RANGE = "range"
KIND_INT = "int"
KIND_FLOAT = "float"
KIND_BOOL = "bool"
KIND_NONE = "none"
KIND_DECIMAL = "decimal"
KIND_DATETIME = "datetime"
KIND_DATE = "date"
KIND_TIME = "time"
KIND_TIMEDELTA = "timedelta"
KIND_TIMEZONE = "timezone"
KIND_ZONEINFO = "zoneinfo"
KIND_STRUCT_TIME = "struct_time"
KIND_REGEX_PATTERN = "regex_pattern"
KIND_REGEX_MATCH = "regex_match"
KIND_DIGEST = "digest"
KIND_HMAC = "hmac"
KIND_URL_RESULT = "url_result"
KIND_HEADERS = "headers"
KIND_HTTP_RESPONSE = "http_response"
KIND_STATE = "state"
KIND_EXCEPTION = "exception"
KIND_MODULE = "module"
KIND_CALLABLE = "callable"
KIND_CLASS = "class"
KIND_USER_SYNC_FUNC = "user_sync_func"
KIND_USER_ASYNC_FUNC = "user_async_func"
KIND_CTX = "ctx"
KIND_CTX_HTTP = "ctx_http"
KIND_CTX_RUN = "ctx_run"
KIND_CTX_STATE = "ctx_state"
KIND_SEND_TEXT = "send_text"
KIND_REQUEST = "request"
KIND_UNKNOWN = "unknown"

METHODS: dict[str, frozenset[str]] = {
    KIND_STR: frozenset(
        {
            "capitalize",
            "casefold",
            "center",
            "count",
            "endswith",
            "expandtabs",
            "find",
            "index",
            "isalnum",
            "isalpha",
            "isascii",
            "isdecimal",
            "isdigit",
            "isidentifier",
            "islower",
            "isnumeric",
            "isprintable",
            "isspace",
            "istitle",
            "isupper",
            "join",
            "ljust",
            "lower",
            "lstrip",
            "partition",
            "removeprefix",
            "removesuffix",
            "replace",
            "rfind",
            "rindex",
            "rjust",
            "rpartition",
            "rsplit",
            "rstrip",
            "split",
            "splitlines",
            "startswith",
            "strip",
            "swapcase",
            "title",
            "upper",
            "zfill",
            "encode",
        }
    ),
    KIND_BYTES: frozenset(
        {
            "count",
            "decode",
            "endswith",
            "find",
            "hex",
            "index",
            "join",
            "lower",
            "lstrip",
            "partition",
            "removeprefix",
            "removesuffix",
            "replace",
            "rfind",
            "rindex",
            "rpartition",
            "rsplit",
            "rstrip",
            "split",
            "splitlines",
            "startswith",
            "strip",
            "upper",
        }
    ),
    KIND_LIST: frozenset(
        {
            "append",
            "clear",
            "copy",
            "count",
            "extend",
            "index",
            "insert",
            "pop",
            "remove",
            "reverse",
            "sort",
        }
    ),
    KIND_DICT: frozenset(
        {
            "clear",
            "copy",
            "get",
            "items",
            "keys",
            "pop",
            "popitem",
            "setdefault",
            "update",
            "values",
        }
    ),
    KIND_STATE: frozenset(
        {
            "clear",
            "copy",
            "get",
            "items",
            "keys",
            "pop",
            "popitem",
            "setdefault",
            "update",
            "values",
        }
    ),
    KIND_SET: frozenset(
        {
            "add",
            "clear",
            "copy",
            "difference",
            "difference_update",
            "discard",
            "intersection",
            "intersection_update",
            "isdisjoint",
            "issubset",
            "issuperset",
            "pop",
            "remove",
            "symmetric_difference",
            "symmetric_difference_update",
            "union",
            "update",
        }
    ),
    KIND_TUPLE: frozenset({"count", "index"}),
    KIND_RANGE: frozenset({"count", "index"}),
    KIND_INT: frozenset({"as_integer_ratio", "bit_count", "bit_length", "to_bytes"}),
    KIND_FLOAT: frozenset({"as_integer_ratio", "hex", "is_integer"}),
    KIND_DECIMAL: frozenset(
        {
            "adjusted",
            "canonical",
            "copy_abs",
            "copy_negate",
            "copy_sign",
            "is_finite",
            "is_infinite",
            "is_nan",
            "is_normal",
            "is_qnan",
            "is_signed",
            "is_snan",
            "is_subnormal",
            "is_zero",
            "normalize",
            "quantize",
            "sqrt",
            "to_eng_string",
            "to_integral",
            "to_integral_exact",
            "to_integral_value",
        }
    ),
    KIND_DATETIME: frozenset(
        {
            "isoformat",
            "strftime",
            "weekday",
            "isoweekday",
            "isocalendar",
            "toordinal",
            "replace",
            "timestamp",
            "astimezone",
            "utcoffset",
            "dst",
            "tzname",
            "fromutc",
        }
    ),
    KIND_DATE: frozenset(
        {
            "isoformat",
            "strftime",
            "weekday",
            "isoweekday",
            "isocalendar",
            "toordinal",
            "replace",
            "timestamp",
            "ctime",
        }
    ),
    KIND_TIME: frozenset(
        {"isoformat", "strftime", "replace", "utcoffset", "dst", "tzname"}
    ),
    KIND_TIMEDELTA: frozenset({"total_seconds"}),
    KIND_TIMEZONE: frozenset({"utcoffset", "dst", "tzname", "fromutc"}),
    KIND_ZONEINFO: frozenset({"key", "utcoffset", "dst", "tzname", "fromutc"}),
    KIND_STRUCT_TIME: frozenset(
        {
            "tm_year",
            "tm_mon",
            "tm_mday",
            "tm_hour",
            "tm_min",
            "tm_sec",
            "tm_wday",
            "tm_yday",
            "tm_isdst",
        }
    ),
    KIND_REGEX_PATTERN: frozenset(
        {
            "pattern",
            "flags",
            "groups",
            "groupindex",
            "findall",
            "finditer",
            "fullmatch",
            "match",
            "search",
            "split",
            "sub",
            "subn",
        }
    ),
    KIND_REGEX_MATCH: frozenset(
        {
            "group",
            "groups",
            "groupdict",
            "start",
            "end",
            "span",
            "expand",
            "re",
            "string",
            "pos",
            "endpos",
            "lastindex",
            "lastgroup",
        }
    ),
    KIND_DIGEST: frozenset(
        {"update", "digest", "hexdigest", "copy", "name", "digest_size", "block_size"}
    ),
    KIND_HMAC: frozenset(
        {"update", "digest", "hexdigest", "copy", "name", "digest_size", "block_size"}
    ),
    KIND_URL_RESULT: frozenset(
        {
            "scheme",
            "netloc",
            "path",
            "params",
            "query",
            "fragment",
            "username",
            "password",
            "hostname",
            "port",
        }
    ),
    KIND_HEADERS: frozenset({"get", "keys", "values", "items"}),
    KIND_HTTP_RESPONSE: frozenset({"status", "headers", "text", "url", "json"}),
    KIND_EXCEPTION: frozenset({"args"}),
    KIND_CTX: frozenset({"http", "run", "state", "send_text"}),
    KIND_CTX_RUN: frozenset({"job_id", "run_id", "started_at", "timezone"}),
}

# Class-level factory attributes (validated when the attribute is read off a
# class object before calling).
CLASS_MEMBERS: dict[str, frozenset[str]] = {
    "date": frozenset({"today", "fromtimestamp", "fromordinal", "fromisoformat"}),
    "datetime": frozenset(
        {"now", "fromtimestamp", "fromisoformat", "strptime", "combine"}
    ),
    "time": frozenset({"fromisoformat"}),
    "timezone": frozenset({"utc"}),
    "datetime.timedelta": frozenset({"total_seconds"}),
}

# ``from x import y`` members that are values/classes rather than functions.
# The validator uses this only to improve diagnostics; the interpreter applies
# the module registry at runtime.
MODULE_MEMBER_KIND = MODULES

# Field access on primitive container / scalar values used with ``in``,
# ``len`` and iteration is resolved by the interpreter; nothing to declare here.

# ---------------------------------------------------------------------------
# Static call signatures (module member or kind method -> (min, max, kwargs)).
# ``max`` is inclusive; ``kwargs`` names allowed.  A ``max`` of ``None`` means
# unbounded.  Registry entries are the authoritative signature; runtime
# wrappers enforce them.
# ---------------------------------------------------------------------------

_ANY = None

SIGNATURES: dict[tuple[str, str], tuple[int, int | None, frozenset[str]]] = {
    ("builtin", "abs"): (1, 1, frozenset()),
    ("builtin", "all"): (1, 1, frozenset()),
    ("builtin", "any"): (1, 1, frozenset()),
    ("builtin", "bin"): (1, 1, frozenset()),
    ("builtin", "bool"): (0, 1, frozenset()),
    ("builtin", "bytes"): (0, 2, frozenset()),
    ("builtin", "chr"): (1, 1, frozenset()),
    ("builtin", "dict"): (0, 1, frozenset()),
    ("builtin", "divmod"): (2, 2, frozenset()),
    ("builtin", "enumerate"): (1, 2, frozenset()),
    ("builtin", "float"): (0, 1, frozenset()),
    ("builtin", "format"): (1, 2, frozenset()),
    ("builtin", "hex"): (1, 1, frozenset()),
    ("builtin", "int"): (0, 2, frozenset()),
    ("builtin", "isinstance"): (2, 2, frozenset()),
    ("builtin", "len"): (1, 1, frozenset()),
    ("builtin", "list"): (0, 1, frozenset()),
    ("builtin", "max"): (1, _ANY, frozenset()),
    ("builtin", "min"): (1, _ANY, frozenset()),
    ("builtin", "oct"): (1, 1, frozenset()),
    ("builtin", "ord"): (1, 1, frozenset()),
    ("builtin", "pow"): (2, 3, frozenset()),
    ("builtin", "range"): (1, 3, frozenset()),
    ("builtin", "repr"): (1, 1, frozenset()),
    ("builtin", "reversed"): (1, 1, frozenset()),
    ("builtin", "round"): (1, 2, frozenset()),
    ("builtin", "set"): (0, 1, frozenset()),
    ("builtin", "sorted"): (1, 1, frozenset()),
    ("builtin", "str"): (0, 1, frozenset()),
    ("builtin", "sum"): (1, 2, frozenset()),
    ("builtin", "tuple"): (0, 1, frozenset()),
    ("builtin", "zip"): (1, _ANY, frozenset()),
    # ctx
    ("ctx_http", "request"): (
        2,
        2,
        frozenset(
            {
                "params",
                "headers",
                "content",
                "json",
                "data",
                "timeout_seconds",
                "follow_redirects",
                "use_proxy",
            }
        ),
    ),
    ("ctx", "send_text"): (1, 1, frozenset()),
    # json
    ("json", "loads"): (1, 1, frozenset()),
    ("json", "dumps"): (1, 1, frozenset()),
    # re
    ("re", "compile"): (1, 2, frozenset()),
    ("re", "escape"): (1, 1, frozenset()),
    ("re", "fullmatch"): (2, 3, frozenset()),
    ("re", "findall"): (2, 4, frozenset()),
    ("re", "finditer"): (2, 4, frozenset()),
    ("re", "match"): (2, 3, frozenset()),
    ("re", "search"): (2, 3, frozenset()),
    ("re", "split"): (2, 4, frozenset()),
    ("re", "sub"): (3, 4, frozenset()),
    ("re", "subn"): (3, 4, frozenset()),
    # hashlib constructors take one optional data argument plus optional kwargs.
    ("hashlib", "md5"): (0, 1, frozenset()),
    ("hashlib", "sha1"): (0, 1, frozenset()),
    ("hashlib", "sha224"): (0, 1, frozenset()),
    ("hashlib", "sha256"): (0, 1, frozenset()),
    ("hashlib", "sha384"): (0, 1, frozenset()),
    ("hashlib", "sha512"): (0, 1, frozenset()),
    ("hashlib", "sha3_224"): (0, 1, frozenset()),
    ("hashlib", "sha3_256"): (0, 1, frozenset()),
    ("hashlib", "sha3_384"): (0, 1, frozenset()),
    ("hashlib", "sha3_512"): (0, 1, frozenset()),
    ("hashlib", "shake_128"): (0, 1, frozenset()),
    ("hashlib", "shake_256"): (0, 1, frozenset()),
    ("hashlib", "blake2b"): (0, 1, frozenset()),
    ("hashlib", "blake2s"): (0, 1, frozenset()),
    ("hashlib", "new"): (1, 1, frozenset()),
    ("hmac", "new"): (1, 2, frozenset()),
    ("hmac", "digest"): (3, 3, frozenset()),
    ("hmac", "compare_digest"): (2, 2, frozenset()),
    # base64
    ("base64", "b64encode"): (1, 2, frozenset()),
    ("base64", "b64decode"): (1, 2, frozenset()),
    ("base64", "b16encode"): (1, 1, frozenset()),
    ("base64", "b16decode"): (1, 2, frozenset()),
    ("base64", "b32encode"): (1, 1, frozenset()),
    ("base64", "b32decode"): (1, 2, frozenset()),
    ("base64", "b32hexencode"): (1, 1, frozenset()),
    ("base64", "b32hexdecode"): (1, 2, frozenset()),
    ("base64", "b85encode"): (1, 2, frozenset()),
    ("base64", "b85decode"): (1, 2, frozenset()),
    ("base64", "a85encode"): (1, 2, frozenset()),
    ("base64", "a85decode"): (1, 2, frozenset()),
    ("base64", "encodebytes"): (1, 1, frozenset()),
    ("base64", "decodebytes"): (1, 1, frozenset()),
    # urllib.parse
    ("urllib.parse", "urlparse"): (1, 2, frozenset()),
    ("urllib.parse", "urlsplit"): (1, 2, frozenset()),
    ("urllib.parse", "urlunparse"): (1, 1, frozenset()),
    ("urllib.parse", "urlunsplit"): (1, 1, frozenset()),
    ("urllib.parse", "urljoin"): (2, 2, frozenset()),
    ("urllib.parse", "urldefrag"): (1, 1, frozenset()),
    ("urllib.parse", "urlencode"): (1, 2, frozenset()),
    ("urllib.parse", "parse_qs"): (1, 2, frozenset()),
    ("urllib.parse", "parse_qsl"): (1, 2, frozenset()),
    ("urllib.parse", "quote"): (1, 2, frozenset()),
    ("urllib.parse", "quote_plus"): (1, 2, frozenset()),
    ("urllib.parse", "quote_from_bytes"): (1, 2, frozenset()),
    ("urllib.parse", "unquote"): (1, 2, frozenset()),
    ("urllib.parse", "unquote_plus"): (1, 2, frozenset()),
    ("urllib.parse", "unquote_to_bytes"): (1, 1, frozenset()),
}


def allowed_signature(
    key: tuple[str, str],
) -> tuple[int, int | None, frozenset[str]] | None:
    """Return the registered signature for a callable key or ``None``."""
    return SIGNATURES.get(key)


# ---------------------------------------------------------------------------
# Module documentation text (generated documentation is kept in sync by tests)
# ---------------------------------------------------------------------------

_DOC_HEADER = """# astrbot-python-subset/v1 reference

`astrbot-python-subset` is a restricted Python dialect executed by an
AST interpreter in an isolated one-shot worker process.  It is **not**
CPython: there is no `exec`/`eval`, no native module imports beyond the
registry below, and no access to AstrBot internals.
"""


def _module_doc() -> str:
    lines = ["## Allowed modules"]
    for module in sorted(MODULES):
        members = ", ".join(sorted(MODULES[module]))
        lines.append(f"- `{module}`: {members}")
    return "\n".join(lines)


def _methods_doc() -> str:
    lines = ["## Allowed methods by value type"]
    for kind in sorted(METHODS):
        members = ", ".join(sorted(METHODS[kind]))
        lines.append(f"- `{kind}`: {members}")
    return "\n".join(lines)


def build_reference_doc() -> str:
    """Render the complete language reference used by the human docs."""
    sections = [
        _DOC_HEADER,
        _module_doc(),
        _methods_doc(),
    ]
    return "\n\n".join(sections) + "\n"


def build_compact_contract() -> str:
    """Return the short contract injected into the LLM Tool description."""
    module_list = ", ".join(sorted(MODULES))
    return (
        "astrbot-python-subset/v1: a restricted Python dialect run by an "
        "AST interpreter. The script body is the task itself: `ctx` is "
        "pre-bound at module top level. Use `await ctx.http.request(method, url, "
        "params=None, headers=None, content=None, json=None, data=None, "
        "timeout_seconds=None, follow_redirects=False, use_proxy=True)` for HTTP, "
        "`await ctx.send_text(text)` to send to the task's bound session, "
        "`ctx.state` for persistent JSON state, and `ctx.run` (job_id/run_id/"
        "started_at/timezone) for run metadata. Top-level `await` is allowed; "
        "helper functions must be defined at module top level and awaited "
        "directly. Allowed imports: " + module_list + ". "
        "No native Python execution, files, AstrBot tools, or arbitrary send "
        "targets. Example:\n"
        "```python\n"
        'response = await ctx.http.request("GET", "https://example.com/price", '
        "use_proxy=False)\n"
        'price = response.json()["price"]\n'
        "if price < 3900:\n"
        '    await ctx.send_text(f"Gold below 3900: {price}")\n'
        "```"
    )


__all__ = [
    "ALLOWED_BUILTINS",
    "ALLOWED_EXCEPTIONS",
    "ALLOWED_EXCEPTION_ALIASES",
    "ALLOWED_EXPRESSIONS",
    "ALLOWED_OPERATORS",
    "ALLOWED_STATEMENTS",
    "CLASS_MEMBERS",
    "DEFAULT_LANGUAGE_VERSION",
    "DEFAULT_LIMITS",
    "FEATURE_VERSION",
    "FEATURE_VERSION_INT",
    "FORBIDDEN_BUILTINS",
    "FORBIDDEN_MODULE_MEMBERS",
    "LANGUAGE_VERSION",
    "LIMIT_KEYS",
    "METHODS",
    "MODULES",
    "MODULE_NAMES",
    "SIGNATURES",
    "SUPPORTED_LANGUAGE_VERSIONS",
    "allowed_signature",
    "build_compact_contract",
    "build_reference_doc",
    "coerce_limits",
]
