"""Interpreter and state behavior tests."""

from __future__ import annotations

import pytest

from astrbot.script_runtime.errors import ScriptRuntimeError, StateNotJsonError
from astrbot.script_runtime.interpreter import Interpreter, RunFacade
from astrbot.script_runtime.state import AtomicState, validate_json_value
from astrbot.script_runtime.stdlib import Stdlib
from astrbot.script_runtime.values import SafeValue


async def run(source: str, state: dict | None = None) -> tuple[dict, dict]:
    atomic = AtomicState(state or {})
    sent: list[str] = []

    async def send_text(args, kwargs):
        sent.append(args[0].value)
        return SafeValue("none", None)

    stdlib = Stdlib(send_text=send_text, state=atomic)
    stdlib.run_facade = RunFacade(
        job_id="job-1",
        run_id="run-1",
        started_at=__import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ),
        timezone="Asia/Shanghai",
    )
    interpreter = Interpreter(stdlib, deadline=None)
    await interpreter.run_module(source)
    return atomic.snapshot(), {"sent": sent}


@pytest.mark.asyncio
async def test_simple_math_and_state():
    state, meta = await run(
        "x = 1 + 2\nctx.state['total'] = x\nctx.state['nested'] = {'a': [1, 2]}\n"
    )
    assert state == {"total": 3, "nested": {"a": [1, 2]}}


@pytest.mark.asyncio
async def test_conditional_send():
    state, meta = await run(
        "price = 3888\nif price < 3900:\n    await ctx.send_text(f'low {price}')\n"
    )
    assert meta["sent"] == ["low 3888"]


@pytest.mark.asyncio
async def test_loops_and_comprehensions():
    state, _ = await run(
        "total = 0\n"
        "for i in range(5):\n"
        "    total += i\n"
        "squares = [i * i for i in range(4)]\n"
        "while total < 100:\n"
        "    total *= 2\n"
        "ctx.state['total'] = total\n"
        "ctx.state['squares'] = squares\n"
    )
    assert state["total"] == 160
    assert state["squares"] == [0, 1, 4, 9]


@pytest.mark.asyncio
async def test_sync_helper():
    state, _ = await run(
        "def double(x):\n    return x * 2\nctx.state['v'] = double(21)\n"
    )
    assert state == {"v": 42}


@pytest.mark.asyncio
async def test_async_helper():
    state, _ = await run(
        "async def add(a, b):\n    return a + b\nctx.state['v'] = await add(20, 22)\n"
    )
    assert state == {"v": 42}


@pytest.mark.asyncio
async def test_try_except_catchable():
    state, _ = await run(
        "try:\n"
        "    raise ValueError('boom')\n"
        "except ValueError as e:\n"
        "    ctx.state['caught'] = str(e)\n"
    )
    assert state == {"caught": "boom"}


@pytest.mark.asyncio
async def test_exception_subclasses_are_catchable_by_exception():
    state, _ = await run(
        "try:\n"
        "    b'\\xff'.decode('utf-8')\n"
        "except Exception:\n"
        "    ctx.state['caught'] = True\n"
    )
    assert state == {"caught": True}


@pytest.mark.asyncio
async def test_state_mutation_is_atomic_on_error():
    atomic = AtomicState({"x": 1})
    stdlib = Stdlib(state=atomic)
    interpreter = Interpreter(stdlib, deadline=None)
    with pytest.raises(StateNotJsonError):
        await interpreter.run_module(
            "ctx.state['x'] = 1\n"
            "import datetime\n"
            "ctx.state['bad'] = datetime.datetime(2020, 1, 1)\n"
        )
    assert atomic.snapshot() == {"x": 1}


@pytest.mark.asyncio
async def test_nested_dict_mutation_commits_complete_root_state():
    state, _ = await run(
        "ctx.state['nested']['x'] = 2\nctx.state['nested'].update({'y': 3})\n",
        {"keep": "root", "nested": {"x": 1}},
    )
    assert state == {"keep": "root", "nested": {"x": 2, "y": 3}}


@pytest.mark.asyncio
async def test_nested_list_mutations_commit_complete_root_state():
    state, _ = await run(
        "ctx.state['items'].append(3)\n"
        "ctx.state['items'][0] = 9\n"
        "ctx.state['items'].pop(1)\n"
        "ctx.state['items'].insert(1, {'nested': [4]})\n"
        "ctx.state['items'][1]['nested'].extend([5, 6])\n",
        {"keep": True, "items": [1, 2]},
    )
    assert state == {
        "keep": True,
        "items": [9, {"nested": [4, 5, 6]}, 3],
    }


@pytest.mark.asyncio
async def test_failed_nested_mutation_rolls_back_live_view():
    state, _ = await run(
        "import datetime\n"
        "try:\n"
        "    ctx.state['nested']['bad'] = datetime.datetime(2020, 1, 1)\n"
        "except StateNotJsonError:\n"
        "    ctx.state['nested']['ok'] = 2\n",
        {"keep": "root", "nested": {"x": 1}},
    )
    assert state == {"keep": "root", "nested": {"x": 1, "ok": 2}}


