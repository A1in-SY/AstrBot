"""Trusted runtime dispatch tables for the script interpreter.

Every member/method/builtin exposed to scripts is resolved through the
dispatch tables below.  The script can never call ``getattr`` on an arbitrary
Python object; the interpreter only calls these explicit wrappers.
"""

from __future__ import annotations

import base64
import datetime as _datetime
import hashlib
import hmac
import json as _json
import math
import re
import time as _time
import urllib.parse
import zoneinfo
from decimal import Decimal
from typing import Any

from astrbot.script_runtime import spec
from astrbot.script_runtime.errors import (
    HttpInvalidRequestError,
    SendTargetUnavailableError,
)
from astrbot.script_runtime.state import AtomicState, validate_json_value
from astrbot.script_runtime.values import SafeValue

# ---------------------------------------------------------------------------
# State-aware containers
# ---------------------------------------------------------------------------


class _StateContainerSnapshot:
    """A rollback snapshot that retains every pre-mutation container object."""

    def __init__(self, container: Any, children: dict[Any, Any] | list[Any]) -> None:
        self.container = container
        self.children = children


class _StateGraph:
    """A shared transaction context for every container in the state tree."""

    def __init__(self, atomic: AtomicState) -> None:
        self.atomic = atomic
        self.root = self._wrap_dict(atomic.snapshot())

    def _wrap_dict(self, value: dict[Any, Any]) -> StateDict:
        return StateDict(
            self,
            {key: self.wrap(item) for key, item in value.items()},
        )

    def _wrap_list(self, value: list[Any]) -> StateList:
        return StateList(self, [self.wrap(item) for item in value])

    def wrap(self, value: Any) -> Any:
        if isinstance(value, (StateDict, StateList)):
            value = self.to_plain(value)
        if isinstance(value, dict):
            return self._wrap_dict(value)
        if isinstance(value, list):
            return self._wrap_list(value)
        return value

    def to_plain(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {key: self.to_plain(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.to_plain(item) for item in value]
        return value

    def mutate(self, mutation) -> Any:
        """Apply one mutation and roll the live graph back if validation fails."""
        before = self._capture(self.root)
        try:
            result = mutation()
            self.atomic.commit(self.to_plain(self.root))
            return result
        except Exception:
            self._restore(before)
            raise

    def _capture(self, value: Any) -> Any:
        if isinstance(value, StateDict):
            return _StateContainerSnapshot(
                value,
                {key: self._capture(item) for key, item in value.items()},
            )
        if isinstance(value, StateList):
            return _StateContainerSnapshot(
                value,
                [self._capture(item) for item in value],
            )
        return value

    def _restore(self, snapshot: Any) -> Any:
        if not isinstance(snapshot, _StateContainerSnapshot):
            return snapshot
        target = snapshot.container
        if isinstance(target, StateDict) and isinstance(snapshot.children, dict):
            dict.clear(target)
            for key, child in snapshot.children.items():
                dict.__setitem__(target, key, self._restore(child))
            return target
        if isinstance(target, StateList) and isinstance(snapshot.children, list):
            list.clear(target)
            list.extend(target, [self._restore(child) for child in snapshot.children])
            return target
        raise TypeError("state rollback snapshot does not match its container")


class StateDict(dict):
    """A dict whose mutations commit the complete shared root state."""

    def __init__(self, graph: _StateGraph, data: dict[Any, Any]) -> None:
        dict.__init__(self, data)
        self._graph = graph

    def __setitem__(self, key: Any, value: Any) -> None:
        self._graph.mutate(lambda: dict.__setitem__(self, key, self._graph.wrap(value)))

    def __delitem__(self, key: Any) -> None:
        self._graph.mutate(lambda: dict.__delitem__(self, key))

    def pop(self, *args: Any) -> Any:
        return self._graph.mutate(lambda: dict.pop(self, *args))

    def popitem(self) -> Any:
        return self._graph.mutate(lambda: dict.popitem(self))

    def clear(self) -> None:
        self._graph.mutate(lambda: dict.clear(self))

    def update(self, *args: Any, **kwargs: Any) -> None:
        updates = dict(*args, **kwargs)

        def apply() -> None:
            for key, value in updates.items():
                dict.__setitem__(self, key, self._graph.wrap(value))

        self._graph.mutate(apply)

    def setdefault(self, key: Any, default: Any = None) -> Any:
        if key in self:
            return dict.__getitem__(self, key)

        def apply() -> Any:
            value = self._graph.wrap(default)
            dict.__setitem__(self, key, value)
            return value

        return self._graph.mutate(apply)


class StateList(list):
    """A list whose mutations commit the complete shared root state."""

    def __init__(self, graph: _StateGraph, data: list[Any]) -> None:
        list.__init__(self, data)
        self._graph = graph

    def __setitem__(self, key: Any, value: Any) -> None:
        converted = (
            [self._graph.wrap(item) for item in value]
            if isinstance(key, slice)
            else self._graph.wrap(value)
        )
        self._graph.mutate(lambda: list.__setitem__(self, key, converted))

    def __delitem__(self, key: Any) -> None:
        self._graph.mutate(lambda: list.__delitem__(self, key))

    def append(self, value: Any) -> None:
        self._graph.mutate(lambda: list.append(self, self._graph.wrap(value)))

    def extend(self, values: Any) -> None:
        converted = [self._graph.wrap(value) for value in values]
        self._graph.mutate(lambda: list.extend(self, converted))

    def insert(self, index: int, value: Any) -> None:
        self._graph.mutate(lambda: list.insert(self, index, self._graph.wrap(value)))

    def pop(self, *args: Any) -> Any:
        return self._graph.mutate(lambda: list.pop(self, *args))

    def remove(self, value: Any) -> None:
        self._graph.mutate(lambda: list.remove(self, value))

    def clear(self) -> None:
        self._graph.mutate(lambda: list.clear(self))

    def reverse(self) -> None:
        self._graph.mutate(lambda: list.reverse(self))

    def sort(self, *args: Any, **kwargs: Any) -> None:
        if kwargs:
            raise TypeError("sort() does not accept keyword arguments")
        self._graph.mutate(lambda: list.sort(self, *args))


def build_state_dict(root: AtomicState) -> StateDict:
    return _StateGraph(root).root


# ---------------------------------------------------------------------------
# Value conversion helpers
# ---------------------------------------------------------------------------


def unwrap(sv: SafeValue) -> Any:
    return sv.value


def wrap(value: Any) -> SafeValue:
    return SafeValue.from_python(value)


def _json_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _require_str(sv: SafeValue, what: str) -> str:
    if sv.kind != spec.KIND_STR:
        raise TypeError(f"{what} must be a string")
    return sv.value


def _require_int(sv: SafeValue, what: str) -> int:
    if sv.kind != spec.KIND_INT:
        raise TypeError(f"{what} must be an integer")
    return sv.value


def _require_number(sv: SafeValue, what: str) -> int | float | Decimal:
    if sv.kind not in (spec.KIND_INT, spec.KIND_FLOAT, spec.KIND_DECIMAL):
        raise TypeError(f"{what} must be a number")
    return sv.value


def _require_bool(sv: SafeValue, what: str) -> bool:
    if sv.kind != spec.KIND_BOOL:
        raise TypeError(f"{what} must be a bool")
    return sv.value


def _to_plain(value: SafeValue | Any) -> Any:
    if isinstance(value, SafeValue):
        return unwrap(value)
    return value


def _unwrap_list(values: list[SafeValue]) -> list[Any]:
    return [_to_plain(value) for value in values]


# ---------------------------------------------------------------------------
# Primitive helpers
# ---------------------------------------------------------------------------


def _safe_repr(value: Any) -> str:
    """A safe repr that never leaks host type paths or addresses."""
    if value is None:
        return "None"
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, str):
        return repr(value)
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, bytes):
        return repr(value)
    if isinstance(value, Decimal):
        return repr(value)
    if isinstance(value, (list, tuple, set)):
        inner = ", ".join(_safe_repr(item) for item in value)
        if isinstance(value, list):
            return f"[{inner}]"
        if isinstance(value, tuple):
            if len(value) == 1:
                return f"({_safe_repr(value[0])},)"
            return f"({inner})"
        return f"{{{inner}}}"
    if isinstance(value, dict):
        inner = ", ".join(f"{_safe_repr(k)}: {_safe_repr(v)}" for k, v in value.items())
        return f"{{{inner}}}"
    if isinstance(value, (_datetime.date, _datetime.time, _datetime.timedelta)):
        return str(value)
    if isinstance(value, zoneinfo.ZoneInfo):
        return f"ZoneInfo({value.key!r})"
    if isinstance(value, _time.struct_time):
        return str(value)
    if isinstance(value, Exception):
        return f"{type(value).__name__}: {value}"
    if isinstance(value, re.Pattern):
        return value.pattern
    if isinstance(value, re.Match):
        return f"<re.Match object; span={value.span()}, match={value.group()!r}>"
    if hasattr(value, "__class__") and type(value).__module__.startswith(
        "astrbot.script_runtime"
    ):
        return str(value)
    return f"<{type(value).__name__}>"


