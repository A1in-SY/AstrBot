"""JSON-only persistent state with atomic mutation semantics."""

from __future__ import annotations

import copy
import math
from collections.abc import Iterator
from typing import Any

from astrbot.script_runtime.errors import StateNotJsonError

JSONScalar = None | bool | int | float | str
JSONValue = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]


def _is_json_scalar(value: Any) -> bool:
    return (
        value is None
        or isinstance(value, (bool, str, int, float))
        and not (isinstance(value, float) and (math.isnan(value) or math.isinf(value)))
    )


def validate_json_value(value: Any, *, path: str = "state") -> JSONValue:
    """Validate and deep-copy a candidate value as strict JSON."""
    if _is_json_scalar(value):
        if isinstance(value, bool) or isinstance(value, (int, float)):
            return value
        return copy.deepcopy(value)
    if isinstance(value, dict):
        result: dict[str, JSONValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise StateNotJsonError(f"{path}: dict keys must be strings")
            result[key] = validate_json_value(item, path=f"{path}.{key}")
        return result
    if isinstance(value, list):
        return [
            validate_json_value(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    raise StateNotJsonError(f"{path}: unsupported value type {type(value).__name__}")


class StateView:
    """A read-only snapshot view of validated JSON state."""

    __slots__ = ("_data",)

    def __init__(self, data: dict[str, JSONValue]) -> None:
        self._data = data

    def to_dict(self) -> dict[str, JSONValue]:
        return copy.deepcopy(self._data)


class AtomicState:
    """State wrapper that only commits fully-valid JSON mutations.

    The wrapper keeps an internal raw ``dict`` plus a separate shallow
    container graph.  Mutations validate the entire candidate state first and
    then atomically replace the internal snapshot, so a failed mutation never
    leaves a partially applied change.
    """

    def __init__(self, initial: Any | None = None) -> None:
        validated = validate_json_value(initial if initial is not None else {})
        if not isinstance(validated, dict):
            raise StateNotJsonError("state: root value must be an object")
        self._data: dict[str, JSONValue] = validated

    def snapshot(self) -> dict[str, JSONValue]:
        return copy.deepcopy(self._data)

    def get(self, key: str) -> JSONValue:
        return copy.deepcopy(self._data[key])

    def commit(self, candidate: Any) -> None:
        validated = validate_json_value(candidate)
        if not isinstance(validated, dict):
            raise StateNotJsonError("state: root value must be an object")
        self._data = validated

    def mutate(self, mutation) -> JSONValue:
        """Apply a mutation callable against a fresh deep copy, atomically."""
        candidate = self.snapshot()
        result = mutation(candidate)
        self._data = validate_json_value(candidate)
        return copy.deepcopy(result)

    def __len__(self) -> int:
        return len(self._data)

    def __contains__(self, key: object) -> bool:
        return key in self._data

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)


__all__ = [
    "AtomicState",
    "JSONValue",
    "StateView",
    "validate_json_value",
]
