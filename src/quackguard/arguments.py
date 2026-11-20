"""Immutable snapshots of the JSON objects supplied to application tools."""

from __future__ import annotations

import json
import math
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import TypeAlias

JSONValue: TypeAlias = str | int | float | bool | None | list["JSONValue"] | dict[str, "JSONValue"]
JSONObject: TypeAlias = dict[str, JSONValue]


@dataclass(frozen=True, slots=True, init=False)
class Arguments(Mapping[str, JSONValue]):
    """Canonical JSON identity. Each read returns detached ordinary Python values."""

    _json: str = field(repr=False)

    def __init__(self, values: JSONObject):
        if type(values) is not dict:
            raise ValueError("Tool arguments must be a JSON object.")
        # Bound traversal before serialization and reject coercions such as integer keys.
        budget = 65_536

        def visit(value: JSONValue, depth: int) -> None:
            nonlocal budget
            budget -= len(value) + 1 if type(value) is str else 1
            if budget < 0 or depth > 32:
                raise ValueError("Tool arguments exceed the size or depth limit.")
            if type(value) is dict:
                for key, item in value.items():
                    if type(key) is not str:
                        raise ValueError("JSON object keys must be strings.")
                    visit(key, depth + 1)
                    visit(item, depth + 1)
            elif type(value) is list:
                for item in value:
                    visit(item, depth + 1)
            elif type(value) is int:
                budget -= value.bit_length() // 3
                if budget < 0 or value.bit_length() > 4096:
                    raise ValueError("Integer argument exceeds the supported size.")
            elif type(value) is float:
                if not math.isfinite(value):
                    raise ValueError("JSON numbers must be finite.")
            elif value is not None and type(value) not in (str, bool):
                raise ValueError("Unsupported JSON argument type.")

        visit(values, 0)
        encoded = json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                             allow_nan=False)
        if len(encoded) > 65_536:
            raise ValueError("Tool arguments exceed 64 KiB of canonical JSON.")
        object.__setattr__(self, "_json", encoded)

    def as_dict(self) -> JSONObject:
        return json.loads(self._json)

    def preview(self) -> str:
        return self._json

    def __getitem__(self, key: str) -> JSONValue:
        return self.as_dict()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.as_dict())

    def __len__(self) -> int:
        return len(self.as_dict())