def _format_plain(value: Any, format_spec: str) -> str:
    if value is None:
        return "None"
    if isinstance(value, (str, int, float, Decimal)):
        return format(value, format_spec)
    if isinstance(value, (_datetime.date, _datetime.time, _datetime.timedelta)):
        if not format_spec:
            return str(value)
        return value.strftime(format_spec)
    return format(value, format_spec)


# ---------------------------------------------------------------------------
# Dispatch tables
# ---------------------------------------------------------------------------


class Stdlib:
    """All safe runtime operations for the interpreter."""

    def __init__(
        self,
        *,
        http_client: Any = None,
        send_text: Any = None,
        run_facade: Any = None,
        state: AtomicState | None = None,
    ) -> None:
        self.http_client = http_client
        self.send_text = send_text
        self.run_facade = run_facade
        self.state_root = state
        self._state_dict: StateDict | None = None

    # ------------------------------------------------------------------
    # ctx
    # ------------------------------------------------------------------

    def get_ctx_attr(self, name: str) -> SafeValue:
        if name == "http":
            return SafeValue(spec.KIND_CTX_HTTP, self.http_client)
        if name == "run":
            return SafeValue(spec.KIND_CTX_RUN, self.run_facade)
        if name == "state":
            if self._state_dict is None:
                self._state_dict = build_state_dict(self.state_root)
            return SafeValue(spec.KIND_STATE, self._state_dict)
        if name == "send_text":
            return SafeValue(spec.KIND_SEND_TEXT, self.send_text)
        raise AttributeError(f"ctx.{name} is not available")

    def get_ctx_run_attr(self, name: str) -> SafeValue:
        if self.run_facade is None:
            raise AttributeError("ctx.run is not available")
        if name not in ("job_id", "run_id", "started_at", "timezone"):
            raise AttributeError(f"ctx.run.{name} is not available")
        value = getattr(self.run_facade, name)
        if name == "started_at":
            return SafeValue(spec.KIND_DATETIME, value)
        return wrap(value)

    # ------------------------------------------------------------------
    # Modules
    # ------------------------------------------------------------------

    def get_module_member(self, module: str, member: str) -> SafeValue:
        allowed = spec.MODULES.get(module, {})
        if member not in allowed:
            forbidden = spec.FORBIDDEN_MODULE_MEMBERS.get(module, frozenset())
            if member in forbidden:
                raise AttributeError(f"{module}.{member} is explicitly not allowed")
            raise AttributeError(f"{module}.{member} is not available")
        kind = allowed[member]
        if kind == "const":
            return wrap(_MODULE_CONSTS[(module, member)])
        if kind == "class":
            return SafeValue(spec.KIND_CLASS, (module, member))
        return SafeValue(spec.KIND_CALLABLE, ("module", module, member))

    # ------------------------------------------------------------------
    # Calls
    # ------------------------------------------------------------------

    async def call_callable(
        self, callee: SafeValue, args: list[SafeValue], kwargs: dict[str, SafeValue]
    ) -> SafeValue:
        if callee.kind == spec.KIND_CALLABLE:
            tag = callee.value
            if isinstance(tag, tuple) and len(tag) == 3 and tag[0] == "module":
                _, module, member = tag
                return self.call_module_member(module, member, args, kwargs)
            if isinstance(tag, tuple) and len(tag) == 2 and tag[0] == "method":
                _, (kind, value, method) = tag
                return self.call_method(kind, value, method, args, kwargs)
            if isinstance(tag, tuple) and len(tag) == 2 and tag[0] == "class_member":
                _, (class_name, member) = tag
                return self.call_class_member(class_name, member, args, kwargs)
            if isinstance(tag, tuple) and len(tag) == 2 and tag[0] == "builtin":
                return self.call_builtin(tag[1], args, kwargs)
            if isinstance(tag, tuple) and len(tag) == 2 and tag[0] == "response_json":
                return self.call_response_json(tag[1], args, kwargs)
            if isinstance(tag, tuple) and len(tag) == 2 and tag[0] == "ctx_http":
                if self.http_client is None:
                    raise HttpInvalidRequestError("ctx.http is not available")
                return await self.http_client.request(args, kwargs)
        if callee.kind == spec.KIND_CLASS:
            module, member = callee.value
            if module == "script_exc":
                import astrbot.script_runtime.errors as errors

                exc_cls = errors.BUILTIN_EXCEPTIONS.get(member) or getattr(
                    errors, member, None
                )
                if exc_cls is None:
                    raise TypeError(f"unknown exception {member!r}")
                plain_args = _unwrap_list(args)
                return SafeValue(spec.KIND_EXCEPTION, exc_cls(*plain_args))
            return self.call_class_constructor(module, member, args, kwargs)
        if callee.kind == spec.KIND_REQUEST:
            if self.http_client is None:
                raise HttpInvalidRequestError("ctx.http is not available")
            return await self.http_client.request(args, kwargs)
        if callee.kind == spec.KIND_SEND_TEXT:
            if self.send_text is None:
                raise SendTargetUnavailableError("ctx.send_text is not available")
            return await self.send_text(args, kwargs)
        if callee.kind in (spec.KIND_USER_SYNC_FUNC, spec.KIND_USER_ASYNC_FUNC):
            return await callee.value(args, kwargs)
        raise TypeError("This value cannot be called")

    def call_module_member(
        self,
        module: str,
        member: str,
        args: list[SafeValue],
        kwargs: dict[str, SafeValue],
    ) -> SafeValue:
        key = (module, member)
        if key == ("datetime", "date"):
            return self.call_class_constructor("datetime", "date", args, kwargs)
        if key == ("datetime", "datetime"):
            return self.call_class_constructor("datetime", "datetime", args, kwargs)
        if key == ("datetime", "time"):
            return self.call_class_constructor("datetime", "time", args, kwargs)
        if key == ("datetime", "timedelta"):
            return self.call_class_constructor("datetime", "timedelta", args, kwargs)
        if key == ("datetime", "timezone"):
            return self.call_class_constructor("datetime", "timezone", args, kwargs)
        if key == ("zoneinfo", "ZoneInfo"):
            return self.call_class_constructor("zoneinfo", "ZoneInfo", args, kwargs)
        if key == ("decimal", "Decimal"):
            return self.call_class_constructor("decimal", "Decimal", args, kwargs)
        if key == ("json", "loads"):
            return self._json_loads(args, kwargs)
        if key == ("json", "dumps"):
            return self._json_dumps(args, kwargs)
        if module == "time":
            return self._time_module(member, args, kwargs)
        if module == "math":
            return self._math_module(member, args, kwargs)
        if module == "re":
            return self._re_module(member, args, kwargs)
        if module == "base64":
            return self._base64_module(member, args, kwargs)
        if module == "hashlib":
            return self._hashlib_module(member, args, kwargs)
        if module == "hmac":
            return self._hmac_module(member, args, kwargs)
        if module == "urllib.parse":
            return self._urlparse_module(member, args, kwargs)
        raise AttributeError(f"{module}.{member} is not callable")

    def call_class_constructor(
        self,
        module: str,
        member: str,
        args: list[SafeValue],
        kwargs: dict[str, SafeValue],
    ) -> SafeValue:
        if kwargs:
            if member == "datetime" and set(kwargs) - {"tzinfo", "fold"}:
                raise TypeError("datetime() got unexpected keyword arguments")
            if member == "time" and set(kwargs) - {"tzinfo", "fold"}:
                raise TypeError("time() got unexpected keyword arguments")
            if member == "timezone" and set(kwargs) - {"offset", "name"}:
                raise TypeError("timezone() got unexpected keyword arguments")
            if member == "timedelta" and set(kwargs) - {
                "days",
                "seconds",
                "microseconds",
                "milliseconds",
                "minutes",
                "hours",
                "weeks",
            }:
                raise TypeError("timedelta() got unexpected keyword arguments")
        plain = _unwrap_list(args)
        if (module, member) == ("datetime", "date"):
            return wrap(_datetime.date(*plain))
        if (module, member) == ("datetime", "datetime"):
            return wrap(_datetime.datetime(*plain, **kwargs))
        if (module, member) == ("datetime", "time"):
            return wrap(_datetime.time(*plain, **kwargs))
        if (module, member) == ("datetime", "timedelta"):
            return wrap(_datetime.timedelta(*plain, **kwargs))
        if (module, member) == ("datetime", "timezone"):
            return wrap(_datetime.timezone(*plain, **kwargs))
        if (module, member) == ("zoneinfo", "ZoneInfo"):
            if len(plain) != 1 or not isinstance(plain[0], str):
                raise TypeError("ZoneInfo() requires a string key")
            return wrap(zoneinfo.ZoneInfo(plain[0]))
        if (module, member) == ("decimal", "Decimal"):
            if len(plain) != 1:
                raise TypeError("Decimal() requires one argument")
            return wrap(
                Decimal(
                    str(plain[0]) if not isinstance(plain[0], Decimal) else plain[0]
                )
            )
        raise AttributeError(f"{module}.{member} is not a constructor")

    def call_class_member(
        self,
        class_name: str,
        member: str,
        args: list[SafeValue],
        kwargs: dict[str, SafeValue],
    ) -> SafeValue:
        plain = _unwrap_list(args)
        if kwargs:
            if member == "strptime" and set(kwargs) - {"format"}:
                raise TypeError("strptime() got unexpected keyword arguments")
            if member == "now" and set(kwargs) - {"tz"}:
                raise TypeError("now() got unexpected keyword arguments")
            if member == "fromisoformat" and set(kwargs) - {"sep"}:
                raise TypeError("fromisoformat() got unexpected keyword arguments")
        if class_name == "date":
            if member == "today":
                return wrap(_datetime.date.today())
            if member == "fromtimestamp":
                return wrap(_datetime.date.fromtimestamp(*plain))
            if member == "fromordinal":
                return wrap(_datetime.date.fromordinal(*plain))
            if member == "fromisoformat":
                return wrap(_datetime.date.fromisoformat(*plain))
        if class_name == "datetime":
            if member == "now":
                return wrap(_datetime.datetime.now(*plain))
            if member == "fromtimestamp":
                return wrap(_datetime.datetime.fromtimestamp(*plain, **kwargs))
            if member == "fromisoformat":
                return wrap(_datetime.datetime.fromisoformat(*plain, **kwargs))
            if member == "strptime":
                return wrap(_datetime.datetime.strptime(*plain))
            if member == "combine":
                return wrap(_datetime.datetime.combine(*plain))
        if class_name == "time":
            if member == "fromisoformat":
                return wrap(_datetime.time.fromisoformat(*plain))
        if class_name == "timezone" and member == "utc":
            return wrap(_datetime.timezone.utc)
        raise AttributeError(f"{class_name}.{member} is not callable")

    def call_response_json(
        self, response: Any, args: list[SafeValue], kwargs: dict[str, SafeValue]
    ) -> SafeValue:
        if args or kwargs:
            raise TypeError("response.json() takes no arguments")
        from astrbot.script_runtime.http import response_json

        return response_json(response)

    # ------------------------------------------------------------------
    # Builtins
    # ------------------------------------------------------------------

    def call_builtin(
        self, name: str, args: list[SafeValue], kwargs: dict[str, SafeValue]
    ) -> SafeValue:
        if kwargs:
            raise TypeError(f"{name}() does not accept keyword arguments")
        plain = _unwrap_list(args)
        if name == "abs":
            return wrap(abs(_require_number(args[0], "abs")))
        if name == "all":
            return wrap(all(item.truthy() for item in self._iterate(args[0])))
        if name == "any":
            return wrap(any(item.truthy() for item in self._iterate(args[0])))
        if name == "bin":
            return wrap(bin(_require_int(args[0], "bin")))
        if name == "bool":
            return wrap(bool(args[0].truthy()) if args else False)
        if name == "bytes":
            if not args:
                return wrap(b"")
            if args[0].kind == spec.KIND_STR:
                return wrap(args[0].value.encode("utf-8"))
            return wrap(
                bytes(plain[0])
                if isinstance(plain[0], (bytes, list, tuple, range))
                else bytes(plain[0])
            )
        if name == "chr":
            return wrap(chr(_require_int(args[0], "chr")))
        if name == "dict":
            if not args:
                return wrap({})
            if args[0].kind in (spec.KIND_DICT, spec.KIND_STATE):
                return wrap(dict(args[0].value))
            return wrap(dict(self._pairs(args[0])))
        if name == "divmod":
            return wrap(divmod(plain[0], plain[1]))
        if name == "enumerate":
            items = list(self._iterate(args[0]))
            start = plain[1] if len(args) > 1 else 0
            return wrap([(i, item.value) for i, item in enumerate(items, start)])
        if name == "float":
            if not args:
                return wrap(0.0)
            if args[0].kind in (
                spec.KIND_INT,
                spec.KIND_FLOAT,
                spec.KIND_DECIMAL,
                spec.KIND_STR,
            ):
                return wrap(float(plain[0]))
            raise TypeError("float() argument must be a number or string")
        if name == "format":
            spec_text = _require_str(args[1], "format spec") if len(args) > 1 else ""
            return wrap(_format_plain(plain[0], spec_text))
        if name == "hex":
            return wrap(hex(_require_int(args[0], "hex")))
        if name == "int":
            if not args:
                return wrap(0)
            if args[0].kind in (
                spec.KIND_INT,
                spec.KIND_FLOAT,
                spec.KIND_DECIMAL,
                spec.KIND_STR,
            ):
                base = plain[1] if len(args) > 1 else None
                return wrap(int(plain[0], base) if base is not None else int(plain[0]))
            raise TypeError("int() argument must be a number or string")
        if name == "isinstance":
            type_names = plain[1]
            names = type_names if isinstance(type_names, tuple) else (type_names,)
            allowed_names = {
                "int": (spec.KIND_INT,),
                "float": (spec.KIND_FLOAT,),
                "bool": (spec.KIND_BOOL,),
                "str": (spec.KIND_STR,),
                "bytes": (spec.KIND_BYTES,),
                "list": (spec.KIND_LIST,),
                "tuple": (spec.KIND_TUPLE,),
                "set": (spec.KIND_SET,),
                "dict": (spec.KIND_DICT,),
                "Decimal": (spec.KIND_DECIMAL,),
            }
            matched = False
            for name in names:
                if not isinstance(name, str) or name not in allowed_names:
                    raise TypeError("isinstance() type must be a supported type token")
                if args[0].kind in allowed_names[name]:
                    matched = True
            return wrap(matched)
        if name == "len":
            try:
                return wrap(len(plain[0]))
            except TypeError:
                raise TypeError("len() argument has no len()") from None
        if name == "list":
            if not args:
                return wrap([])
            return wrap([item.value for item in self._iterate(args[0])])
        if name in ("max", "min"):
            if not args:
                raise TypeError(f"{name}() needs at least one argument")
            if len(args) == 1:
                items = [item.value for item in self._iterate(args[0])]
                return wrap(max(items) if name == "max" else min(items))
            return wrap(max(plain) if name == "max" else min(plain))
        if name == "oct":
            return wrap(oct(_require_int(args[0], "oct")))
        if name == "ord":
            return wrap(ord(_require_str(args[0], "ord")))
        if name == "pow":
            return wrap(pow(*plain))
        if name == "range":
            return wrap(range(*plain))
        if name == "repr":
            return wrap(_safe_repr(plain[0]))
        if name == "reversed":
            items = [item.value for item in self._iterate(args[0])]
            return wrap(list(reversed(items)))
        if name == "round":
            return wrap(round(plain[0], plain[1]) if len(args) > 1 else round(plain[0]))
        if name == "set":
            if not args:
                return wrap(set())
            return wrap({item.value for item in self._iterate(args[0])})
        if name == "sorted":
            items = [item.value for item in self._iterate(args[0])]
            return wrap(sorted(items))
        if name == "str":
            if not args:
                return wrap("")
            if isinstance(plain[0], Exception):
                return wrap(str(plain[0]))
            return wrap(
                _safe_repr(plain[0])
                if not isinstance(
                    plain[0], (str, int, float, bool, Decimal, type(None))
                )
                else str(plain[0])
            )
        if name == "sum":
            items = [item.value for item in self._iterate(args[0])]
            start = plain[1] if len(args) > 1 else 0
            return wrap(sum(items, start))
        if name == "tuple":
            if not args:
                return wrap(())
            return wrap(tuple(item.value for item in self._iterate(args[0])))
        if name == "zip":
            iterables = [list(self._iterate(arg)) for arg in args]
            return wrap([tuple(items) for items in zip(*iterables)])
        raise TypeError(f"{name} is not a supported builtin")

    # ------------------------------------------------------------------
    # Methods
    # ------------------------------------------------------------------

    def call_method(
        self,
        kind: str,
        value: Any,
        method: str,
        args: list[SafeValue],
        kwargs: dict[str, SafeValue],
    ) -> SafeValue:
        if kwargs:
            raise TypeError(f"{method}() does not accept keyword arguments")
        plain = _unwrap_list(args)
        try:
            result = _METHOD_DISPATCH[kind][method](value, plain)
        except KeyError:
            raise AttributeError(
                f"{method} is not available on {kind} values"
            ) from None
        return wrap(result)

    # ------------------------------------------------------------------
    # Iteration and containers
    # ------------------------------------------------------------------

    def _iterate(self, sv: SafeValue) -> list[SafeValue]:
        value = sv.value
        if sv.kind == spec.KIND_DICT:
            items = list(value.keys())
        elif sv.kind == spec.KIND_STATE:
            items = list(value.keys())
        elif sv.kind == spec.KIND_HEADERS:
            items = list(value.keys())
        elif sv.kind in (
            spec.KIND_LIST,
            spec.KIND_TUPLE,
            spec.KIND_SET,
            spec.KIND_STR,
            spec.KIND_BYTES,
            spec.KIND_RANGE,
        ):
            items = list(value)
        elif sv.kind == spec.KIND_REGEX_MATCH:
            items = list(value)
        else:
            try:
                items = list(value)
            except TypeError:
                raise TypeError("this value cannot be iterated") from None
        return [wrap(item) for item in items]

    def _pairs(self, sv: SafeValue) -> list[tuple[Any, Any]]:
        items = self._iterate(sv)
        pairs: list[tuple[Any, Any]] = []
        for item in items:
            if (
                item.kind not in (spec.KIND_TUPLE, spec.KIND_LIST)
                or len(item.value) != 2
            ):
                raise ValueError("dict() requires an iterable of key/value pairs")
            pairs.append((item.value[0], item.value[1]))
        return pairs

    def get_item(self, sv: SafeValue, key: SafeValue) -> SafeValue:
        if sv.kind == spec.KIND_STATE:
            return wrap(sv.value[key.value])
        if sv.kind == spec.KIND_DICT:
            return wrap(sv.value[key.value])
        if sv.kind == spec.KIND_HEADERS:
            return wrap(sv.value[key.value])
        if sv.kind == spec.KIND_STRUCT_TIME:
            return wrap(sv.value[key.value])
        if sv.kind in (
            spec.KIND_LIST,
            spec.KIND_TUPLE,
            spec.KIND_STR,
            spec.KIND_BYTES,
            spec.KIND_RANGE,
        ):
            return wrap(sv.value[key.value])
        raise TypeError("this value cannot be subscripted")

    def set_item(self, sv: SafeValue, key: SafeValue, value: SafeValue) -> None:
        if sv.kind == spec.KIND_STATE:
            _state_write(sv.value, key.value, value.value)
            return
        if sv.kind == spec.KIND_DICT:
            sv.value[key.value] = value.value
            return
        if sv.kind == spec.KIND_LIST:
            sv.value[key.value] = value.value
            return
        raise TypeError("this value does not support item assignment")

    def contains(self, sv: SafeValue, item: SafeValue) -> bool:
        try:
            return item.value in sv.value
        except TypeError:
            return False

    def length(self, sv: SafeValue) -> int:
        return len(sv.value)

    def delete_item(self, sv: SafeValue, key: SafeValue) -> None:
        if sv.kind == spec.KIND_STATE:
            _state_delete(sv.value, key.value)
            return
        if sv.kind == spec.KIND_DICT:
            del sv.value[key.value]
            return
        if sv.kind == spec.KIND_LIST:
            del sv.value[key.value]
            return
        raise TypeError("this value does not support item deletion")

    # ------------------------------------------------------------------
    # Attribute access
    # ------------------------------------------------------------------

    def get_attr(self, sv: SafeValue, name: str) -> SafeValue:
        kind = sv.kind
        if kind == spec.KIND_MODULE:
            return self.get_module_member(sv.value, name)
        if kind == spec.KIND_CLASS:
            module, member = sv.value
            if member == "timezone" and name == "utc":
                return SafeValue(spec.KIND_TIMEZONE, _datetime.timezone.utc)
            if member in (
                "date",
                "datetime",
                "time",
            ) and name in spec.CLASS_MEMBERS.get(member, frozenset()):
                return SafeValue(spec.KIND_CALLABLE, ("class_member", (member, name)))
            raise AttributeError(f"{member}.{name} is not available")
        if kind == spec.KIND_CTX:
            return self.get_ctx_attr(name)
        if kind == spec.KIND_CTX_HTTP:
            if name != "request":
                raise AttributeError("ctx.http.request is the only http capability")
            return SafeValue(spec.KIND_CALLABLE, ("ctx_http", "request"))
        if kind == spec.KIND_CTX_RUN:
            return self.get_ctx_run_attr(name)
        if kind == spec.KIND_CTX_STATE:
            return SafeValue(
                spec.KIND_STATE, self._state_dict or build_state_dict(self.state_root)
            )
        if kind == spec.KIND_HTTP_RESPONSE:
            return self._response_attr(sv.value, name)
        if kind == spec.KIND_HEADERS:
            return self._headers_attr(sv.value, name)
        if kind in (spec.KIND_DIGEST, spec.KIND_HMAC):
            return self._digest_attr(sv.value, name)
        if kind == spec.KIND_REGEX_PATTERN:
            return self._pattern_attr(sv.value, name)
        if kind == spec.KIND_REGEX_MATCH:
            return self._match_attr(sv.value, name)
        if kind == spec.KIND_URL_RESULT:
            if name not in spec.METHODS[spec.KIND_URL_RESULT]:
                raise AttributeError(f"{name} is not available on a parsed URL")
            value = getattr(sv.value, name)
            return wrap(value if name != "port" or value is not None else None)
        if kind == spec.KIND_STRUCT_TIME:
            if name not in spec.METHODS[spec.KIND_STRUCT_TIME]:
                raise AttributeError(f"{name} is not available on struct_time")
            return wrap(getattr(sv.value, name))
        if kind == spec.KIND_EXCEPTION:
            if name != "args":
                raise AttributeError("only exc.args is available")
            return wrap(sv.value.args)
        if kind == spec.KIND_STATE:
            if name not in spec.METHODS[spec.KIND_STATE]:
                raise AttributeError(f"{name} is not available on state")
            return SafeValue(
                spec.KIND_CALLABLE, ("method", (spec.KIND_STATE, sv.value, name))
            )
        if kind in spec.METHODS:
            if name not in spec.METHODS[kind]:
                raise AttributeError(f"{name} is not available on {kind} values")
            return SafeValue(spec.KIND_CALLABLE, ("method", (kind, sv.value, name)))
        # Unknown safe value: allow any name in the global method union.
        union = set()
        for members in spec.METHODS.values():
            union.update(members)
        if name in union:
            return SafeValue(
                spec.KIND_CALLABLE, ("method", (spec.KIND_UNKNOWN, sv.value, name))
            )
        raise AttributeError(f"{name} is not a known safe member")

    def _response_attr(self, response: Any, name: str) -> SafeValue:
        if name not in spec.METHODS[spec.KIND_HTTP_RESPONSE]:
            raise AttributeError(f"response.{name} is not available")
        if name == "json":
            return SafeValue(spec.KIND_CALLABLE, ("response_json", response))
        if name == "status":
            return wrap(response.status)
        if name == "text":
            return wrap(response.text)
        if name == "url":
            return wrap(response.url)
        if name == "headers":
            return SafeValue(spec.KIND_HEADERS, response.headers)
        raise AttributeError(f"response.{name} is not available")

    def _headers_attr(self, headers: Any, name: str) -> SafeValue:
        if name not in spec.METHODS[spec.KIND_HEADERS]:
            raise AttributeError(f"headers.{name} is not available")
        return SafeValue(
            spec.KIND_CALLABLE, ("method", (spec.KIND_HEADERS, headers, name))
        )

    def _digest_attr(self, digest: Any, name: str) -> SafeValue:
        allowed = spec.METHODS[spec.KIND_DIGEST]
        if name not in allowed:
            raise AttributeError(f"{name} is not available on a digest")
        if name in ("update", "digest", "hexdigest", "copy"):
            return SafeValue(
                spec.KIND_CALLABLE, ("method", (spec.KIND_DIGEST, digest, name))
            )
        return wrap(getattr(digest, name))

    def _pattern_attr(self, pattern: Any, name: str) -> SafeValue:
        allowed = spec.METHODS[spec.KIND_REGEX_PATTERN]
        if name not in allowed:
            raise AttributeError(f"{name} is not available on a regex pattern")
        if name in (
            "findall",
            "finditer",
            "fullmatch",
            "match",
            "search",
            "split",
            "sub",
            "subn",
        ):
            return SafeValue(
                spec.KIND_CALLABLE, ("method", (spec.KIND_REGEX_PATTERN, pattern, name))
            )
        return wrap(getattr(pattern, name))

    def _match_attr(self, match: Any, name: str) -> SafeValue:
        allowed = spec.METHODS[spec.KIND_REGEX_MATCH]
        if name not in allowed:
            raise AttributeError(f"{name} is not available on a regex match")
        if name in ("group", "groups", "groupdict", "start", "end", "span", "expand"):
            return SafeValue(
                spec.KIND_CALLABLE, ("method", (spec.KIND_REGEX_MATCH, match, name))
            )
        return wrap(getattr(match, name))

    # ------------------------------------------------------------------
    # Operators
    # ------------------------------------------------------------------

    def binary_op(self, op: str, left: SafeValue, right: SafeValue) -> SafeValue:
        a, b = left.value, right.value
        try:
            if op == "Add":
                return wrap(a + b)
            if op == "Sub":
                return wrap(a - b)
            if op == "Mult":
                return wrap(a * b)
            if op == "Div":
                return wrap(a / b)
            if op == "FloorDiv":
                return wrap(a // b)
            if op == "Mod":
                return wrap(a % b)
            if op == "Pow":
                return wrap(a**b)
            if op == "BitAnd":
                return wrap(a & b)
            if op == "BitOr":
                return wrap(a | b)
            if op == "BitXor":
                return wrap(a ^ b)
            if op == "LShift":
                return wrap(a << b)
            if op == "RShift":
                return wrap(a >> b)
        except (TypeError, ValueError, ZeroDivisionError, OverflowError) as exc:
            raise type(exc)(
                f"unsupported operand types for {op}: {type(a).__name__} and {type(b).__name__}"
            ) from None
        raise TypeError(f"operator {op} is not supported")

    def unary_op(self, op: str, value: SafeValue) -> SafeValue:
        a = value.value
        try:
            if op == "UAdd":
                return wrap(+a)
            if op == "USub":
                return wrap(-a)
            if op == "Invert":
                return wrap(~a)
            if op == "Not":
                return wrap(not value.truthy())
        except TypeError as exc:
            raise TypeError(f"unsupported operand for unary {op}") from exc
        raise TypeError(f"operator {op} is not supported")

    def compare_op(self, op: str, left: SafeValue, right: SafeValue) -> bool:
        a, b = left.value, right.value
        try:
            if op == "Eq":
                return a == b
            if op == "NotEq":
                return a != b
            if op == "Lt":
                return a < b
            if op == "LtE":
                return a <= b
            if op == "Gt":
                return a > b
            if op == "GtE":
                return a >= b
            if op == "Is":
                return a is b
            if op == "IsNot":
                return a is not b
            if op == "In":
                return self.contains(left, right)
            if op == "NotIn":
                return not self.contains(left, right)
        except TypeError as exc:
            raise TypeError(
                f"unsupported comparison between {type(a).__name__} and {type(b).__name__}"
            ) from exc
        raise TypeError(f"comparison {op} is not supported")

    # ------------------------------------------------------------------
    # Module implementations
    # ------------------------------------------------------------------

    def _json_loads(
        self, args: list[SafeValue], kwargs: dict[str, SafeValue]
    ) -> SafeValue:
        if kwargs or len(args) != 1:
            raise TypeError("json.loads() accepts exactly one string argument")
        text = _require_str(args[0], "json.loads")
        try:
            return wrap(_json.loads(text))
        except ValueError as exc:
            raise ValueError(f"json.loads: {exc}") from None

    def _json_dumps(
        self, args: list[SafeValue], kwargs: dict[str, SafeValue]
    ) -> SafeValue:
        if kwargs or len(args) != 1:
            raise TypeError("json.dumps() accepts exactly one argument")
        value = validate_json_value(args[0].value)
        return wrap(
            _json.dumps(
                value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            )
        )

    def _time_module(
        self, member: str, args: list[SafeValue], kwargs: dict[str, SafeValue]
    ) -> SafeValue:
        if kwargs:
            raise TypeError(f"time.{member}() does not accept keyword arguments")
        plain = _unwrap_list(args)
        if member == "time":
            return wrap(_time.time())
        if member == "time_ns":
            return wrap(_time.time_ns())
        if member == "monotonic":
            return wrap(_time.monotonic())
        if member == "monotonic_ns":
            return wrap(_time.monotonic_ns())
        if member == "perf_counter":
            return wrap(_time.perf_counter())
        if member == "perf_counter_ns":
            return wrap(_time.perf_counter_ns())
        if member == "gmtime":
            return wrap(_time.gmtime(*plain))
        if member == "localtime":
            return wrap(_time.localtime(*plain))
        if member == "mktime":
            return wrap(_time.mktime(*plain))
        if member == "strftime":
            return wrap(_time.strftime(*plain))
        if member == "strptime":
            return wrap(_time.strptime(*plain))
        if member == "sleep":
            _time.sleep(*plain)
            return wrap(None)
        raise AttributeError(f"time.{member} is not callable")

    def _math_module(
        self, member: str, args: list[SafeValue], kwargs: dict[str, SafeValue]
    ) -> SafeValue:
        if kwargs:
            raise TypeError(f"math.{member}() does not accept keyword arguments")
        plain = _unwrap_list(args)
        func = getattr(math, member, None)
        if func is None or not callable(func):
            raise AttributeError(f"math.{member} is not callable")
        return wrap(func(*plain))

    def _re_module(
        self, member: str, args: list[SafeValue], kwargs: dict[str, SafeValue]
    ) -> SafeValue:
        if kwargs:
            raise TypeError(f"re.{member}() does not accept keyword arguments")
        plain = _unwrap_list(args)
        if member == "compile":
            if len(args) not in (1, 2):
                raise TypeError("re.compile(pattern, flags=0)")
            return wrap(re.compile(plain[0], plain[1] if len(args) > 1 else 0))
        if member == "escape":
            return wrap(re.escape(plain[0]))
        if member == "fullmatch":
            return wrap(re.fullmatch(*plain))
        if member == "findall":
            return wrap(re.findall(*plain))
        if member == "finditer":
            return wrap(list(re.finditer(*plain)))
        if member == "match":
            return wrap(re.match(*plain))
        if member == "search":
            return wrap(re.search(*plain))
        if member == "split":
            return wrap(re.split(*plain))
        if member == "sub":
            if callable(plain[1]):
                raise TypeError("re.sub replacement cannot be a callable")
            return wrap(re.sub(*plain))
        if member == "subn":
            if callable(plain[1]):
                raise TypeError("re.subn replacement cannot be a callable")
            return wrap(re.subn(*plain))
        raise AttributeError(f"re.{member} is not callable")

    def _base64_module(
        self, member: str, args: list[SafeValue], kwargs: dict[str, SafeValue]
    ) -> SafeValue:
        if kwargs:
            raise TypeError(f"base64.{member}() does not accept keyword arguments")
        plain = _unwrap_list(args)
        func = getattr(base64, member, None)
        if func is None or not callable(func):
            raise AttributeError(f"base64.{member} is not callable")
        return wrap(func(*plain))

    def _hashlib_module(
        self, member: str, args: list[SafeValue], kwargs: dict[str, SafeValue]
    ) -> SafeValue:
        if member == "algorithms_guaranteed":
            return wrap(frozenset(hashlib.algorithms_guaranteed))
        if member == "new":
            if len(args) != 1 or not isinstance(args[0].value, str):
                raise TypeError("hashlib.new() requires an algorithm name string")
            name = args[0].value
            if name not in {
                "md5",
                "sha1",
                "sha224",
                "sha256",
                "sha384",
                "sha512",
                "sha3_224",
                "sha3_256",
                "sha3_384",
                "sha3_512",
                "shake_128",
                "shake_256",
                "blake2b",
                "blake2s",
            }:
                raise ValueError(f"hashlib.new: algorithm {name!r} is not allowed")
            return wrap(hashlib.new(name))
        if kwargs:
            raise TypeError(f"hashlib.{member}() does not accept keyword arguments")
        plain = _unwrap_list(args)
        func = getattr(hashlib, member, None)
        if func is None or not callable(func):
            raise AttributeError(f"hashlib.{member} is not callable")
        return wrap(func(*plain))

    def _hmac_module(
        self, member: str, args: list[SafeValue], kwargs: dict[str, SafeValue]
    ) -> SafeValue:
        if kwargs:
            raise TypeError(f"hmac.{member}() does not accept keyword arguments")
        plain = _unwrap_list(args)
        if member == "new":
            if len(args) < 1 or len(args) > 3:
                raise TypeError("hmac.new(key, msg=None, digestmod='')")
            key = plain[0]
            digestmod = plain[2] if len(args) > 2 else None
            if isinstance(digestmod, str):
                if digestmod not in {
                    "md5",
                    "sha1",
                    "sha224",
                    "sha256",
                    "sha384",
                    "sha512",
                }:
                    raise ValueError(
                        f"hmac.new: digestmod {digestmod!r} is not allowed"
                    )
                digestmod = getattr(hashlib, digestmod)
            return wrap(hmac.new(key, plain[1] if len(args) > 1 else None, digestmod))
        if member == "digest":
            if len(args) != 3:
                raise TypeError("hmac.digest(key, msg, digest)")
            if isinstance(plain[2], str):
                if plain[2] not in {
                    "md5",
                    "sha1",
                    "sha224",
                    "sha256",
                    "sha384",
                    "sha512",
                }:
                    raise ValueError(f"hmac.digest: digest {plain[2]!r} is not allowed")
                plain = (plain[0], plain[1], getattr(hashlib, plain[2]))
            return wrap(hmac.digest(*plain))
        if member == "compare_digest":
            return wrap(hmac.compare_digest(*plain))
        raise AttributeError(f"hmac.{member} is not callable")

    def _urlparse_module(
        self, member: str, args: list[SafeValue], kwargs: dict[str, SafeValue]
    ) -> SafeValue:
        if kwargs:
            raise TypeError(
                f"urllib.parse.{member}() does not accept keyword arguments"
            )
        plain = _unwrap_list(args)
        func = getattr(urllib.parse, member, None)
        if func is None or not callable(func):
            raise AttributeError(f"urllib.parse.{member} is not callable")
        return wrap(func(*plain))


_MODULE_CONSTS: dict[tuple[str, str], Any] = {
    ("datetime", "MINYEAR"): _datetime.MINYEAR,
    ("datetime", "MAXYEAR"): _datetime.MAXYEAR,
    ("math", "e"): math.e,
    ("math", "pi"): math.pi,
    ("math", "tau"): math.tau,
    ("math", "inf"): math.inf,
    ("math", "nan"): math.nan,
    ("decimal", "ROUND_CEILING"): "ROUND_CEILING",
    ("decimal", "ROUND_DOWN"): "ROUND_DOWN",
    ("decimal", "ROUND_FLOOR"): "ROUND_FLOOR",
    ("decimal", "ROUND_HALF_DOWN"): "ROUND_HALF_DOWN",
    ("decimal", "ROUND_HALF_EVEN"): "ROUND_HALF_EVEN",
    ("decimal", "ROUND_HALF_UP"): "ROUND_HALF_UP",
    ("decimal", "ROUND_UP"): "ROUND_UP",
    ("decimal", "ROUND_05UP"): "ROUND_05UP",
    ("re", "ASCII"): re.ASCII,
    ("re", "IGNORECASE"): re.IGNORECASE,
    ("re", "MULTILINE"): re.MULTILINE,
    ("re", "DOTALL"): re.DOTALL,
    ("re", "VERBOSE"): re.VERBOSE,
}


# ---------------------------------------------------------------------------
# Method dispatch
# ---------------------------------------------------------------------------


def _str_join(value: str, plain: list[Any]) -> Any:
    if len(plain) != 1 or not isinstance(plain[0], (list, tuple)):
        raise TypeError("str.join() requires one iterable of strings")
    items = list(plain[0])
    if not all(isinstance(item, str) for item in items):
        raise TypeError("str.join() iterable must contain only strings")
    return value.join(items)


def _str_encode(value: str, plain: list[Any]) -> Any:
    if len(plain) > 1:
        raise TypeError("str.encode() takes at most 1 argument")
    encoding = plain[0] if plain else "utf-8"
    if encoding not in (
        "utf-8",
        "utf_8",
        "ascii",
        "latin-1",
        "latin1",
        "utf-16",
        "utf_16",
        "utf-32",
        "utf_32",
    ):
        raise ValueError(f"unsupported encoding {encoding!r}")
    return value.encode(encoding)


def _bytes_decode(value: bytes, plain: list[Any]) -> Any:
    if len(plain) > 1:
        raise TypeError("bytes.decode() takes at most 1 argument")
    encoding = plain[0] if plain else "utf-8"
    return value.decode(encoding)


def _method_dispatch_table() -> dict[str, dict[str, Any]]:
    table: dict[str, dict[str, Any]] = {}

    def add(kind: str, names: set[str], fn: Any) -> None:
        bucket = table.setdefault(kind, {})
        for name in names:
            bucket[name] = fn

    def passthrough(name: str) -> Any:
        def run(value: Any, plain: list[Any]) -> Any:
            return getattr(value, name)(*plain)

        return run

    for name in spec.METHODS[spec.KIND_STR]:
        if name == "join":
            add(spec.KIND_STR, {name}, _str_join)
        elif name == "encode":
            add(spec.KIND_STR, {name}, _str_encode)
        else:
            add(spec.KIND_STR, {name}, passthrough(name))
    for name in spec.METHODS[spec.KIND_BYTES]:
        if name == "decode":
            add(spec.KIND_BYTES, {name}, _bytes_decode)
        else:
            add(spec.KIND_BYTES, {name}, passthrough(name))
    for name in spec.METHODS[spec.KIND_LIST]:
        if name == "sort":

            def list_sort(value: Any, plain: list[Any]) -> Any:
                if plain:
                    raise TypeError("list.sort() does not accept positional arguments")
                value.sort()
                return None

            add(spec.KIND_LIST, {name}, list_sort)
        else:
            add(spec.KIND_LIST, {name}, passthrough(name))
    for name in spec.METHODS[spec.KIND_DICT]:
        add(spec.KIND_DICT, {name}, passthrough(name))
    for name in spec.METHODS[spec.KIND_SET]:
        if name in ("difference", "intersection", "symmetric_difference", "union"):

            def set_binary(value: Any, plain: list[Any], _name: str = name) -> Any:
                others = [
                    list(item) if isinstance(item, (list, tuple, set, range)) else item
                    for item in plain
                ]
                return getattr(value, _name)(*others)

            add(spec.KIND_SET, {name}, set_binary)
        else:
            add(spec.KIND_SET, {name}, passthrough(name))
    for name in spec.METHODS[spec.KIND_TUPLE]:
        add(spec.KIND_TUPLE, {name}, passthrough(name))
    for name in spec.METHODS[spec.KIND_RANGE]:
        add(spec.KIND_RANGE, {name}, passthrough(name))
    for name in spec.METHODS[spec.KIND_INT]:
        add(spec.KIND_INT, {name}, passthrough(name))
    for name in spec.METHODS[spec.KIND_FLOAT]:
        add(spec.KIND_FLOAT, {name}, passthrough(name))
    for name in spec.METHODS[spec.KIND_DECIMAL]:
        add(spec.KIND_DECIMAL, {name}, passthrough(name))
    for kind in (
        spec.KIND_DATETIME,
        spec.KIND_DATE,
        spec.KIND_TIME,
        spec.KIND_TIMEDELTA,
        spec.KIND_TIMEZONE,
        spec.KIND_ZONEINFO,
    ):
        for name in spec.METHODS[kind]:
            add(kind, {name}, passthrough(name))
    for name in spec.METHODS[spec.KIND_REGEX_PATTERN]:
        add(spec.KIND_REGEX_PATTERN, {name}, passthrough(name))
    for name in spec.METHODS[spec.KIND_REGEX_MATCH]:
        add(spec.KIND_REGEX_MATCH, {name}, passthrough(name))
    for name in spec.METHODS[spec.KIND_DIGEST]:
        add(spec.KIND_DIGEST, {name}, passthrough(name))
    for name in spec.METHODS[spec.KIND_HMAC]:
        add(spec.KIND_HMAC, {name}, passthrough(name))

    # State methods operate on the state container (which commits atomically).
    for name in spec.METHODS[spec.KIND_STATE]:
        add(spec.KIND_STATE, {name}, passthrough(name))
    return table


_METHOD_DISPATCH = _method_dispatch_table()


def _state_write(container: Any, key: Any, value: Any) -> None:
    container[key] = value


def _state_delete(container: Any, key: Any) -> None:
    del container[key]


__all__ = [
    "StateDict",
    "StateList",
    "Stdlib",
    "build_state_dict",
    "unwrap",
    "wrap",
]