@pytest.mark.asyncio
async def test_failed_overwrite_keeps_saved_nested_view_attached():
    state, _ = await run(
        "import datetime\n"
        "nested = ctx.state['nested']\n"
        "try:\n"
        "    ctx.state['nested'] = datetime.datetime(2020, 1, 1)\n"
        "except StateNotJsonError:\n"
        "    nested['ok'] = 2\n",
        {"keep": "root", "nested": {"x": 1}},
    )
    assert state == {"keep": "root", "nested": {"x": 1, "ok": 2}}


def test_state_graph_does_not_reuse_container_from_another_root():
    first = AtomicState({"nested": {"x": 1}})
    second = AtomicState({})
    first_dict = Stdlib(state=first).get_ctx_attr("state").value
    second_dict = Stdlib(state=second).get_ctx_attr("state").value

    second_dict["copy"] = first_dict["nested"]
    second_dict["copy"]["x"] = 2

    assert first.snapshot() == {"nested": {"x": 1}}
    assert second.snapshot() == {"copy": {"x": 2}}


@pytest.mark.asyncio
async def test_state_requires_string_keys():
    atomic = AtomicState({})
    stdlib = Stdlib(state=atomic)
    interpreter = Interpreter(stdlib, deadline=None)
    with pytest.raises(StateNotJsonError):
        await interpreter.run_module("ctx.state[1] = 'x'\n")


@pytest.mark.asyncio
async def test_fstring_conversions():
    state, _ = await run(
        "s = 'abc'\nctx.state['a'] = f'{s!r}'\nctx.state['b'] = f'{3.14159:.2f}'\n"
    )
    assert state["a"] == "'abc'"
    assert state["b"] == "3.14"


def test_json_validation_rejects_non_json():
    with pytest.raises(StateNotJsonError):
        validate_json_value({"x": b"bytes"})
    with pytest.raises(StateNotJsonError):
        validate_json_value({"x": float("nan")})
    with pytest.raises(StateNotJsonError):
        validate_json_value({1: "x"})
    assert validate_json_value({"a": [1, True, None, "s"]}) == {
        "a": [1, True, None, "s"]
    }


def test_atomic_state_rejects_non_object_root():
    atomic = AtomicState({"x": 1})
    with pytest.raises(StateNotJsonError, match="root value must be an object"):
        atomic.commit([])
    assert atomic.snapshot() == {"x": 1}
    with pytest.raises(StateNotJsonError, match="root value must be an object"):
        AtomicState([])


@pytest.mark.asyncio
async def test_try_except_finally_order_matches_python():
    state, _ = await run(
        "ctx.state['order'] = []\n"
        "try:\n"
        "    raise ValueError('boom')\n"
        "except ValueError:\n"
        "    ctx.state['order'].append('except')\n"
        "finally:\n"
        "    ctx.state['order'].append('finally')\n"
    )
    assert state["order"] == ["except", "finally"]


@pytest.mark.asyncio
async def test_unmatched_exception_preserves_original_type():
    atomic = AtomicState({})
    interpreter = Interpreter(Stdlib(state=atomic), deadline=None)
    with pytest.raises(KeyError, match="boom"):
        await interpreter.run_module(
            "try:\n"
            "    raise KeyError('boom')\n"
            "except ValueError:\n"
            "    pass\n"
            "finally:\n"
            "    ctx.state['finally'] = True\n"
        )
    assert atomic.snapshot() == {"finally": True}


@pytest.mark.asyncio
async def test_bare_raise_in_finally_preserves_propagating_exception():
    atomic = AtomicState({})
    interpreter = Interpreter(Stdlib(state=atomic), deadline=None)
    with pytest.raises(KeyError, match="boom"):
        await interpreter.run_module(
            "try:\n"
            "    raise KeyError('boom')\n"
            "except ValueError:\n"
            "    pass\n"
            "finally:\n"
            "    raise\n"
        )


@pytest.mark.asyncio
async def test_control_flow_signal_is_not_exposed_as_current_exception():
    atomic = AtomicState({})
    interpreter = Interpreter(Stdlib(state=atomic), deadline=None)
    with pytest.raises(ScriptRuntimeError, match="bare raise outside except block"):
        await interpreter.run_module(
            "def choose():\n"
            "    try:\n"
            "        return 7\n"
            "    finally:\n"
            "        raise\n"
            "ctx.state['value'] = choose()\n"
        )


@pytest.mark.asyncio
async def test_control_flow_in_except_keeps_outer_exception_active():
    atomic = AtomicState({})
    interpreter = Interpreter(Stdlib(state=atomic), deadline=None)
    with pytest.raises(ValueError, match="outer"):
        await interpreter.run_module(
            "def choose():\n"
            "    try:\n"
            "        raise ValueError('outer')\n"
            "    except ValueError:\n"
            "        try:\n"
            "            return 7\n"
            "        finally:\n"
            "            raise\n"
            "ctx.state['value'] = choose()\n"
        )


@pytest.mark.asyncio
async def test_control_flow_signals_cross_finally():
    state, _ = await run(
        "ctx.state['events'] = []\n"
        "def choose():\n"
        "    try:\n"
        "        return 7\n"
        "    finally:\n"
        "        ctx.state['events'].append('return-finally')\n"
        "ctx.state['value'] = choose()\n"
        "for i in range(3):\n"
        "    try:\n"
        "        if i == 0:\n"
        "            continue\n"
        "        break\n"
        "    finally:\n"
        "        ctx.state['events'].append(i)\n"
    )
    assert state == {
        "events": ["return-finally", 0, 1],
        "value": 7,
    }
