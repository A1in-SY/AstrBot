"""Static validator for ``astrbot-python-subset/v1`` sources.

The validator performs a full-AST walk with lexical scope tracking and
abstract value provenance.  It reports every independent violation it can find
in one pass.  It never executes the source and never imports ``astrbot.core``.
"""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass
from typing import Any

from astrbot.script_runtime import spec
from astrbot.script_runtime.diagnostics import (
    ASSIGNMENT_TARGET_NOT_ALLOWED,
    AST_TOO_DEEP,
    AST_TOO_LARGE,
    ASYNC_CALL_REQUIRES_AWAIT,
    ATTRIBUTE_NOT_ALLOWED,
    AWAIT_TARGET_NOT_ALLOWED,
    BUILTIN_NOT_ALLOWED,
    CALL_NOT_ALLOWED,
    CALL_SIGNATURE_INVALID,
    CAPABILITY_VALUE_NOT_ALLOWED,
    CONTROL_FLOW_NOT_ALLOWED,
    DEFAULT_VALUE_NOT_JSON,
    EXCEPTION_TYPE_NOT_ALLOWED,
    FUNCTION_LOCATION_NOT_ALLOWED,
    FUNCTION_SIGNATURE_NOT_ALLOWED,
    FUNCTION_VALUE_NOT_ALLOWED,
    IMPORT_FORM_NOT_ALLOWED,
    INVALID_SOURCE_ENCODING,
    MODULE_MEMBER_NOT_ALLOWED,
    MODULE_NOT_ALLOWED,
    NAME_NOT_DEFINED,
    NAME_REBIND_NOT_ALLOWED,
    NODE_NOT_ALLOWED,
    RELATIVE_IMPORT_NOT_ALLOWED,
    SOURCE_TOO_LARGE,
    SYNTAX_ERROR,
    WILDCARD_IMPORT_NOT_ALLOWED,
    DiagnosticCollector,
    DiagnosticOccurrence,
    ValidationResult,
    node_occurrence,
)


def compute_source_hash(source: str) -> str:
    """SHA-256 (lowercase hex) of the raw UTF-8 source bytes."""
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def count_ast(source: str) -> tuple[int, int]:
    """Return ``(node_count, max_depth)`` for a parsed module."""
    tree = ast.parse(
        source,
        mode="exec",
        type_comments=False,
        feature_version=spec.FEATURE_VERSION,
    )
    count = 0
    max_depth = 0

    def walk(node: ast.AST, depth: int) -> None:
        nonlocal count, max_depth
        count += 1
        if depth > max_depth:
            max_depth = depth
        for child in ast.iter_child_nodes(node):
            walk(child, depth + 1)

    walk(tree, 1)
    return count, max_depth


@dataclass
class Binding:
    kind: str
    module: str | None = None
    member: str | None = None
    member_kind: str | None = None
    is_async: bool = False
    value_kind: str | None = None
    name: str | None = None


@dataclass
class KindInfo:
    """Abstract provenance of an expression."""

    value_kind: str | None = None
    binding: Binding | None = None
    module: str | None = None
    member: str | None = None
    member_kind: str | None = None
    callable_kind: str | None = None
    class_member: tuple[str, str] | None = None
    receiver_kind: str | None = None
    method_name: str | None = None
    escapeable: bool = False
    async_capability: bool = False
    tainted: bool = False

    @classmethod
    def unknown(cls, *, tainted: bool = False) -> KindInfo:
        return cls(value_kind=spec.KIND_UNKNOWN, tainted=tainted)


_ITERABLE_KINDS = frozenset(
    {
        spec.KIND_LIST,
        spec.KIND_TUPLE,
        spec.KIND_SET,
        spec.KIND_STR,
        spec.KIND_BYTES,
        spec.KIND_DICT,
        spec.KIND_STATE,
        spec.KIND_RANGE,
        spec.KIND_HEADERS,
        spec.KIND_REGEX_MATCH,
        spec.KIND_UNKNOWN,
    }
)

_SUBSCRIPTABLE_KINDS = frozenset(
    {
        spec.KIND_LIST,
        spec.KIND_TUPLE,
        spec.KIND_STR,
        spec.KIND_BYTES,
        spec.KIND_DICT,
        spec.KIND_STATE,
        spec.KIND_HEADERS,
        spec.KIND_STRUCT_TIME,
        spec.KIND_RANGE,
        spec.KIND_UNKNOWN,
    }
)

_JSON_SCALAR_KINDS = frozenset(
    {spec.KIND_NONE, spec.KIND_BOOL, spec.KIND_INT, spec.KIND_FLOAT, spec.KIND_STR}
)

_KNOWN_UNALLOWED_EXCEPTIONS = frozenset(
    {
        "MemoryError",
        "SystemError",
        "SystemExit",
        "KeyboardInterrupt",
        "GeneratorExit",
        "OSError",
        "IOError",
        "AttributeError",
        "ImportError",
        "ModuleNotFoundError",
        "StopIteration",
        "StopAsyncIteration",
        "RecursionError",
        "InterruptedError",
        "ConnectionError",
        "FileNotFoundError",
        "NotImplementedError",
        "AssertionError",
        "SyntaxError",
        "IndentationError",
        "TabError",
        "EOFError",
        "FloatingPointError",
        "BufferError",
        "ReferenceError",
    }
)


