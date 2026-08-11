"""AST interpreter for ``astrbot-python-subset/v1``.

The interpreter walks the same grammar subset enforced by the validator and
resolves every operation through the trusted ``Stdlib`` dispatch tables.
Scripts never receive raw Python objects, coroutine handles or host callables.
"""

from __future__ import annotations

import ast
import time
from dataclasses import dataclass
from typing import Any

from astrbot.script_runtime import spec
from astrbot.script_runtime.errors import (
    ALL_CATCHABLE,
    ScriptInterrupted,
    ScriptLanguageVersionError,
    ScriptProtocolError,
    ScriptRuntimeError,
)
from astrbot.script_runtime.stdlib import Stdlib, unwrap, wrap
from astrbot.script_runtime.values import SafeValue


class ReturnSignal(Exception):
    def __init__(self, value: SafeValue) -> None:
        super().__init__()
        self.value = value


class BreakSignal(Exception):
    pass


class ContinueSignal(Exception):
    pass


@dataclass
class RunFacade:
    """The read-only ``ctx.run`` object."""

    job_id: str
    run_id: str
    started_at: Any
    timezone: str


def _exception_class_map() -> dict[str, type[BaseException]]:
    mapping: dict[str, type[BaseException]] = {}
    for exc in ALL_CATCHABLE:
        mapping[exc.__name__] = exc
    return mapping


