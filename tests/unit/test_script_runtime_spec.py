"""Registry consistency tests for astrbot-python-subset."""

from __future__ import annotations

import ast

import pytest

from astrbot.script_runtime import spec


def test_every_module_member_has_a_kind():
    for module, members in spec.MODULES.items():
        for member, kind in members.items():
            assert kind in {"const", "callable", "class", "value"}, (
                module,
                member,
                kind,
            )


def test_forbidden_members_are_not_in_registry():
    for module, forbidden in spec.FORBIDDEN_MODULE_MEMBERS.items():
        for member in forbidden:
            assert member not in spec.MODULES[module]


def test_allowed_statements_and_expressions_are_real_ast_names():
    for name in spec.ALLOWED_STATEMENTS | spec.ALLOWED_EXPRESSIONS:
        assert hasattr(ast, name), name


def test_allowed_operators_are_real_ast_operators():
    for name in spec.ALLOWED_OPERATORS:
        assert hasattr(ast, name), name


def test_builtins_and_exceptions_are_disjoint():
    assert spec.ALLOWED_BUILTINS.isdisjoint(spec.ALLOWED_EXCEPTIONS)


def test_compact_contract_mentions_core_api():
    contract = spec.build_compact_contract()
    assert "ctx.http.request" in contract
    assert "ctx.send_text" in contract
    assert "ctx.state" in contract
    assert "ctx.run" in contract


def test_reference_doc_lists_every_module():
    doc = spec.build_reference_doc()
    for module in spec.MODULES:
        assert f"`{module}`" in doc


def test_coerce_limits_validates():
    assert (
        spec.coerce_limits({"execution_timeout_seconds": 1})[
            "execution_timeout_seconds"
        ]
        == 1
    )
    with pytest.raises(ValueError):
        spec.coerce_limits({"execution_timeout_seconds": 0})
    with pytest.raises(ValueError):
        spec.coerce_limits({"execution_timeout_seconds": "abc"})
