"""Tagged safe values for the script interpreter."""

from __future__ import annotations

import datetime as _datetime
import hmac
import re
import time as _time
import urllib.parse
import zoneinfo
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from astrbot.script_runtime import spec


@dataclass
class SafeValue:
    """A value owned by the interpreter.

    ``kind`` is one of the ``spec.KIND_*`` constants.  ``value`` is the
    underlying trusted Python object.  Scripts never see this class directly.
    """

    kind: str
    value: Any

    @classmethod
    def from_python(cls, value: Any) -> SafeValue:
        if value is None:
            return cls(spec.KIND_NONE, None)
        if isinstance(value, bool):
            return cls(spec.KIND_BOOL, value)
        if isinstance(value, int):
            return cls(spec.KIND_INT, value)
        if isinstance(value, float):
            return cls(spec.KIND_FLOAT, value)
        if isinstance(value, str):
            return cls(spec.KIND_STR, value)
        if isinstance(value, bytes):
            return cls(spec.KIND_BYTES, value)
        if isinstance(value, list):
            return cls(spec.KIND_LIST, value)
        if isinstance(value, tuple):
            return cls(spec.KIND_TUPLE, value)
        if isinstance(value, set):
            return cls(spec.KIND_SET, value)
        if isinstance(value, dict):
            return cls(spec.KIND_DICT, value)
        if isinstance(value, range):
            return cls(spec.KIND_RANGE, value)
        if isinstance(value, _datetime.datetime):
            return cls(spec.KIND_DATETIME, value)
        if isinstance(value, _datetime.date):
            return cls(spec.KIND_DATE, value)
        if isinstance(value, _datetime.time):
            return cls(spec.KIND_TIME, value)
        if isinstance(value, _datetime.timedelta):
            return cls(spec.KIND_TIMEDELTA, value)
        if isinstance(value, zoneinfo.ZoneInfo):
            return cls(spec.KIND_ZONEINFO, value)
        if isinstance(value, _datetime.tzinfo):
            return cls(spec.KIND_TIMEZONE, value)
        if isinstance(value, Decimal):
            return cls(spec.KIND_DECIMAL, value)
        if isinstance(value, _time.struct_time):
            return cls(spec.KIND_STRUCT_TIME, value)
        if isinstance(value, re.Pattern):
            return cls(spec.KIND_REGEX_PATTERN, value)
        if isinstance(value, re.Match):
            return cls(spec.KIND_REGEX_MATCH, value)
        if isinstance(value, urllib.parse.ParseResult):
            return cls(spec.KIND_URL_RESULT, value)
        if isinstance(value, hmac.HMAC):
            return cls(spec.KIND_HMAC, value)
        if (
            type(value).__module__ == "hashlib._hashlib"
            and type(value).__name__ == "HASH"
        ):
            return cls(spec.KIND_DIGEST, value)
        return cls(spec.KIND_UNKNOWN, value)

    def truthy(self) -> bool:
        try:
            return bool(self.value)
        except Exception:
            return True

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"SafeValue(kind={self.kind!r})"


__all__ = ["SafeValue"]