class Interpreter:
    """Executes a validated source tree against a trusted ``Stdlib``."""

    def __init__(
        self,
        stdlib: Stdlib,
        *,
        deadline: float | None,
    ) -> None:
        self.stdlib = stdlib
        self.deadline = deadline
        self._exception_map = _exception_class_map()
        self._current_exception: BaseException | None = None
        self._last_final_value: SafeValue | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run_module(self, source: str) -> SafeValue:
        if not isinstance(source, str):
            raise ScriptProtocolError("source must be a string")
        try:
            tree = ast.parse(
                source,
                mode="exec",
                type_comments=False,
                feature_version=spec.FEATURE_VERSION,
            )
        except SyntaxError:
            raise ScriptLanguageVersionError("syntax error") from None
        scope = self._root_scope()
        try:
            await self._exec_body(tree.body, scope, is_module=True)
        except (BreakSignal, ContinueSignal) as exc:
            raise ScriptRuntimeError("break/continue outside loop") from exc
        except ReturnSignal:
            pass
        return self._last_final_value or SafeValue(spec.KIND_NONE, None)

    def _root_scope(self) -> dict[str, SafeValue]:
        scope: dict[str, SafeValue] = {}
        for name in spec.ALLOWED_BUILTINS:
            scope[name] = SafeValue(spec.KIND_CALLABLE, ("builtin", name))
        for name in spec.ALLOWED_EXCEPTIONS:
            scope[name] = SafeValue(spec.KIND_CLASS, ("script_exc", name))
        scope["ctx"] = SafeValue(spec.KIND_CTX, None)
        return scope

    def _remaining(self) -> float:
        if self.deadline is None:
            return float("inf")
        return max(self.deadline - time.monotonic(), 0.0)

    # ------------------------------------------------------------------
    # Statements
    # ------------------------------------------------------------------

    async def _exec_body(
        self,
        body: list[ast.stmt],
        scope: dict[str, SafeValue],
        *,
        is_module: bool = False,
    ) -> None:
        for stmt in body:
            if self._remaining() <= 0:
                raise ScriptInterrupted("run deadline expired")
            await self._exec_stmt(stmt, scope, is_module=is_module)

    async def _exec_stmt(
        self, node: ast.stmt, scope: dict[str, SafeValue], *, is_module: bool
    ) -> None:
        node_type = type(node).__name__
        if node_type == "Import":
            for alias in node.names:
                module = alias.name
                name = alias.asname or module
                scope[name] = SafeValue(spec.KIND_MODULE, module)
            return
        if node_type == "ImportFrom":
            module = node.module or ""
            for alias in node.names:
                if alias.name == "*":
                    continue
                name = alias.asname or alias.name
                scope[name] = self.stdlib.get_module_member(module, alias.name)
            return
        if node_type in ("FunctionDef", "AsyncFunctionDef"):
            is_async = node_type == "AsyncFunctionDef"
            scope[node.name] = self._make_user_func(node, scope, is_async=is_async)
            return
        if node_type == "Assign":
            value = await self._eval_expr(node.value, scope)
            for target in node.targets:
                await self._assign(target, value, scope)
            return
        if node_type == "AugAssign":
            current = await self._eval_expr(node.target, scope)
            right = await self._eval_expr(node.value, scope)
            op = type(node.op).__name__
            result = self.stdlib.binary_op(op, current, right)
            await self._assign(node.target, result, scope)
            return
        if node_type == "Expr":
            self._last_final_value = await self._eval_expr(node.value, scope)
            return
        if node_type == "If":
            test = await self._eval_expr(node.test, scope)
            if test.truthy():
                await self._exec_body(node.body, scope)
            else:
                await self._exec_body(node.orelse, scope)
            return
        if node_type == "For":
            iterable = await self._eval_expr(node.iter, scope)
            items = self.stdlib._iterate(iterable)
            broke = False
            for item in items:
                await self._assign(node.target, item, scope)
                try:
                    await self._exec_body(node.body, scope)
                except BreakSignal:
                    broke = True
                    break
                except ContinueSignal:
                    continue
            if not broke:
                await self._exec_body(node.orelse, scope)
            return
        if node_type == "While":
            broke = False
            while True:
                if self._remaining() <= 0:
                    raise ScriptInterrupted("run deadline expired")
                test = await self._eval_expr(node.test, scope)
                if not test.truthy():
                    break
                try:
                    await self._exec_body(node.body, scope)
                except BreakSignal:
                    broke = True
                    break
                except ContinueSignal:
                    continue
            if not broke:
                await self._exec_body(node.orelse, scope)
            return
        if node_type == "Break":
            raise BreakSignal()
        if node_type == "Continue":
            raise ContinueSignal()
        if node_type == "Pass":
            return
        if node_type == "Return":
            value = (
                await self._eval_expr(node.value, scope)
                if node.value is not None
                else SafeValue(spec.KIND_NONE, None)
            )
            raise ReturnSignal(value)
        if node_type == "Try":
            await self._exec_try(node, scope)
            return
        if node_type == "Raise":
            await self._exec_raise(node, scope)
            return
        raise ScriptRuntimeError(f"unsupported statement: {node_type}")

    async def _exec_try(self, node: ast.Try, scope: dict[str, SafeValue]) -> None:
        error: BaseException | None = None
        matched = False
        try:
            try:
                await self._exec_body(node.body, scope)
            except tuple(ALL_CATCHABLE) as exc:
                error = exc
                matched = self._match_handler(node.handlers, exc)
                if not matched:
                    raise
            else:
                await self._exec_body(node.orelse, scope)
        finally:
            await self._exec_body(node.finalbody, scope)
        if matched and error is not None:
            handler = self._matching_handler(node.handlers, error)
            previous = self._current_exception
            self._current_exception = error
            try:
                handler_scope = scope
                if handler.name:
                    handler_scope = dict(scope)
                    handler_scope[handler.name] = SafeValue(spec.KIND_EXCEPTION, error)
                await self._exec_body(handler.body, handler_scope)
            finally:
                self._current_exception = previous

    def _matching_handler(self, handlers: list[ast.ExceptHandler], exc: BaseException):
        for handler in handlers:
            if self._handler_matches(handler, exc):
                return handler
        raise ScriptRuntimeError("no matching except handler")  # pragma: no cover

    def _match_handler(
        self, handlers: list[ast.ExceptHandler], exc: BaseException
    ) -> bool:
        return self._matching_handler(handlers, exc) is not None

    def _handler_matches(self, handler: ast.ExceptHandler, exc: BaseException) -> bool:
        if handler.type is None:
            return True
        if isinstance(handler.type, ast.Tuple):
            names = [elt.id for elt in handler.type.elts if isinstance(elt, ast.Name)]
        else:
            names = [handler.type.id] if isinstance(handler.type, ast.Name) else []
        for name in names:
            cls = self._exception_map.get(name)
            if cls is not None and isinstance(exc, cls):
                return True
        return False

    async def _exec_raise(self, node: ast.Raise, scope: dict[str, SafeValue]) -> None:
        if node.exc is None:
            if self._current_exception is None:
                raise ScriptRuntimeError("bare raise outside except block")
            raise self._current_exception
        value = await self._eval_expr(node.exc, scope)
        if value.kind != spec.KIND_EXCEPTION:
            raise ScriptRuntimeError("raise target is not an exception value")
        raise value.value

    def _make_user_func(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        scope: dict[str, SafeValue],
        *,
        is_async: bool,
    ):
        param_names = [arg.arg for arg in node.args.args]
        defaults = node.args.defaults
        default_values = [unwrap(self._const_default(d)) for d in defaults]
        default_offset = len(param_names) - len(default_values)

        def bind_args(
            args: list[SafeValue], kwargs: dict[str, SafeValue]
        ) -> dict[str, SafeValue]:
            if kwargs:
                raise TypeError(f"{node.name}() got unexpected keyword arguments")
            if len(args) < default_offset or len(args) > len(param_names):
                raise TypeError(
                    f"{node.name}() takes {default_offset}..{len(param_names)} arguments"
                )
            bound = dict(scope)
            for index, name in enumerate(param_names):
                if index < len(args):
                    bound[name] = args[index]
                else:
                    bound[name] = wrap(default_values[index - default_offset])
            return bound

        async def run_async(
            args: list[SafeValue], kwargs: dict[str, SafeValue]
        ) -> SafeValue:
            bound = bind_args(args, kwargs)
            try:
                await self._exec_body(node.body, bound)
            except ReturnSignal as signal:
                return signal.value
            return SafeValue(spec.KIND_NONE, None)

        if is_async:
            return SafeValue(spec.KIND_USER_ASYNC_FUNC, run_async)
        return SafeValue(spec.KIND_USER_SYNC_FUNC, run_async)

    def _const_default(self, node: ast.AST) -> SafeValue:
        if isinstance(node, ast.Constant):
            return wrap(node.value)
        raise ScriptRuntimeError("non-constant default value")

    async def _assign(
        self, target: ast.AST, value: SafeValue, scope: dict[str, SafeValue]
    ) -> None:
        if isinstance(target, ast.Name):
            scope[target.id] = value
            return
        if isinstance(target, (ast.Tuple, ast.List)):
            items = self._unpack(value, len(target.elts))
            for elt, item in zip(target.elts, items):
                await self._assign(elt, item, scope)
            return
        if isinstance(target, ast.Subscript):
            base = await self._eval_expr(target.value, scope)
            key = await self._eval_expr(target.slice, scope)
            self.stdlib.set_item(base, key, value)
            return
        raise ScriptRuntimeError("unsupported assignment target")

    def _unpack(self, value: SafeValue, count: int) -> list[SafeValue]:
        if value.kind not in (
            spec.KIND_LIST,
            spec.KIND_TUPLE,
            spec.KIND_STR,
            spec.KIND_BYTES,
        ):
            raise TypeError("cannot unpack non-sequence value")
        items = list(value.value)
        if len(items) != count:
            raise ValueError(f"cannot unpack {len(items)} values into {count} targets")
        return [wrap(item) for item in items]

    # ------------------------------------------------------------------
    # Expressions
    # ------------------------------------------------------------------

    async def _eval_expr(self, node: ast.AST, scope: dict[str, SafeValue]) -> SafeValue:
        node_type = type(node).__name__
        if node_type == "Constant":
            return wrap(node.value)
        if node_type == "Name":
            if node.id not in scope:
                raise NameError(f"name {node.id!r} is not defined")
            return scope[node.id]
        if node_type == "List":
            return SafeValue(
                spec.KIND_LIST,
                [unwrap(await self._eval_expr(elt, scope)) for elt in node.elts],
            )
        if node_type == "Tuple":
            return SafeValue(
                spec.KIND_TUPLE,
                tuple(unwrap(await self._eval_expr(elt, scope)) for elt in node.elts),
            )
        if node_type == "Set":
            return SafeValue(
                spec.KIND_SET,
                {unwrap(await self._eval_expr(elt, scope)) for elt in node.elts},
            )
        if node_type == "Dict":
            result: dict[Any, Any] = {}
            for key_node, value_node in zip(node.keys, node.values):
                if key_node is None:
                    raise TypeError("dict unpacking is not supported")
                key = await self._eval_expr(key_node, scope)
                value = await self._eval_expr(value_node, scope)
                result[key.value] = value.value
            return SafeValue(spec.KIND_DICT, result)
        if node_type == "BinOp":
            left = await self._eval_expr(node.left, scope)
            right = await self._eval_expr(node.right, scope)
            return self.stdlib.binary_op(type(node.op).__name__, left, right)
        if node_type == "UnaryOp":
            operand = await self._eval_expr(node.operand, scope)
            return self.stdlib.unary_op(type(node.op).__name__, operand)
        if node_type == "BoolOp":
            values = [await self._eval_expr(value, scope) for value in node.values]
            if type(node.op).__name__ == "And":
                result: SafeValue | None = None
                for value in values:
                    if not value.truthy():
                        return value
                    result = value
                return result or SafeValue(spec.KIND_BOOL, True)
            result = None
            for value in values:
                if value.truthy():
                    return value
                result = value
            return result or SafeValue(spec.KIND_BOOL, False)
        if node_type == "Compare":
            left = await self._eval_expr(node.left, scope)
            for op, comparator_node in zip(node.ops, node.comparators):
                right = await self._eval_expr(comparator_node, scope)
                if not self.stdlib.compare_op(type(op).__name__, left, right):
                    return SafeValue(spec.KIND_BOOL, False)
                left = right
            return SafeValue(spec.KIND_BOOL, True)
        if node_type == "IfExp":
            test = await self._eval_expr(node.test, scope)
            if test.truthy():
                return await self._eval_expr(node.body, scope)
            return await self._eval_expr(node.orelse, scope)
        if node_type == "Subscript":
            base = await self._eval_expr(node.value, scope)
            key = await self._eval_expr(node.slice, scope)
            if isinstance(node.slice, ast.Slice):
                start = key.value.start if key.value.start is not None else None
                stop = key.value.stop if key.value.stop is not None else None
                step = key.value.step if key.value.step is not None else None
                return wrap(base.value[start:stop:step])
            return self.stdlib.get_item(base, key)
        if node_type == "Slice":
            lower = (
                await self._eval_expr(node.lower, scope)
                if node.lower is not None
                else None
            )
            upper = (
                await self._eval_expr(node.upper, scope)
                if node.upper is not None
                else None
            )
            step = (
                await self._eval_expr(node.step, scope)
                if node.step is not None
                else None
            )
            return SafeValue(
                spec.KIND_UNKNOWN,
                slice(
                    lower.value if lower is not None else None,
                    upper.value if upper is not None else None,
                    step.value if step is not None else None,
                ),
            )
        if node_type == "Attribute":
            base = await self._eval_expr(node.value, scope)
            return self.stdlib.get_attr(base, node.attr)
        if node_type == "Call":
            return await self._eval_call(node, scope)
        if node_type == "Await":
            return await self._eval_expr(node.value, scope)
        if node_type in ("ListComp", "SetComp", "DictComp"):
            return await self._eval_comprehension(node, scope, node_type)
        if node_type == "JoinedStr":
            parts = []
            for value in node.values:
                if isinstance(value, ast.Constant):
                    parts.append(value.value)
                else:
                    parts.append(unwrap(await self._eval_expr(value, scope)))
            return SafeValue(spec.KIND_STR, "".join(str(part) for part in parts))
        if node_type == "FormattedValue":
            return await self._eval_formatted_value(node, scope)
        raise ScriptRuntimeError(f"unsupported expression: {node_type}")

    async def _eval_call(
        self, node: ast.Call, scope: dict[str, SafeValue]
    ) -> SafeValue:
        callee = await self._eval_expr(node.func, scope)
        args = [await self._eval_expr(arg, scope) for arg in node.args]
        kwargs: dict[str, SafeValue] = {}
        for kw in node.keywords:
            if kw.arg is None:
                raise TypeError("**kwargs expansion is not supported")
            kwargs[kw.arg] = await self._eval_expr(kw.value, scope)
        return await self.stdlib.call_callable(callee, args, kwargs)

    async def _eval_formatted_value(
        self, node: ast.FormattedValue, scope: dict[str, SafeValue]
    ) -> SafeValue:
        value = await self._eval_expr(node.value, scope)
        raw = value.value
        if node.conversion == 115:  # !s
            text = str(raw)
        elif node.conversion == 114:  # !r
            from astrbot.script_runtime.stdlib import _safe_repr

            text = _safe_repr(raw)
        elif node.conversion == 97:  # !a
            text = ascii(raw)
        else:
            text = str(raw)
        if node.format_spec is not None:
            spec_sv = await self._eval_expr(node.format_spec, scope)
            from astrbot.script_runtime.stdlib import _format_plain

            text = _format_plain(raw, spec_sv.value)
        return SafeValue(spec.KIND_STR, text)

    async def _eval_comprehension(
        self,
        node: ast.AST,
        scope: dict[str, SafeValue],
        node_type: str,
    ) -> SafeValue:
        result_list: list[Any] = []
        result_set: set[Any] = set()
        result_dict: dict[Any, Any] = {}

        async def build(
            generators: list[ast.comprehension],
            index: int,
            child_scope: dict[str, SafeValue],
        ) -> None:
            if index >= len(generators):
                if node_type == "ListComp":
                    result_list.append(
                        unwrap(await self._eval_expr(node.elt, child_scope))
                    )
                elif node_type == "SetComp":
                    result_set.add(unwrap(await self._eval_expr(node.elt, child_scope)))
                else:
                    key = await self._eval_expr(node.key, child_scope)
                    value = await self._eval_expr(node.value, child_scope)
                    result_dict[key.value] = value.value
                return
            generator = generators[index]
            iterable = await self._eval_expr(generator.iter, child_scope)
            items = self.stdlib._iterate(iterable)
            for item in items:
                next_scope = dict(child_scope)
                await self._assign(generator.target, item, next_scope)
                conditions_ok = True
                for condition in generator.ifs:
                    if not (await self._eval_expr(condition, next_scope)).truthy():
                        conditions_ok = False
                        break
                if conditions_ok:
                    await build(generators, index + 1, next_scope)

        await build(node.generators, 0, scope)
        if node_type == "ListComp":
            return SafeValue(spec.KIND_LIST, result_list)
        if node_type == "SetComp":
            return SafeValue(spec.KIND_SET, result_set)
        return SafeValue(spec.KIND_DICT, result_dict)


__all__ = ["BreakSignal", "ContinueSignal", "Interpreter", "ReturnSignal", "RunFacade"]
