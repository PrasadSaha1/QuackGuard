"""The email tool is bound by the application before any proposal arrives."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from .types import Alignment, AlignmentResult, Email, Permission, ToolRequest


class AlignmentCheck(Protocol):
    def check(self, request: ToolRequest) -> AlignmentResult: ...


class FixtureAlignment:
    """Look up the entire trusted request, tool name and immutable arguments."""

    def __init__(self, fixtures: Mapping[ToolRequest, Alignment]):
        self._fixtures = dict(fixtures)

    def check(self, request: ToolRequest) -> AlignmentResult:
        return AlignmentResult(self._fixtures.get(request, Alignment.UNAVAILABLE))


@dataclass(frozen=True, slots=True)
class EmailTool:
    send: Callable[[Email], object]
    permission: Callable[[Email], Permission]