class ScriptValidator:
    """Full-AST validator producing grouped diagnostics."""

    def __init__(self, language_version: str, limits: dict[str, int]) -> None:
        self.language_version = language_version
        self.limits = spec.coerce_limits(limits)
        self.collector = DiagnosticCollector()
        self._lines: list[str] = []
        self._node_count = 0
        self._await_allowed_stack: list[bool] = []

    # ------------------------------------------------------------------
    # Public entry
    # ------------------------------------------------------------------

    def validate(self, source: str) -> ValidationResult:
        try:
            encoded = source.encode("utf-8")
        except UnicodeEncodeError as exc:
            occurrence = DiagnosticOccurrence(
                line=1, column=1, end_line=1, end_column=1
            )
            self.collector.add(
                INVALID_SOURCE_ENCODING,
                subject="source",
                hint="Source must be valid UTF-8.",
                occurrence=occurrence,
                message=f"Source cannot be encoded as UTF-8: {exc}",
            )
            return self.collector.result(self.language_version)
        if len(encoded) > self.limits["max_source_bytes"]:
            occurrence = DiagnosticOccurrence(
                line=1, column=1, end_line=1, end_column=1
            )
            self.collector.add(
                SOURCE_TOO_LARGE,
                subject="source",
                hint=f"Source must not exceed {self.limits['max_source_bytes']} UTF-8 bytes.",
                occurrence=occurrence,
                message=(
                    f"Source is {len(encoded)} UTF-8 bytes; the limit is "
                    f"{self.limits['max_source_bytes']}."
                ),
            )
            return self.collector.result(self.language_version)

        self._lines = source.splitlines(keepends=False) or [""]
        try:
            tree = ast.parse(
                source,
                mode="exec",
                type_comments=False,
                feature_version=spec.FEATURE_VERSION,
            )
        except SyntaxError as exc:
            occurrence = self._syntax_error_occurrence(exc)
            self.collector.add(
                SYNTAX_ERROR,
                subject=exc.msg or "syntax error",
                hint="Fix the syntax and validate again.",
                occurrence=occurrence,
                message=f"Syntax error: {exc.msg}",
            )
            return self.collector.result(self.language_version)
        except RecursionError:
            occurrence = DiagnosticOccurrence(
                line=1, column=1, end_line=1, end_column=1
            )
            self.collector.add(
                AST_TOO_DEEP,
                subject="source",
                hint="The source nesting is too deep to parse.",
                occurrence=occurrence,
            )
            return self.collector.result(self.language_version)

        try:
            node_count, max_depth = count_ast(source)
        except RecursionError:
            node_count, max_depth = (
                self.limits["max_ast_nodes"] + 1,
                self.limits["max_ast_depth"] + 1,
            )
        if node_count > self.limits["max_ast_nodes"]:
            occurrence = self._tree_occurrence(tree)
            self.collector.add(
                AST_TOO_LARGE,
                subject="source",
                hint=f"Source must not exceed {self.limits['max_ast_nodes']} AST nodes.",
                occurrence=occurrence,
                message=(
                    f"Source has {node_count} AST nodes; the limit is "
                    f"{self.limits['max_ast_nodes']}."
                ),
            )
            return self.collector.result(self.language_version)
        if max_depth > self.limits["max_ast_depth"]:
            occurrence = self._tree_occurrence(tree)
            self.collector.add(
                AST_TOO_DEEP,
                subject="source",
                hint=f"Source must not nest deeper than {self.limits['max_ast_depth']} AST levels.",
                occurrence=occurrence,
                message=(
                    f"Source nests {max_depth} AST levels; the limit is "
                    f"{self.limits['max_ast_depth']}."
                ),
            )
            return self.collector.result(self.language_version)

        module_scope = _Scope(is_module=True)
        self._seed_scope(module_scope)
        try:
            self._visit_body(tree.body, module_scope, parent=None, is_async_body=True)
        except RecursionError:
            occurrence = node_occurrence(tree, self._lines)
            self.collector.add(
                AST_TOO_DEEP,
                subject="source",
                hint="The source nesting is too deep to validate.",
                occurrence=occurrence,
            )
        return self.collector.result(self.language_version)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _seed_scope(self, scope: _Scope) -> None:
        scope.bind("ctx", Binding(kind="ctx", value_kind=spec.KIND_CTX))
        scope.bind("http", Binding(kind="ctx_http", value_kind=spec.KIND_CTX_HTTP))
        scope.bind("run", Binding(kind="ctx_run", value_kind=spec.KIND_CTX_RUN))
        scope.bind("state", Binding(kind="ctx_state", value_kind=spec.KIND_CTX_STATE))
        for name in sorted(spec.ALLOWED_BUILTINS):
            scope.bind(name, Binding(kind="builtin", value_kind=spec.KIND_CALLABLE))
        for name in sorted(spec.ALLOWED_EXCEPTIONS):
            scope.bind(
                name, Binding(kind="exception_class", value_kind=spec.KIND_CLASS)
            )

    def _syntax_error_occurrence(self, exc: SyntaxError) -> DiagnosticOccurrence:
        lineno = exc.lineno or 1
        offset = exc.offset or 1
        line_text = self._lines[lineno - 1] if 0 < lineno <= len(self._lines) else ""
        # CPython reports SyntaxError.offset in UTF-8 bytes for str input on
        # some versions and characters on others; convert defensively.
        column = 1
        consumed = 0
        for char in line_text:
            char_len = len(char.encode("utf-8"))
            if consumed + char_len >= max(offset, 1):
                break
            consumed += char_len
            column += 1 if ord(char) <= 0xFFFF else 2
        return DiagnosticOccurrence(
            line=lineno,
            column=column,
            end_line=lineno,
            end_column=column + 1,
        )

    def _occurrence(self, node: ast.AST) -> DiagnosticOccurrence:
        return node_occurrence(node, self._lines)

    def _tree_occurrence(self, tree: ast.Module) -> DiagnosticOccurrence:
        if tree.body:
            return self._occurrence(tree.body[0])
        return DiagnosticOccurrence(line=1, column=1, end_line=1, end_column=1)

    def _add(
        self,
        code: str,
        *,
        subject: str,
        hint: str | None,
        node: ast.AST,
        message: str | None = None,
    ) -> None:
        self.collector.add(
            code,
            subject=subject,
            hint=hint,
            occurrence=self._occurrence(node),
            message=message,
        )

    # ------------------------------------------------------------------
    # Scope model
    # ------------------------------------------------------------------

    def _resolve(self, name: str, scope: _Scope) -> Binding | None:
        return scope.lookup(name)

    def _check_rebindable(self, name: str, scope: _Scope, node: ast.AST) -> bool:
        binding = self._resolve(name, scope)
        if binding is None:
            return True
        if binding.kind in {
            "ctx",
            "ctx_http",
            "ctx_run",
            "ctx_state",
            "builtin",
            "module",
            "imported",
            "user_func",
            "exception_class",
        }:
            self._add(
                NAME_REBIND_NOT_ALLOWED,
                subject=name,
                hint=f"Do not reassign `{name}`.",
                node=node,
                message=f"`{name}` cannot be reassigned.",
            )
            return False
        return True

    # ------------------------------------------------------------------
    # Statement dispatch
    # ------------------------------------------------------------------

    def _visit_body(
        self,
        body: list[ast.stmt],
        scope: _Scope,
        *,
        parent: ast.AST | None,
        is_async_body: bool,
        in_loop: bool = False,
        in_function: bool = False,
    ) -> None:
        self._await_allowed_stack.append(is_async_body)
        try:
            for stmt in body:
                self._visit_stmt(
                    stmt,
                    scope,
                    parent=parent,
                    is_async_body=is_async_body,
                    in_loop=in_loop,
                    in_function=in_function,
                )
        finally:
            self._await_allowed_stack.pop()

    def _visit_stmt(
        self,
        node: ast.stmt,
        scope: _Scope,
        *,
        parent: ast.AST | None,
        is_async_body: bool,
        in_loop: bool,
        in_function: bool,
    ) -> None:
        node_type = type(node).__name__
        if node_type not in spec.ALLOWED_STATEMENTS:
            self._reject_node(node, node_type)
            return
        if node_type == "Import":
            self._visit_import(node, scope, is_module=scope.is_module)
            return
        if node_type == "ImportFrom":
            self._visit_import_from(node, scope, is_module=scope.is_module)
            return
        if node_type in ("FunctionDef", "AsyncFunctionDef"):
            self._visit_function_def(
                node, scope, is_async=node_type == "AsyncFunctionDef"
            )
            return
        if node_type == "Assign":
            self._visit_assign(node, scope, parent=parent, is_async_body=is_async_body)
            return
        if node_type == "AugAssign":
            self._visit_aug_assign(node, scope)
            return
        if node_type == "Expr":
            self._visit_expr_stmt(node, scope)
            return
        if node_type == "If":
            self._visit_if(
                node,
                scope,
                parent=parent,
                is_async_body=is_async_body,
                in_loop=in_loop,
                in_function=in_function,
            )
            return
        if node_type == "For":
            self._visit_for(
                node,
                scope,
                parent=parent,
                is_async_body=is_async_body,
                in_function=in_function,
            )
            return
        if node_type == "While":
            self._visit_while(
                node,
                scope,
                parent=parent,
                is_async_body=is_async_body,
                in_function=in_function,
            )
            return
        if node_type in ("Break", "Continue"):
            if not in_loop:
                self._add(
                    CONTROL_FLOW_NOT_ALLOWED,
                    subject=node_type.lower(),
                    hint=f"`{node_type.lower()}` must appear inside a for or while loop.",
                    node=node,
                    message=f"`{node_type.lower()}` outside a loop.",
                )
            return
        if node_type == "Pass":
            return
        if node_type == "Return":
            if not in_function:
                self._add(
                    CONTROL_FLOW_NOT_ALLOWED,
                    subject="return",
                    hint="`return` is only allowed inside a user function.",
                    node=node,
                )
            if node.value is not None:
                info = self._expr(node.value, scope, parent=node)
                self._check_non_escapeable(node.value, info, scope, parent=node)
            return
        if node_type == "Try":
            self._visit_try(
                node,
                scope,
                parent=parent,
                is_async_body=is_async_body,
                in_loop=in_loop,
                in_function=in_function,
            )
            return
        if node_type == "Raise":
            self._visit_raise(node, scope)
            return

    def _reject_node(self, node: ast.AST, node_type: str) -> None:
        self._add(
            NODE_NOT_ALLOWED,
            subject=node_type,
            hint=f"`{node_type}` is not supported in {self.language_version}.",
            node=node,
            message=f"`{node_type}` is not supported.",
        )

    def _visit_import(
        self, node: ast.Import, scope: _Scope, *, is_module: bool
    ) -> None:
        if not is_module:
            self._add(
                FUNCTION_LOCATION_NOT_ALLOWED,
                subject="import",
                hint="Imports are only allowed at module top level.",
                node=node,
            )
            return
        for alias in node.names:
            if alias.name.count(".") > 0 and not alias.asname:
                self._add(
                    IMPORT_FORM_NOT_ALLOWED,
                    subject=alias.name,
                    hint="Dotted imports must use an explicit alias, e.g. `import urllib.parse as urlparse`.",
                    node=node,
                )
                continue
            if alias.name not in spec.MODULES:
                self._add(
                    MODULE_NOT_ALLOWED,
                    subject=alias.name,
                    hint=f"`{alias.name}` is not in the allowed import registry.",
                    node=node,
                    message=f"Module `{alias.name}` is not allowed.",
                )
                continue
            name = alias.asname or alias.name
            scope.bind(
                name,
                Binding(
                    kind="module",
                    module=alias.name,
                    value_kind=spec.KIND_MODULE,
                ),
            )

    def _visit_import_from(
        self, node: ast.ImportFrom, scope: _Scope, *, is_module: bool
    ) -> None:
        if not is_module:
            self._add(
                FUNCTION_LOCATION_NOT_ALLOWED,
                subject="import",
                hint="Imports are only allowed at module top level.",
                node=node,
            )
            return
        if node.level:
            self._add(
                RELATIVE_IMPORT_NOT_ALLOWED,
                subject=".",
                hint="Relative imports are not allowed.",
                node=node,
            )
            return
        module = node.module or ""
        if module not in spec.MODULES:
            self._add(
                MODULE_NOT_ALLOWED,
                subject=module,
                hint=f"`{module}` is not in the allowed import registry.",
                node=node,
                message=f"Module `{module}` is not allowed.",
            )
            return
        for alias in node.names:
            if alias.name == "*":
                self._add(
                    WILDCARD_IMPORT_NOT_ALLOWED,
                    subject=module,
                    hint="Wildcard imports are not allowed.",
                    node=node,
                )
                continue
            if alias.name not in spec.MODULES[module]:
                self._add(
                    MODULE_MEMBER_NOT_ALLOWED,
                    subject=f"{module}.{alias.name}",
                    hint=f"`{module}.{alias.name}` is not exported by the allowed registry.",
                    node=node,
                    message=f"`{module}.{alias.name}` is not allowed.",
                )
                continue
            forbidden = spec.FORBIDDEN_MODULE_MEMBERS.get(module, frozenset())
            if alias.name in forbidden:
                self._add(
                    MODULE_MEMBER_NOT_ALLOWED,
                    subject=f"{module}.{alias.name}",
                    hint=f"`{module}.{alias.name}` is explicitly not allowed.",
                    node=node,
                    message=f"`{module}.{alias.name}` is not allowed.",
                )
                continue
            member_kind = spec.MODULES[module][alias.name]
            name = alias.asname or alias.name
            scope.bind(
                name,
                Binding(
                    kind="imported",
                    module=module,
                    member=alias.name,
                    member_kind=member_kind,
                    value_kind=spec.KIND_CALLABLE
                    if member_kind == "callable"
                    else spec.KIND_UNKNOWN,
                ),
            )

    def _visit_function_def(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        scope: _Scope,
        *,
        is_async: bool,
    ) -> None:
        if not scope.is_module:
            self._add(
                FUNCTION_LOCATION_NOT_ALLOWED,
                subject=node.name,
                hint="User functions must be defined at module top level.",
                node=node,
            )
            return
        if node.decorator_list:
            self._add(
                FUNCTION_SIGNATURE_NOT_ALLOWED,
                subject=node.name,
                hint="Decorators are not allowed.",
                node=node,
            )
        if node.returns is not None:
            self._add(
                FUNCTION_SIGNATURE_NOT_ALLOWED,
                subject=node.name,
                hint="Function annotations are not allowed.",
                node=node,
            )
        args = node.args
        if args.posonlyargs or args.vararg or args.kwarg or args.kwonlyargs:
            self._add(
                FUNCTION_SIGNATURE_NOT_ALLOWED,
                subject=node.name,
                hint="Only plain positional parameters are allowed (no *args, **kwargs, keyword-only or positional-only).",
                node=node,
            )
        for arg in args.args:
            if arg.annotation is not None:
                self._add(
                    FUNCTION_SIGNATURE_NOT_ALLOWED,
                    subject=node.name,
                    hint="Parameter annotations are not allowed.",
                    node=node,
                )
        for default in node.args.defaults:
            if not self._is_json_default(default):
                self._add(
                    DEFAULT_VALUE_NOT_JSON,
                    subject=node.name,
                    hint="Default values must be JSON scalar constants (str, int, float, bool or None).",
                    node=default,
                )
        if scope.lookup(node.name) is not None:
            self._add(
                NAME_REBIND_NOT_ALLOWED,
                subject=node.name,
                hint="Function names cannot be reused or rebound.",
                node=node,
            )
        func_scope = _Scope(parent=scope)
        for arg in args.args:
            func_scope.bind(
                arg.arg, Binding(kind="param", value_kind=spec.KIND_UNKNOWN)
            )
        self._visit_body(
            node.body,
            func_scope,
            parent=node,
            is_async_body=is_async,
            in_function=True,
        )
        scope.bind(
            node.name,
            Binding(
                kind="user_func",
                is_async=is_async,
                name=node.name,
                value_kind=(
                    spec.KIND_USER_ASYNC_FUNC if is_async else spec.KIND_USER_SYNC_FUNC
                ),
            ),
        )

    def _is_json_default(self, node: ast.AST) -> bool:
        if not isinstance(node, ast.Constant):
            return False
        value = node.value
        if value is None:
            return True
        return isinstance(value, (str, int, float, bool)) and not isinstance(
            value, bytes
        )

    def _visit_assign(
        self,
        node: ast.Assign,
        scope: _Scope,
        *,
        parent: ast.AST | None,
        is_async_body: bool,
    ) -> None:
        info = self._expr(node.value, scope, parent=node)
        self._check_non_escapeable(node.value, info, scope, parent=node)
        for target in node.targets:
            self._assign_target(target, scope, parent=node)

    def _visit_aug_assign(self, node: ast.AugAssign, scope: _Scope) -> None:
        if type(node.op).__name__ not in spec.ALLOWED_OPERATORS:
            self._add(
                NODE_NOT_ALLOWED,
                subject=f"op {type(node.op).__name__}",
                hint="This operator is not supported.",
                node=node,
            )
        if isinstance(node.target, ast.Name):
            self._check_rebindable(node.target.id, scope, node.target)
        elif isinstance(node.target, ast.Subscript):
            base_info = self._expr(node.target.value, scope, parent=node)
            if base_info.value_kind not in _SUBSCRIPTABLE_KINDS:
                self._add(
                    ASSIGNMENT_TARGET_NOT_ALLOWED,
                    subject=base_info.value_kind or "unknown",
                    hint="Subscript assignment target must be a list, dict, state, tuple or str-like value.",
                    node=node.target,
                )
            self._expr(node.target.slice, scope, parent=node)
        else:
            self._add(
                ASSIGNMENT_TARGET_NOT_ALLOWED,
                subject=type(node.target).__name__,
                hint="Augmented assignment targets must be names or subscripts.",
                node=node.target,
            )
        self._expr(node.value, scope, parent=node)

    def _assign_target(
        self, target: ast.AST, scope: _Scope, *, parent: ast.AST
    ) -> None:
        if isinstance(target, ast.Name):
            self._check_rebindable(target.id, scope, target)
            if scope.lookup(target.id) is None:
                scope.bind(target.id, Binding(kind="var", value_kind=spec.KIND_UNKNOWN))
            return
        if isinstance(target, (ast.Tuple, ast.List)) and not any(
            isinstance(elt, ast.Starred) for elt in target.elts
        ):
            for elt in target.elts:
                self._assign_target(elt, scope, parent=parent)
            return
        if isinstance(target, ast.Subscript):
            base_info = self._expr(target.value, scope, parent=parent)
            if base_info.value_kind not in _SUBSCRIPTABLE_KINDS:
                self._add(
                    ASSIGNMENT_TARGET_NOT_ALLOWED,
                    subject=base_info.value_kind or "unknown",
                    hint="Subscript assignment target must be a list, dict, state or similar container.",
                    node=target,
                )
            self._expr(target.slice, scope, parent=parent)
            return
        self._add(
            ASSIGNMENT_TARGET_NOT_ALLOWED,
            subject=type(target).__name__,
            hint="Assignment targets must be names, tuples/lists of names, or subscripts.",
            node=target,
        )

    def _visit_expr_stmt(self, node: ast.Expr, scope: _Scope) -> None:
        self._expr(node.value, scope, parent=node)

    def _visit_if(
        self,
        node: ast.If,
        scope: _Scope,
        *,
        parent: ast.AST,
        is_async_body: bool,
        in_loop: bool,
        in_function: bool,
    ) -> None:
        self._expr(node.test, scope, parent=node)
        self._visit_body(
            node.body,
            scope,
            parent=node,
            is_async_body=is_async_body,
            in_loop=in_loop,
            in_function=in_function,
        )
        self._visit_body(
            node.orelse,
            scope,
            parent=node,
            is_async_body=is_async_body,
            in_loop=in_loop,
            in_function=in_function,
        )

    def _visit_for(
        self,
        node: ast.For,
        scope: _Scope,
        *,
        parent: ast.AST,
        is_async_body: bool,
        in_function: bool,
    ) -> None:
        iter_info = self._expr(node.iter, scope, parent=node)
        if iter_info.value_kind not in _ITERABLE_KINDS:
            self._add(
                CALL_NOT_ALLOWED,
                subject="iteration",
                hint=f"Values of kind {iter_info.value_kind or 'unknown'} cannot be iterated.",
                node=node.iter,
                message="This value cannot be iterated.",
            )
        self._for_target(node.target, scope, parent=node)
        self._visit_body(
            node.body,
            scope,
            parent=node,
            is_async_body=is_async_body,
            in_loop=True,
            in_function=in_function,
        )
        self._visit_body(
            node.orelse,
            scope,
            parent=node,
            is_async_body=is_async_body,
            in_loop=False,
            in_function=in_function,
        )

    def _for_target(self, target: ast.AST, scope: _Scope, *, parent: ast.AST) -> None:
        if isinstance(target, ast.Name):
            self._check_rebindable(target.id, scope, target)
            if scope.lookup(target.id) is None:
                scope.bind(target.id, Binding(kind="var", value_kind=spec.KIND_UNKNOWN))
            return
        if isinstance(target, (ast.Tuple, ast.List)) and not any(
            isinstance(elt, ast.Starred) for elt in target.elts
        ):
            for elt in target.elts:
                self._for_target(elt, scope, parent=parent)
            return
        self._add(
            ASSIGNMENT_TARGET_NOT_ALLOWED,
            subject=type(target).__name__,
            hint="For-loop targets must be names or tuples/lists of names.",
            node=target,
        )

    def _visit_while(
        self,
        node: ast.While,
        scope: _Scope,
        *,
        parent: ast.AST,
        is_async_body: bool,
        in_function: bool,
    ) -> None:
        self._expr(node.test, scope, parent=node)
        self._visit_body(
            node.body,
            scope,
            parent=node,
            is_async_body=is_async_body,
            in_loop=True,
            in_function=in_function,
        )
        self._visit_body(
            node.orelse,
            scope,
            parent=node,
            is_async_body=is_async_body,
            in_loop=False,
            in_function=in_function,
        )

    def _visit_try(
        self,
        node: ast.Try,
        scope: _Scope,
        *,
        parent: ast.AST,
        is_async_body: bool,
        in_loop: bool,
        in_function: bool,
    ) -> None:
        self._visit_body(
            node.body,
            scope,
            parent=node,
            is_async_body=is_async_body,
            in_loop=in_loop,
            in_function=in_function,
        )
        for handler in node.handlers:
            if handler.type is not None:
                self._exception_type_expr(handler.type, scope, parent=node)
            else:
                # Bare except is allowed; it can only catch script exceptions.
                pass
            if handler.name:
                handler_scope = scope
                handler_scope.bind(
                    handler.name,
                    Binding(kind="var", value_kind=spec.KIND_EXCEPTION),
                )
                self._visit_body(
                    handler.body,
                    handler_scope,
                    parent=node,
                    is_async_body=is_async_body,
                    in_loop=in_loop,
                    in_function=in_function,
                )
            else:
                self._visit_body(
                    handler.body,
                    scope,
                    parent=node,
                    is_async_body=is_async_body,
                    in_loop=in_loop,
                    in_function=in_function,
                )
        self._visit_body(
            node.orelse,
            scope,
            parent=node,
            is_async_body=is_async_body,
            in_loop=in_loop,
            in_function=in_function,
        )
        self._visit_body(
            node.finalbody,
            scope,
            parent=node,
            is_async_body=is_async_body,
            in_loop=in_loop,
            in_function=in_function,
        )

    def _exception_type_expr(
        self, node: ast.AST, scope: _Scope, *, parent: ast.AST
    ) -> None:
        if isinstance(node, ast.Name):
            binding = self._resolve(node.id, scope)
            if binding is None:
                self._add(
                    NAME_NOT_DEFINED,
                    subject=node.id,
                    hint=f"`{node.id}` is not defined.",
                    node=node,
                )
                return
            if binding.kind != "exception_class" and not (
                binding.kind == "imported"
                and (binding.module, binding.member) in spec.ALLOWED_EXCEPTION_ALIASES
            ):
                self._add(
                    EXCEPTION_TYPE_NOT_ALLOWED,
                    subject=node.id,
                    hint=f"`{node.id}` is not an allowed catchable exception.",
                    node=node,
                )
            return
        if isinstance(node, ast.Tuple):
            for elt in node.elts:
                self._exception_type_expr(elt, scope, parent=parent)
            return
        self._add(
            EXCEPTION_TYPE_NOT_ALLOWED,
            subject=type(node).__name__,
            hint="Except types must be allowed exception names or a tuple of them.",
            node=node,
        )

    def _visit_raise(self, node: ast.Raise, scope: _Scope) -> None:
        if node.exc is None:
            return  # bare re-raise is allowed inside an except block
        if node.cause is not None:
            self._add(
                NODE_NOT_ALLOWED,
                subject="raise ... from ...",
                hint="`raise ... from ...` is not supported.",
                node=node,
            )
        if not isinstance(node.exc, ast.Call):
            self._add(
                EXCEPTION_TYPE_NOT_ALLOWED,
                subject=type(node.exc).__name__,
                hint="Raise targets must be allowed exception constructors, e.g. `raise ValueError(...)`.",
                node=node.exc,
            )
            return
        func = node.exc.func
        if isinstance(func, ast.Name):
            binding = self._resolve(func.id, scope)
            if binding is None:
                if func.id in _KNOWN_UNALLOWED_EXCEPTIONS:
                    self._add(
                        EXCEPTION_TYPE_NOT_ALLOWED,
                        subject=func.id,
                        hint=f"`{func.id}` is not an allowed exception.",
                        node=func,
                    )
                else:
                    self._add(
                        NAME_NOT_DEFINED,
                        subject=func.id,
                        hint=f"`{func.id}` is not defined.",
                        node=func,
                    )
            elif binding.kind != "exception_class":
                self._add(
                    EXCEPTION_TYPE_NOT_ALLOWED,
                    subject=func.id,
                    hint=f"`{func.id}` is not an allowed exception.",
                    node=func,
                )
            for arg in node.exc.args:
                self._expr(arg, scope, parent=node)
            for kw in node.exc.keywords:
                self._expr(kw.value, scope, parent=node)
        else:
            self._add(
                EXCEPTION_TYPE_NOT_ALLOWED,
                subject=type(func).__name__,
                hint="Raise targets must be simple allowed exception names.",
                node=func,
            )

    # ------------------------------------------------------------------
    # Expression dispatch
    # ------------------------------------------------------------------

    def _expr(
        self,
        node: ast.AST,
        scope: _Scope,
        *,
        parent: ast.AST | None,
        in_async_ctx: bool = False,
    ) -> KindInfo:
        node_type = type(node).__name__
        if node_type not in spec.ALLOWED_EXPRESSIONS:
            self._reject_node(node, node_type)
            return KindInfo.unknown()
        if node_type == "Constant":
            return self._constant(node)
        if node_type == "Name":
            return self._name(node, scope)
        if node_type in ("List", "Tuple", "Set", "Dict"):
            return self._container(node, scope, parent=parent)
        if node_type == "BinOp":
            return self._binop(node, scope, parent=parent)
        if node_type == "UnaryOp":
            return self._unaryop(node, scope, parent=parent)
        if node_type == "BoolOp":
            return self._boolop(node, scope, parent=parent)
        if node_type == "Compare":
            return self._compare(node, scope, parent=parent)
        if node_type == "IfExp":
            self._expr(node.test, scope, parent=node)
            self._expr(node.body, scope, parent=node)
            self._expr(node.orelse, scope, parent=node)
            return KindInfo.unknown()
        if node_type == "Subscript":
            return self._subscript(node, scope, parent=parent)
        if node_type == "Slice":
            if node.lower is not None:
                self._expr(node.lower, scope, parent=node)
            if node.upper is not None:
                self._expr(node.upper, scope, parent=node)
            if node.step is not None:
                self._expr(node.step, scope, parent=node)
            return KindInfo.unknown()
        if node_type == "Attribute":
            return self._attribute(node, scope, parent=parent)
        if node_type == "Call":
            return self._call(node, scope, parent=parent)
        if node_type == "Await":
            return self._await(node, scope, parent=parent)
        if node_type in ("ListComp", "SetComp", "DictComp"):
            return self._comprehension(node, scope, parent=parent, node_type=node_type)
        if node_type == "JoinedStr":
            return self._joined_str(node, scope, parent=parent)
        if node_type == "FormattedValue":
            self._expr(node.value, scope, parent=node)
            if node.format_spec is not None:
                self._expr(node.format_spec, scope, parent=node)
            return KindInfo.unknown()
        return KindInfo.unknown()

    def _constant(self, node: ast.Constant) -> KindInfo:
        value = node.value
        if value is None:
            kind = spec.KIND_NONE
        elif isinstance(value, bool):
            kind = spec.KIND_BOOL
        elif isinstance(value, int):
            kind = spec.KIND_INT
        elif isinstance(value, float):
            kind = spec.KIND_FLOAT
        elif isinstance(value, str):
            kind = spec.KIND_STR
        elif isinstance(value, bytes):
            kind = spec.KIND_BYTES
        else:
            self._add(
                NODE_NOT_ALLOWED,
                subject=f"constant {type(value).__name__}",
                hint="This constant type is not supported.",
                node=node,
            )
            return KindInfo.unknown()
        return KindInfo(value_kind=kind)

    def _name(self, node: ast.Name, scope: _Scope) -> KindInfo:
        if node.id in spec.FORBIDDEN_BUILTINS:
            self._add(
                BUILTIN_NOT_ALLOWED,
                subject=node.id,
                hint=f"`{node.id}` is not available in {self.language_version}.",
                node=node,
                message=f"`{node.id}` is not available.",
            )
            return KindInfo.unknown(tainted=True)
        binding = self._resolve(node.id, scope)
        if binding is None:
            self._add(
                NAME_NOT_DEFINED,
                subject=node.id,
                hint=f"`{node.id}` is not defined in this scope.",
                node=node,
                message=f"Name `{node.id}` is not defined.",
            )
            return KindInfo.unknown(tainted=True)
        if binding.kind == "builtin":
            return KindInfo(
                value_kind=spec.KIND_CALLABLE, binding=binding, escapeable=True
            )
        if binding.kind == "module":
            return KindInfo(
                value_kind=spec.KIND_MODULE, module=binding.module, escapeable=True
            )
        if binding.kind == "imported":
            return KindInfo(
                value_kind=spec.KIND_CALLABLE
                if binding.member_kind == "callable"
                else binding.value_kind,
                binding=binding,
                module=binding.module,
                member=binding.member,
                member_kind=binding.member_kind,
                escapeable=binding.member_kind in ("callable", "class"),
            )
        if binding.kind == "user_func":
            return KindInfo(
                value_kind=binding.value_kind,
                binding=binding,
                escapeable=True,
            )
        if binding.kind == "exception_class":
            return KindInfo(
                value_kind=spec.KIND_CLASS, binding=binding, escapeable=True
            )
        return KindInfo(value_kind=binding.value_kind)

    def _container(
        self,
        node: ast.AST,
        scope: _Scope,
        *,
        parent: ast.AST | None,
    ) -> KindInfo:
        node_type = type(node).__name__
        if node_type in ("List", "Tuple", "Set"):
            for elt in node.elts:
                info = self._expr(elt, scope, parent=node)
                self._check_non_escapeable(elt, info, scope, parent=node)
            kind = {
                "List": spec.KIND_LIST,
                "Tuple": spec.KIND_TUPLE,
                "Set": spec.KIND_SET,
            }[node_type]
            return KindInfo(value_kind=kind)
        for key_node, value_node in zip(node.keys, node.values):
            key_info = self._expr(key_node, scope, parent=node)
            self._check_non_escapeable(key_node, key_info, scope, parent=node)
            value_info = self._expr(value_node, scope, parent=node)
            self._check_non_escapeable(value_node, value_info, scope, parent=node)
        return KindInfo(value_kind=spec.KIND_DICT)

    def _check_non_escapeable(
        self,
        node: ast.AST,
        info: KindInfo,
        scope: _Scope,
        *,
        parent: ast.AST | None,
    ) -> None:
        if info is None or info.tainted:
            return
        if info.escapeable:
            if info.binding and info.binding.kind == "builtin":
                self._add(
                    FUNCTION_VALUE_NOT_ALLOWED,
                    subject="builtin",
                    hint="Builtins must be called directly and cannot be stored, passed or returned.",
                    node=node,
                    message="Builtin cannot be used as a value.",
                )
            elif info.module and info.member:
                self._add(
                    FUNCTION_VALUE_NOT_ALLOWED,
                    subject=f"{info.module}.{info.member}",
                    hint="Imported functions must be called directly and cannot be stored, passed or returned.",
                    node=node,
                    message=f"{info.module}.{info.member} cannot be used as a value.",
                )
            elif info.binding and info.binding.kind == "module":
                self._add(
                    CAPABILITY_VALUE_NOT_ALLOWED,
                    subject=info.module or "module",
                    hint="Module objects cannot be stored, passed or returned; call their members directly.",
                    node=node,
                )
            elif info.binding and info.binding.kind == "user_func":
                self._add(
                    FUNCTION_VALUE_NOT_ALLOWED,
                    subject="user function",
                    hint="User functions must be called directly and cannot be stored, passed or returned.",
                    node=node,
                )
            elif info.value_kind in (
                spec.KIND_CTX,
                spec.KIND_CTX_HTTP,
                spec.KIND_CTX_RUN,
                spec.KIND_CTX_STATE,
            ) or (info.value_kind == spec.KIND_STATE and info.escapeable):
                self._add(
                    CAPABILITY_VALUE_NOT_ALLOWED,
                    subject="ctx.state"
                    if info.value_kind == spec.KIND_STATE
                    else info.value_kind,
                    hint="ctx capabilities must be used inline and cannot be stored, passed or returned.",
                    node=node,
                )
            else:
                self._add(
                    FUNCTION_VALUE_NOT_ALLOWED,
                    subject=info.value_kind or "callable",
                    hint="Callables and capabilities cannot be stored, passed or returned.",
                    node=node,
                )
            self.collector.suppress()

    def _binop(
        self,
        node: ast.BinOp,
        scope: _Scope,
        *,
        parent: ast.AST | None,
    ) -> KindInfo:
        if type(node.op).__name__ not in spec.ALLOWED_OPERATORS:
            self._add(
                NODE_NOT_ALLOWED,
                subject=f"op {type(node.op).__name__}",
                hint="This binary operator is not supported.",
                node=node,
            )
        left = self._expr(node.left, scope, parent=node)
        right = self._expr(node.right, scope, parent=node)
        self._check_non_escapeable(node.left, left, scope, parent=node)
        self._check_non_escapeable(node.right, right, scope, parent=node)
        if left.value_kind == spec.KIND_STR and right.value_kind == spec.KIND_STR:
            return KindInfo(value_kind=spec.KIND_STR)
        if (
            left.value_kind in _JSON_SCALAR_KINDS
            and right.value_kind in _JSON_SCALAR_KINDS
        ):
            return KindInfo(value_kind=spec.KIND_UNKNOWN)
        if left.value_kind in (
            spec.KIND_LIST,
            spec.KIND_TUPLE,
        ) and right.value_kind in (
            spec.KIND_LIST,
            spec.KIND_TUPLE,
        ):
            return KindInfo(value_kind=left.value_kind)
        return KindInfo.unknown()

    def _unaryop(
        self,
        node: ast.UnaryOp,
        scope: _Scope,
        *,
        parent: ast.AST | None,
    ) -> KindInfo:
        if type(node.op).__name__ not in spec.ALLOWED_OPERATORS:
            self._add(
                NODE_NOT_ALLOWED,
                subject=f"op {type(node.op).__name__}",
                hint="This unary operator is not supported.",
                node=node,
            )
        operand = self._expr(node.operand, scope, parent=node)
        self._check_non_escapeable(node.operand, operand, scope, parent=node)
        return KindInfo(
            value_kind=spec.KIND_BOOL
            if type(node.op).__name__ == "Not"
            else spec.KIND_UNKNOWN
        )

    def _boolop(
        self,
        node: ast.BoolOp,
        scope: _Scope,
        *,
        parent: ast.AST | None,
    ) -> KindInfo:
        for value in node.values:
            info = self._expr(value, scope, parent=node)
            self._check_non_escapeable(value, info, scope, parent=node)
        return KindInfo(value_kind=spec.KIND_BOOL)

    def _compare(
        self,
        node: ast.Compare,
        scope: _Scope,
        *,
        parent: ast.AST | None,
    ) -> KindInfo:
        self._expr(node.left, scope, parent=node)
        for op in node.ops:
            if type(op).__name__ not in spec.ALLOWED_OPERATORS:
                self._add(
                    NODE_NOT_ALLOWED,
                    subject=f"op {type(op).__name__}",
                    hint="This comparison operator is not supported.",
                    node=node,
                )
        for comparator in node.comparators:
            info = self._expr(comparator, scope, parent=node)
            self._check_non_escapeable(comparator, info, scope, parent=node)
        return KindInfo(value_kind=spec.KIND_BOOL)

    def _subscript(
        self,
        node: ast.Subscript,
        scope: _Scope,
        *,
        parent: ast.AST | None,
    ) -> KindInfo:
        base = self._expr(node.value, scope, parent=node)
        if base.value_kind not in _SUBSCRIPTABLE_KINDS:
            self._add(
                CALL_NOT_ALLOWED,
                subject="subscript",
                hint=f"Values of kind {base.value_kind or 'unknown'} cannot be subscripted.",
                node=node,
                message="This value cannot be subscripted.",
            )
        self._expr(node.slice, scope, parent=node)
        if base.value_kind == spec.KIND_DICT:
            return KindInfo.unknown()
        if base.value_kind == spec.KIND_STRUCT_TIME:
            return KindInfo(value_kind=spec.KIND_INT)
        if base.value_kind in (
            spec.KIND_STR,
            spec.KIND_BYTES,
            spec.KIND_LIST,
            spec.KIND_TUPLE,
        ):
            return KindInfo(
                value_kind=spec.KIND_STR
                if base.value_kind == spec.KIND_STR
                else spec.KIND_UNKNOWN
            )
        return KindInfo.unknown()

    def _attribute(
        self,
        node: ast.Attribute,
        scope: _Scope,
        *,
        parent: ast.AST | None,
    ) -> KindInfo:
        base = self._expr(node.value, scope, parent=node)
        if base.tainted:
            return KindInfo.unknown(tainted=True)
        attr = node.attr
        # ctx special capabilities
        if base.value_kind == spec.KIND_CTX:
            if attr not in spec.METHODS[spec.KIND_CTX]:
                self._add(
                    ATTRIBUTE_NOT_ALLOWED,
                    subject=f"ctx.{attr}",
                    hint=f"`ctx.{attr}` is not available.",
                    node=node,
                    message=f"`ctx.{attr}` is not available.",
                )
                return KindInfo.unknown()
            if attr == "http":
                return KindInfo(value_kind=spec.KIND_CTX_HTTP, escapeable=True)
            if attr == "run":
                return KindInfo(value_kind=spec.KIND_CTX_RUN, escapeable=True)
            if attr == "state":
                return KindInfo(value_kind=spec.KIND_STATE, escapeable=True)
            if attr == "send_text":
                return KindInfo(
                    value_kind=spec.KIND_SEND_TEXT,
                    escapeable=True,
                    async_capability=True,
                )
            return KindInfo.unknown()
        if base.value_kind == spec.KIND_CTX_HTTP:
            if attr != "request":
                self._add(
                    ATTRIBUTE_NOT_ALLOWED,
                    subject=f"ctx.http.{attr}",
                    hint="Only `ctx.http.request` is available.",
                    node=node,
                )
                return KindInfo.unknown()
            return KindInfo(
                value_kind=spec.KIND_REQUEST, escapeable=True, async_capability=True
            )
        if base.value_kind == spec.KIND_CTX_RUN:
            if attr not in spec.METHODS[spec.KIND_CTX_RUN]:
                self._add(
                    ATTRIBUTE_NOT_ALLOWED,
                    subject=f"ctx.run.{attr}",
                    hint=f"`ctx.run.{attr}` is not available.",
                    node=node,
                )
                return KindInfo.unknown()
            return KindInfo(
                value_kind=spec.KIND_STR if attr != "started_at" else spec.KIND_UNKNOWN
            )
        if base.value_kind == spec.KIND_CTX_STATE:
            return KindInfo(value_kind=spec.KIND_STATE, escapeable=True)
        if base.value_kind == spec.KIND_HTTP_RESPONSE:
            if attr not in spec.METHODS[spec.KIND_HTTP_RESPONSE]:
                self._add(
                    ATTRIBUTE_NOT_ALLOWED,
                    subject=f"response.{attr}",
                    hint=f"`response.{attr}` is not available.",
                    node=node,
                )
                return KindInfo.unknown()
            if attr == "json":
                return KindInfo(value_kind=spec.KIND_CALLABLE, escapeable=True)
            if attr in ("status",):
                return KindInfo(value_kind=spec.KIND_INT)
            if attr in ("text", "url"):
                return KindInfo(value_kind=spec.KIND_STR)
            if attr == "headers":
                return KindInfo(value_kind=spec.KIND_HEADERS)
            return KindInfo.unknown()
        if base.value_kind == spec.KIND_HEADERS:
            if attr not in spec.METHODS[spec.KIND_HEADERS]:
                self._add(
                    ATTRIBUTE_NOT_ALLOWED,
                    subject=f"headers.{attr}",
                    hint=f"`headers.{attr}` is not available.",
                    node=node,
                )
                return KindInfo.unknown()
            return KindInfo(value_kind=spec.KIND_CALLABLE, escapeable=True)
        if base.value_kind == spec.KIND_MODULE:
            module = base.module or ""
            allowed = spec.MODULES.get(module, {})
            forbidden = spec.FORBIDDEN_MODULE_MEMBERS.get(module, frozenset())
            if attr not in allowed:
                if attr in forbidden:
                    hint = f"`{module}.{attr}` is explicitly not allowed."
                else:
                    hint = f"`{module}.{attr}` is not exported by the allowed registry."
                self._add(
                    MODULE_MEMBER_NOT_ALLOWED,
                    subject=f"{module}.{attr}",
                    hint=hint,
                    node=node,
                    message=f"`{module}.{attr}` is not allowed.",
                )
                return KindInfo.unknown(tainted=True)
            member_kind = allowed[attr]
            if member_kind == "callable":
                return KindInfo(
                    value_kind=spec.KIND_CALLABLE,
                    module=module,
                    member=attr,
                    member_kind="callable",
                    escapeable=True,
                )
            if member_kind == "class":
                return KindInfo(
                    value_kind=spec.KIND_CLASS,
                    module=module,
                    member=attr,
                    member_kind="class",
                    escapeable=True,
                )
            if member_kind == "const":
                return KindInfo(value_kind=spec.KIND_UNKNOWN)
            return KindInfo(value_kind=spec.KIND_UNKNOWN)
        if base.value_kind == spec.KIND_CLASS:
            class_name = base.member or ""
            allowed_class_members = spec.CLASS_MEMBERS.get(class_name, frozenset())
            if attr not in allowed_class_members:
                self._add(
                    ATTRIBUTE_NOT_ALLOWED,
                    subject=f"{class_name}.{attr}",
                    hint=f"`{class_name}.{attr}` is not available.",
                    node=node,
                )
                return KindInfo.unknown()
            return KindInfo(
                value_kind=spec.KIND_CALLABLE,
                escapeable=True,
                class_member=(class_name, attr),
            )
        if base.value_kind in (spec.KIND_DIGEST, spec.KIND_HMAC):
            if attr not in spec.METHODS[base.value_kind]:
                self._add(
                    ATTRIBUTE_NOT_ALLOWED,
                    subject=f"digest.{attr}",
                    hint=f"`{attr}` is not available on a digest.",
                    node=node,
                )
                return KindInfo.unknown()
            if attr in ("update", "digest", "hexdigest", "copy"):
                return KindInfo(
                    value_kind=spec.KIND_CALLABLE,
                    escapeable=True,
                    receiver_kind=base.value_kind,
                    method_name=attr,
                )
            return KindInfo(value_kind=spec.KIND_UNKNOWN)
        if base.value_kind == spec.KIND_REGEX_PATTERN:
            allowed_members = spec.METHODS[spec.KIND_REGEX_PATTERN]
            if attr not in allowed_members:
                self._add(
                    ATTRIBUTE_NOT_ALLOWED,
                    subject=f"pattern.{attr}",
                    hint=f"`{attr}` is not available on a regex pattern.",
                    node=node,
                )
                return KindInfo.unknown()
            if attr in (
                "findall",
                "finditer",
                "fullmatch",
                "match",
                "search",
                "split",
                "sub",
                "subn",
            ):
                return KindInfo(
                    value_kind=spec.KIND_CALLABLE,
                    escapeable=True,
                    receiver_kind=spec.KIND_REGEX_PATTERN,
                    method_name=attr,
                )
            return KindInfo.unknown()
        if base.value_kind == spec.KIND_REGEX_MATCH:
            allowed_members = spec.METHODS[spec.KIND_REGEX_MATCH]
            if attr not in allowed_members:
                self._add(
                    ATTRIBUTE_NOT_ALLOWED,
                    subject=f"match.{attr}",
                    hint=f"`{attr}` is not available on a regex match.",
                    node=node,
                )
                return KindInfo.unknown()
            if attr in (
                "group",
                "groups",
                "groupdict",
                "start",
                "end",
                "span",
                "expand",
            ):
                return KindInfo(
                    value_kind=spec.KIND_CALLABLE,
                    escapeable=True,
                    receiver_kind=spec.KIND_REGEX_MATCH,
                    method_name=attr,
                )
            return KindInfo.unknown()
        if base.value_kind == spec.KIND_URL_RESULT:
            if attr not in spec.METHODS[spec.KIND_URL_RESULT]:
                self._add(
                    ATTRIBUTE_NOT_ALLOWED,
                    subject=f"url.{attr}",
                    hint=f"`{attr}` is not available on a parsed URL.",
                    node=node,
                )
                return KindInfo.unknown()
            return KindInfo(
                value_kind=spec.KIND_STR if attr not in ("port",) else spec.KIND_UNKNOWN
            )
        if base.value_kind == spec.KIND_STRUCT_TIME:
            if attr not in spec.METHODS[spec.KIND_STRUCT_TIME]:
                self._add(
                    ATTRIBUTE_NOT_ALLOWED,
                    subject=f"st.{attr}",
                    hint=f"`{attr}` is not available on struct_time.",
                    node=node,
                )
                return KindInfo.unknown()
            return KindInfo(value_kind=spec.KIND_INT)
        if base.value_kind == spec.KIND_EXCEPTION:
            if attr != "args":
                self._add(
                    ATTRIBUTE_NOT_ALLOWED,
                    subject=f"exc.{attr}",
                    hint=f"`{attr}` is not available on an exception.",
                    node=node,
                )
                return KindInfo.unknown()
            return KindInfo(value_kind=spec.KIND_TUPLE)
        if base.value_kind == spec.KIND_STATE:
            if attr not in spec.METHODS[spec.KIND_STATE]:
                self._add(
                    ATTRIBUTE_NOT_ALLOWED,
                    subject=f"state.{attr}",
                    hint=f"`{attr}` is not available on state.",
                    node=node,
                )
                return KindInfo.unknown()
            return KindInfo(
                value_kind=spec.KIND_CALLABLE,
                escapeable=True,
                receiver_kind=spec.KIND_STATE,
                method_name=attr,
            )
        # Dynamic safe values: allow the union of all safe methods.
        if base.value_kind in spec.METHODS:
            allowed = spec.METHODS[base.value_kind]
            if attr in allowed:
                return KindInfo(
                    value_kind=spec.KIND_CALLABLE,
                    escapeable=True,
                    receiver_kind=base.value_kind,
                    method_name=attr,
                )
            self._add(
                ATTRIBUTE_NOT_ALLOWED,
                subject=f"{base.value_kind}.{attr}",
                hint=f"`{attr}` is not available on {base.value_kind} values.",
                node=node,
            )
            return KindInfo.unknown()
        # Unknown safe value: allow any member name in the global method union.
        union = set()
        for members in spec.METHODS.values():
            union.update(members)
        if attr in union:
            return KindInfo(
                value_kind=spec.KIND_CALLABLE,
                escapeable=True,
                receiver_kind=base.value_kind,
                method_name=attr,
            )
        self._add(
            ATTRIBUTE_NOT_ALLOWED,
            subject=attr,
            hint=f"`{attr}` is not a known safe method.",
            node=node,
        )
        return KindInfo.unknown()

    def _call(
        self,
        node: ast.Call,
        scope: _Scope,
        *,
        parent: ast.AST | None,
    ) -> KindInfo:
        func = node.func
        callee = self._expr(func, scope, parent=node)
        if callee.tainted:
            return KindInfo.unknown(tainted=True)

        for arg in node.args:
            info = self._expr(arg, scope, parent=node)
            self._check_non_escapeable(arg, info, scope, parent=node)
        for kw in node.keywords:
            if kw.arg is None:
                self._add(
                    CALL_SIGNATURE_INVALID,
                    subject="**kwargs",
                    hint="`**kwargs` expansion is not supported.",
                    node=node,
                )
            else:
                info = self._expr(kw.value, scope, parent=node)
                self._check_non_escapeable(kw.value, info, scope, parent=node)

        async_capability = callee.async_capability or (
            callee.binding is not None
            and callee.binding.kind == "user_func"
            and callee.binding.is_async
        )
        is_await_parent = isinstance(parent, ast.Await) and parent.value is node
        if async_capability and not is_await_parent:
            self._add(
                ASYNC_CALL_REQUIRES_AWAIT,
                subject=self._callee_name(callee),
                hint="Async capabilities and async helpers must be awaited directly, e.g. `await ctx.http.request(...)`.",
                node=node,
                message=f"`{self._callee_name(callee)}` must be awaited directly.",
            )
        if (
            callee.value_kind
            not in (
                spec.KIND_CALLABLE,
                spec.KIND_CLASS,
                spec.KIND_USER_SYNC_FUNC,
                spec.KIND_USER_ASYNC_FUNC,
                spec.KIND_SEND_TEXT,
                spec.KIND_REQUEST,
            )
            and callee.binding is None
            and not (callee.module and callee.member)
        ):
            if not callee.tainted:
                self._add(
                    CALL_NOT_ALLOWED,
                    subject=self._callee_name(callee) or type(func).__name__,
                    hint="This value cannot be called.",
                    node=func,
                    message="This value cannot be called.",
                )
            return KindInfo.unknown()

        signature_key = self._signature_key(callee, func, scope)
        if signature_key is not None:
            self._check_signature(node, signature_key)
        return KindInfo(value_kind=self._call_return_kind(callee))

    def _call_return_kind(self, info: KindInfo) -> str | None:
        if info.class_member:
            class_name, attr = info.class_member
            mapping = {
                ("date", "today"): spec.KIND_DATE,
                ("date", "fromtimestamp"): spec.KIND_DATE,
                ("date", "fromordinal"): spec.KIND_DATE,
                ("date", "fromisoformat"): spec.KIND_DATE,
                ("datetime", "now"): spec.KIND_DATETIME,
                ("datetime", "fromtimestamp"): spec.KIND_DATETIME,
                ("datetime", "fromisoformat"): spec.KIND_DATETIME,
                ("datetime", "strptime"): spec.KIND_DATETIME,
                ("datetime", "combine"): spec.KIND_DATETIME,
                ("time", "fromisoformat"): spec.KIND_TIME,
                ("timezone", "utc"): spec.KIND_TIMEZONE,
            }
            return mapping.get((class_name, attr))
        if info.module and info.member:
            key = (info.module, info.member)
            module_kinds = {
                ("datetime", "date"): spec.KIND_DATE,
                ("datetime", "datetime"): spec.KIND_DATETIME,
                ("datetime", "time"): spec.KIND_TIME,
                ("datetime", "timedelta"): spec.KIND_TIMEDELTA,
                ("datetime", "timezone"): spec.KIND_TIMEZONE,
                ("zoneinfo", "ZoneInfo"): spec.KIND_ZONEINFO,
                ("decimal", "Decimal"): spec.KIND_DECIMAL,
                ("re", "compile"): spec.KIND_REGEX_PATTERN,
                ("re", "fullmatch"): spec.KIND_REGEX_MATCH,
                ("re", "match"): spec.KIND_REGEX_MATCH,
                ("re", "search"): spec.KIND_REGEX_MATCH,
                ("urllib.parse", "urlparse"): spec.KIND_URL_RESULT,
                ("urllib.parse", "urlsplit"): spec.KIND_URL_RESULT,
            }
            if key in module_kinds:
                return module_kinds[key]
            if info.module == "hashlib":
                return spec.KIND_DIGEST
            if info.module == "hmac" and info.member == "new":
                return spec.KIND_HMAC
            return spec.KIND_UNKNOWN
        if info.receiver_kind and info.method_name:
            return self._method_return_kind(info.receiver_kind, info.method_name)
        return spec.KIND_UNKNOWN

    def _method_return_kind(self, kind: str, method: str) -> str | None:
        if kind == spec.KIND_STR:
            if method == "encode":
                return spec.KIND_BYTES
            if method in ("count", "find", "index", "rfind", "rindex"):
                return spec.KIND_INT
            if method in (
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
                "endswith",
                "startswith",
            ):
                return spec.KIND_BOOL
            if method in ("split", "rsplit", "splitlines"):
                return spec.KIND_LIST
            if method in ("partition", "rpartition"):
                return spec.KIND_TUPLE
            return spec.KIND_STR
        if kind == spec.KIND_BYTES:
            if method == "decode":
                return spec.KIND_STR
            if method in ("count", "find", "index", "rfind", "rindex"):
                return spec.KIND_INT
            if method in ("endswith", "startswith"):
                return spec.KIND_BOOL
            if method in ("split", "rsplit", "splitlines"):
                return spec.KIND_LIST
            if method in ("partition", "rpartition"):
                return spec.KIND_TUPLE
            return spec.KIND_BYTES
        if kind in (spec.KIND_LIST, spec.KIND_TUPLE):
            if method == "copy":
                return kind
            if method in ("count", "index"):
                return spec.KIND_INT
            if method in (
                "append",
                "extend",
                "insert",
                "remove",
                "reverse",
                "sort",
                "clear",
            ):
                return spec.KIND_NONE
            return spec.KIND_UNKNOWN
        if kind in (spec.KIND_DICT, spec.KIND_STATE):
            if method == "copy":
                return kind
            if method == "clear":
                return spec.KIND_NONE
            if method in (
                "keys",
                "values",
                "items",
                "get",
                "pop",
                "popitem",
                "setdefault",
                "update",
            ):
                return spec.KIND_UNKNOWN
        if kind == spec.KIND_SET:
            if method in (
                "copy",
                "difference",
                "intersection",
                "symmetric_difference",
                "union",
            ):
                return spec.KIND_SET
            if method in ("isdisjoint", "issubset", "issuperset"):
                return spec.KIND_BOOL
            if method in (
                "add",
                "clear",
                "discard",
                "remove",
                "difference_update",
                "intersection_update",
                "symmetric_difference_update",
                "update",
            ):
                return spec.KIND_NONE
            return spec.KIND_UNKNOWN
        if kind == spec.KIND_INT:
            if method == "as_integer_ratio":
                return spec.KIND_TUPLE
            if method in ("bit_count", "bit_length"):
                return spec.KIND_INT
            if method == "to_bytes":
                return spec.KIND_BYTES
        if kind == spec.KIND_FLOAT:
            if method == "as_integer_ratio":
                return spec.KIND_TUPLE
            if method == "hex":
                return spec.KIND_STR
            if method == "is_integer":
                return spec.KIND_BOOL
        if kind in (spec.KIND_DATETIME, spec.KIND_DATE, spec.KIND_TIME):
            if method in ("isoformat", "strftime", "ctime"):
                return spec.KIND_STR
            if method in ("weekday", "isoweekday", "toordinal"):
                return spec.KIND_INT
            if method in ("astimezone", "replace", "fromutc"):
                return kind
            if method == "timestamp":
                return spec.KIND_FLOAT
            return spec.KIND_UNKNOWN
        if kind == spec.KIND_TIMEDELTA and method == "total_seconds":
            return spec.KIND_FLOAT
        if kind == spec.KIND_DECIMAL:
            if method == "adjusted":
                return spec.KIND_INT
            if method == "to_eng_string":
                return spec.KIND_STR
            if method in (
                "is_finite",
                "is_infinite",
                "is_nan",
                "is_normal",
                "is_qnan",
                "is_signed",
                "is_snan",
                "is_subnormal",
                "is_zero",
            ):
                return spec.KIND_BOOL
            if method in (
                "copy_abs",
                "copy_negate",
                "copy_sign",
                "normalize",
                "quantize",
                "sqrt",
                "to_integral",
                "to_integral_exact",
                "to_integral_value",
                "canonical",
            ):
                return spec.KIND_DECIMAL
        if kind == spec.KIND_REGEX_PATTERN:
            if method in ("match", "fullmatch", "search"):
                return spec.KIND_REGEX_MATCH
            if method in ("findall", "split"):
                return spec.KIND_LIST
            if method == "finditer":
                return spec.KIND_UNKNOWN
            if method == "sub":
                return spec.KIND_STR
            if method == "subn":
                return spec.KIND_TUPLE
        if kind == spec.KIND_REGEX_MATCH:
            if method in ("start", "end", "lastindex", "pos", "endpos"):
                return spec.KIND_INT
            if method in ("span", "groups"):
                return spec.KIND_TUPLE
            if method == "groupdict":
                return spec.KIND_DICT
            if method in ("expand", "group"):
                return spec.KIND_STR
        if kind in (spec.KIND_DIGEST, spec.KIND_HMAC):
            if method == "digest":
                return spec.KIND_BYTES
            if method == "hexdigest":
                return spec.KIND_STR
            if method == "copy":
                return kind
            if method == "update":
                return spec.KIND_NONE
        return spec.KIND_UNKNOWN

    def _callee_name(self, info: KindInfo) -> str:
        if info.module and info.member:
            return f"{info.module}.{info.member}"
        if info.binding and info.binding.kind == "user_func":
            return info.binding.name or "user_function"
        if info.value_kind == spec.KIND_REQUEST:
            return "ctx.http.request"
        if info.value_kind == spec.KIND_SEND_TEXT:
            return "ctx.send_text"
        return info.value_kind or "callable"

    def _signature_key(
        self, info: KindInfo, func: ast.AST, scope: _Scope
    ) -> tuple[str, str] | None:
        if info.binding and info.binding.kind == "builtin":
            return ("builtin", info.binding.module or self._name_of(func))
        if info.module and info.member:
            if (info.module, info.member) in spec.ALLOWED_EXCEPTION_ALIASES:
                return None
            return (info.module, info.member)
        if info.value_kind == spec.KIND_REQUEST:
            return ("ctx_http", "request")
        if info.value_kind == spec.KIND_SEND_TEXT:
            return ("ctx", "send_text")
        if info.value_kind == spec.KIND_CLASS and info.member:
            return (info.member or "", "construct")
        return None

    def _name_of(self, node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        return ""

    def _check_signature(self, node: ast.Call, key: tuple[str, str]) -> None:
        signature = spec.allowed_signature(key)
        if signature is None:
            return
        min_args, max_args, allowed_kwargs = signature
        positional = len(node.args)
        kw_names = [kw.arg for kw in node.keywords if kw.arg is not None]
        if positional < min_args or (max_args is not None and positional > max_args):
            hint = (
                f"Expected {min_args}"
                + (f"..{max_args}" if max_args is not None else "+")
                + " positional arguments."
            )
            self._add(
                CALL_SIGNATURE_INVALID,
                subject=".".join(key),
                hint=hint,
                node=node,
                message=f"{'.'.join(key)} called with {positional} positional arguments.",
            )
        for name in kw_names:
            if allowed_kwargs and name not in allowed_kwargs:
                self._add(
                    CALL_SIGNATURE_INVALID,
                    subject=f"{'.'.join(key)}.{name}",
                    hint=f"Keyword `{name}` is not accepted by {'.'.join(key)}.",
                    node=node,
                    message=f"Keyword `{name}` is not accepted.",
                )

    def _await(
        self,
        node: ast.Await,
        scope: _Scope,
        *,
        parent: ast.AST | None,
    ) -> KindInfo:
        if not self._await_allowed_stack:
            self._add(
                AWAIT_TARGET_NOT_ALLOWED,
                subject="await",
                hint="`await` is only allowed at module top level or inside an async helper.",
                node=node,
            )
            self._expr(node.value, scope, parent=node)
            return KindInfo.unknown()
        target = node.value
        if not isinstance(target, ast.Call):
            self._add(
                AWAIT_TARGET_NOT_ALLOWED,
                subject=type(target).__name__,
                hint="`await` must wrap a direct call to ctx.http.request, ctx.send_text or an async helper.",
                node=node,
            )
            self._expr(target, scope, parent=node)
            return KindInfo.unknown()
        callee = self._expr(target.func, scope, parent=node)
        is_async = callee.async_capability or (
            callee.binding is not None
            and callee.binding.kind == "user_func"
            and callee.binding.is_async
        )
        if not is_async:
            self._add(
                AWAIT_TARGET_NOT_ALLOWED,
                subject=self._callee_name(callee) or "call",
                hint="Only async capabilities and async helpers can be awaited.",
                node=target,
            )
        return self._call(target, scope, parent=node)

    def _comprehension(
        self,
        node: ast.AST,
        scope: _Scope,
        *,
        parent: ast.AST | None,
        node_type: str,
    ) -> KindInfo:
        if node_type == "ListComp":
            self._comprehension_core(node, scope, parent=parent, key=None)
            return KindInfo(value_kind=spec.KIND_LIST)
        if node_type == "SetComp":
            self._comprehension_core(node, scope, parent=parent, key=None)
            return KindInfo(value_kind=spec.KIND_SET)
        self._comprehension_core(node, scope, parent=parent, key="elt")
        return KindInfo(value_kind=spec.KIND_DICT)

    def _comprehension_core(
        self,
        node: Any,
        scope: _Scope,
        *,
        parent: ast.AST | None,
        key: str | None,
    ) -> None:
        comp_scope = _Scope(parent=scope)
        # dict comprehension has .key and .value; list/set have .elt
        if key == "elt":
            info = self._expr(node.elt, comp_scope, parent=node)
            self._check_non_escapeable(node.elt, info, comp_scope, parent=node)
        else:
            for field in ("key", "value"):
                value_node = getattr(node, field)
                info = self._expr(value_node, comp_scope, parent=node)
                self._check_non_escapeable(value_node, info, comp_scope, parent=node)
        for generator in node.generators:
            if getattr(generator, "is_async", False):
                self._add(
                    NODE_NOT_ALLOWED,
                    subject="async comprehension",
                    hint="Async comprehensions are not supported.",
                    node=generator,
                )
            iter_info = self._expr(generator.iter, comp_scope, parent=node)
            if iter_info.value_kind not in _ITERABLE_KINDS:
                self._add(
                    CALL_NOT_ALLOWED,
                    subject="iteration",
                    hint="This value cannot be iterated in a comprehension.",
                    node=generator,
                    message="This value cannot be iterated.",
                )
            self._for_target(generator.target, comp_scope, parent=node)
            for cond in generator.ifs:
                self._expr(cond, comp_scope, parent=node)

    def _joined_str(
        self,
        node: ast.JoinedStr,
        scope: _Scope,
        *,
        parent: ast.AST | None,
    ) -> KindInfo:
        for value in node.values:
            info = self._expr(value, scope, parent=node)
            self._check_non_escapeable(value, info, scope, parent=node)
        return KindInfo(value_kind=spec.KIND_STR)


class _Scope:
    """Lexical scope with parent lookup."""

    __slots__ = ("_bindings", "parent", "is_module")

    def __init__(
        self, *, parent: _Scope | None = None, is_module: bool = False
    ) -> None:
        self._bindings: dict[str, Binding] = {}
        self.parent = parent
        self.is_module = is_module

    def bind(self, name: str, binding: Binding) -> None:
        self._bindings[name] = binding

    def lookup(self, name: str) -> Binding | None:
        scope: _Scope | None = self
        while scope is not None:
            binding = scope._bindings.get(name)
            if binding is not None:
                return binding
            scope = scope.parent
        return None


def validate_source(
    source: str,
    *,
    language_version: str = spec.DEFAULT_LANGUAGE_VERSION,
    limits: dict[str, int] | None = None,
) -> ValidationResult:
    """Validate a source string for the given language version."""
    if language_version not in spec.SUPPORTED_LANGUAGE_VERSIONS:
        from astrbot.script_runtime.errors import ScriptLanguageVersionError

        raise ScriptLanguageVersionError(language_version)
    return ScriptValidator(language_version, limits or {}).validate(source)


__all__ = [
    "Binding",
    "KindInfo",
    "ScriptValidator",
    "compute_source_hash",
    "count_ast",
    "validate_source",
]
