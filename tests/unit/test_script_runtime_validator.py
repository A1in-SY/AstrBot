"""Validator acceptance tests for astrbot-python-subset/v1."""

from __future__ import annotations

import pytest

from astrbot.script_runtime.diagnostics import (
    ASSIGNMENT_TARGET_NOT_ALLOWED,
    ASYNC_CALL_REQUIRES_AWAIT,
    ATTRIBUTE_NOT_ALLOWED,
    AWAIT_TARGET_NOT_ALLOWED,
    CAPABILITY_VALUE_NOT_ALLOWED,
    CONTROL_FLOW_NOT_ALLOWED,
    FUNCTION_VALUE_NOT_ALLOWED,
    MODULE_MEMBER_NOT_ALLOWED,
    NAME_NOT_DEFINED,
    NAME_REBIND_NOT_ALLOWED,
    NODE_NOT_ALLOWED,
    SYNTAX_ERROR,
)
from astrbot.script_runtime.validator import validate_source


def _codes(source: str) -> list[str]:
    result = validate_source(source)
    return [d.code for d in result.diagnostics]


def test_golden_path_valid():
    source = (
        'response = await ctx.http.request("GET", "https://example.com", '
        "use_proxy=False)\n"
        'price = response.json()["price"]\n'
        "if price < 3900:\n"
        '    await ctx.send_text(f"low: {price}")\n'
        'ctx.state["last"] = str(price)\n'
    )
    result = validate_source(source)
    assert result.valid is True
    assert result.total_diagnostics == 0


def test_syntax_error():
    assert SYNTAX_ERROR in _codes("def x(:\n")


@pytest.mark.parametrize(
    "source, code",
    [
        ("lambda x: x", NODE_NOT_ALLOWED),
        ("class A: pass", NODE_NOT_ALLOWED),
        ("yield 1", NODE_NOT_ALLOWED),
        ("del x", NODE_NOT_ALLOWED),
        ("x = (y := 1)", NODE_NOT_ALLOWED),
        ("def f():\n    return 1\n", None),  # valid
    ],
)
def test_node_allowlist(source, code):
    if code is None:
        assert validate_source(source).valid
    else:
        assert code in _codes(source)


def test_named_expr_rejected():
    assert NODE_NOT_ALLOWED in _codes("x = (y := 1)")


def test_break_outside_loop():
    assert CONTROL_FLOW_NOT_ALLOWED in _codes("break")


def test_return_outside_function():
    assert CONTROL_FLOW_NOT_ALLOWED in _codes("return 1")


def test_lambda_rejected():
    assert NODE_NOT_ALLOWED in _codes("f = lambda x: x")


def test_unknown_name():
    assert NAME_NOT_DEFINED in _codes("x = undefined_var")


def test_rebinding_ctx_rejected():
    assert NAME_REBIND_NOT_ALLOWED in _codes("ctx = 1")


def test_rebinding_builtin_rejected():
    assert NAME_REBIND_NOT_ALLOWED in _codes("len = 1")


def test_rebinding_import_rejected():
    assert NAME_REBIND_NOT_ALLOWED in _codes("import json\njson = 1")


def test_module_not_allowed():
    assert "MODULE_NOT_ALLOWED" in _codes("import requests")


def test_module_member_not_allowed_file_digest():
    codes = _codes("import hashlib as h\nh.file_digest(b'x', 'sha256')")
    assert MODULE_MEMBER_NOT_ALLOWED in codes
    # Follow-up call errors are suppressed after the root cause.
    assert codes.count(MODULE_MEMBER_NOT_ALLOWED) == 1
    assert "CALL_NOT_ALLOWED" not in codes


def test_wildcard_import_rejected():
    assert "WILDCARD_IMPORT_NOT_ALLOWED" in _codes("from json import *")


def test_relative_import_rejected():
    assert "RELATIVE_IMPORT_NOT_ALLOWED" in _codes("from . import json")


def test_dotted_import_requires_alias():
    assert "IMPORT_FORM_NOT_ALLOWED" in _codes("import urllib.parse")
    assert validate_source("import urllib.parse as urlparse").valid


def test_attribute_not_allowed():
    assert ATTRIBUTE_NOT_ALLOWED in _codes("s = 'x'\ns.foo()")


def test_builtin_not_allowed():
    assert "BUILTIN_NOT_ALLOWED" in _codes("print('x')")


def test_function_value_not_allowed():
    assert FUNCTION_VALUE_NOT_ALLOWED in _codes("import json\nf = json.loads")


def test_capability_value_not_allowed():
    assert CAPABILITY_VALUE_NOT_ALLOWED in _codes("s = ctx.state")


def test_async_call_requires_await():
    assert ASYNC_CALL_REQUIRES_AWAIT in _codes("ctx.send_text('x')")


def test_async_helper_requires_await():
    source = "async def helper():\n    return 1\npending = helper()\n"
    assert ASYNC_CALL_REQUIRES_AWAIT in _codes(source)


def test_await_non_async_rejected():
    source = "x = await 1"
    assert AWAIT_TARGET_NOT_ALLOWED in _codes(source)


def test_storing_coroutine_rejected():
    source = "pending = ctx.http.request('GET', 'https://example.com')"
    assert ASYNC_CALL_REQUIRES_AWAIT in _codes(source)


def test_exception_type_not_allowed():
    assert "EXCEPTION_TYPE_NOT_ALLOWED" in _codes("raise MemoryError('x')")


def test_starred_assignment_rejected():
    assert ASSIGNMENT_TARGET_NOT_ALLOWED in _codes("a, *b = [1, 2, 3]")


def test_nested_function_rejected():
    assert "FUNCTION_LOCATION_NOT_ALLOWED" in _codes(
        "def outer():\n    def inner():\n        return 1\n"
    )


def test_function_default_must_be_json():
    assert "DEFAULT_VALUE_NOT_JSON" in _codes("def f(x=()):\n    return x\n")


def test_multiple_independent_errors_reported():
    source = "undefined_a\nundefined_b\nundefined_c\n"
    result = validate_source(source)
    codes = [d.code for d in result.diagnostics]
    assert codes.count(NAME_NOT_DEFINED) == 3


def test_occurrences_merged_for_same_error():
    source = "s = 'x'\ns.foo()\ns.foo()\n"
    result = validate_source(source)
    attr = [d for d in result.diagnostics if d.code == ATTRIBUTE_NOT_ALLOWED]
    assert len(attr) == 1
    assert attr[0].occurrence_count == 2


def test_utf16_columns_for_cjk():
    source = "x = '中文😀'\nx.lowerz()\n"
    result = validate_source(source)
    diag = [d for d in result.diagnostics if d.code == ATTRIBUTE_NOT_ALLOWED]
    assert diag
    occurrence = diag[0].occurrences[0]
    assert occurrence.line == 2
    # The diagnostic covers the whole attribute expression `x.lowerz`.
    assert occurrence.column >= 1


def test_source_too_large():
    source = "#" * 70000
    assert "SOURCE_TOO_LARGE" in _codes(source)


def test_ast_too_deep():
    depth = 200
    source = "x = " + "not " * depth + "1"
    assert "AST_TOO_DEEP" in _codes(source)


def test_hashlib_member_alias_rejected():
    source = "from hashlib import file_digest"
    assert MODULE_MEMBER_NOT_ALLOWED in _codes(source)
