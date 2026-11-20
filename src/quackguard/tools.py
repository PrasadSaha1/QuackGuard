"""Application-owned registrations and action alignment."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from .arguments import Arguments, JSONObject, JSONValue
from .types import Alignment, AlignmentResult, Email, Permission, ToolRequest


class AlignmentCheck(Protocol):
    def check(self, request: ToolRequest) -> AlignmentResult: ...


@dataclass(frozen=True, slots=True)
class ExpectedAction:
    """Match a single trusted, structured action supplied by the application."""

    expected: ToolRequest

    def check(self, request: ToolRequest) -> AlignmentResult:
        if request.user_request != self.expected.user_request:
            return AlignmentResult(Alignment.UNAVAILABLE)
        return AlignmentResult(Alignment.ALIGNED if request == self.expected else Alignment.MISALIGNED)


class FixtureAlignment:
    """Look up the entire trusted request, tool name and immutable arguments."""

    def __init__(self, fixtures: Mapping[ToolRequest, Alignment]):
        self._fixtures = dict(fixtures)

    def check(self, request: ToolRequest) -> AlignmentResult:
        return AlignmentResult(self._fixtures.get(request, Alignment.UNAVAILABLE))


@dataclass(frozen=True, slots=True)
class BoundTool:
    permission: Callable[[], Permission]
    execute: Callable[[], object]
    return_text: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class Tool:
    """Register a synchronous callable that accepts a single JSON object."""

    execute: Callable[[JSONObject], object]
    validate: Callable[[JSONObject], bool]
    permission: Callable[[JSONObject], Permission]
    return_text: bool = False

    def __post_init__(self) -> None:
        if any(not callable(fn) or inspect.iscoroutinefunction(fn)
               for fn in (self.execute, self.validate, self.permission)):
            raise ValueError("Tool callbacks must be synchronous callables.")
        if type(self.return_text) is not bool:
            raise ValueError("return_text must be a boolean.")

    def bind(self, arguments: Email | Arguments) -> BoundTool | None:
        if not isinstance(arguments, Arguments):
            return None
        valid = self.validate(arguments.as_dict())
        if type(valid) is not bool:
            if inspect.iscoroutine(valid):
                valid.close()
            raise ValueError("The validator must return a boolean.")
        if not valid:
            return None
        return BoundTool(lambda: self.permission(arguments.as_dict()),
                         lambda: self.execute(arguments.as_dict()), self.return_text)


@dataclass(frozen=True, slots=True)
class EmailTool:
    send: Callable[[Email], object]
    permission: Callable[[Email], Permission]

    def bind(self, arguments: Email | Arguments) -> BoundTool | None:
        if not isinstance(arguments, Email):
            return None
        return BoundTool(lambda: self.permission(arguments), lambda: self.send(arguments), False)
