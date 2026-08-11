"""Interpreter and state behavior tests."""

from __future__ import annotations

import pytest

from astrbot.script_runtime.errors import StateNotJsonError
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
