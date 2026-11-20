"""Immutable inputs and finite, log-safe outcomes used by the guard."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from .arguments import Arguments, JSONObject, JSONValue


class Decision(StrEnum):
    ALLOW = "ALLOW"
    BLOCK = "BLOCK"
    REVIEW = "REVIEW"


class Verdict(StrEnum):
    SAFE = "SAFE"
    UNSAFE = "UNSAFE"
    UNAVAILABLE = "UNAVAILABLE"


class Direction(StrEnum):
    INPUT = "input"
    OUTPUT = "output"


class Permission(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class Alignment(StrEnum):
    ALIGNED = "aligned"
    MISALIGNED = "misaligned"
    UNCERTAIN = "uncertain"
    UNAVAILABLE = "unavailable"


class UserDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class Execution(StrEnum):
    UNCALLED = "uncalled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Classification:
    verdict: Verdict
    categories: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.verdict) is not Verdict or type(self.categories) is not tuple:
            raise ValueError("Invalid scanner classification.")
        if self.categories and self.verdict is not Verdict.UNSAFE:
            raise ValueError("Only unsafe classifications can carry categories.")
        if any(type(c) is not str or not re.fullmatch(r"S(?:[1-9]|1[0-4])", c)
               for c in self.categories) or len(set(self.categories)) != len(self.categories):
            raise ValueError("Invalid safety categories.")


@dataclass(frozen=True, slots=True)
class ScanResult:
    stage: Direction
    verdict: Verdict
    decision: Decision
    reason_code: str
    explanation: str
    categories: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Email:
    """A single ASCII mailbox and plaintext message, with bounded review fields."""

    recipient: str
    subject: str
    body: str = field(repr=False)

    def __post_init__(self) -> None:
        mailbox = r"[A-Za-z0-9_+%-]+(?:\.[A-Za-z0-9_+%-]+)*"
        label = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
        if (type(self.recipient) is not str or len(self.recipient) > 254
                or not re.fullmatch(rf"{mailbox}@{label}(?:\.{label})+", self.recipient)
                or len(self.recipient.partition("@")[0]) > 64):
            raise ValueError("Expected a single supported email address.")
        if (type(self.subject) is not str or not self.subject.strip()
                or len(self.subject) > 160 or not self.subject.isprintable()):
            raise ValueError("Expected a printable subject of up to 160 characters.")
        if type(self.body) is not str or not self.body.strip() or len(self.body) > 4000:
            raise ValueError("Expected a nonempty body of up to 4000 characters.")

    def preview(self) -> str:
        # Escape controls, newlines, quotes and non-ASCII text at the terminal boundary.
        return json.dumps({"to": self.recipient, "subject": self.subject, "body": self.body},
                          ensure_ascii=True)


@dataclass(frozen=True, slots=True, init=False)
class ToolRequest:
    user_request: str = field(repr=False)
    tool_name: str
    arguments: Email | Arguments = field(repr=False)

    def __init__(self, user_request: str, tool_name: str, arguments: Email | Arguments | JSONObject):
        if type(user_request) is not str or not user_request.strip() or len(user_request) > 65_536:
            raise ValueError("Supply a nonempty trusted user request of up to 65536 characters.")
        if type(tool_name) is not str:
            raise ValueError("Expected a tool name.")
        if type(arguments) is dict:
            arguments = Arguments(arguments)
        if type(arguments) not in (Email, Arguments):
            raise ValueError("Expected email arguments or a JSON object.")
        object.__setattr__(self, "user_request", user_request)
        object.__setattr__(self, "tool_name", tool_name)
        object.__setattr__(self, "arguments", arguments)


@dataclass(frozen=True, slots=True)
class AlignmentResult:
    verdict: Alignment

    def __post_init__(self) -> None:
        if type(self.verdict) is not Alignment:
            raise ValueError("Invalid alignment verdict.")


@dataclass(frozen=True, slots=True)
class ToolResult:
    proposal_id: str
    stage: str
    tool_name: str | None
    decision: Decision
    reason_code: str
    explanation: str
    permission: Permission | None
    alignment: Alignment | None
    execution: Execution = Execution.UNCALLED
    review_summary: str | None = field(default=None, repr=False)
    outcome_recorded: bool | None = None
    output_scan: ScanResult | None = None
    output_text: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class Event:
    timestamp: datetime
    stage: str
    decision: Decision
    reason_code: str
    duration_ms: float
    tool_name: str | None = None
    proposal_id: str | None = None
    execution: Execution = Execution.UNCALLED
    permission: Permission | None = None
    alignment: Alignment | None = None


@dataclass(frozen=True, slots=True)
class Stats:
    scanned_inputs: int
    scanned_outputs: int
    flagged_scans: int
    unavailable_scans: int
    allowed_proposals: int
    blocked_proposals: int
    held_proposals: int
    executions_succeeded: int
    executions_failed: int
