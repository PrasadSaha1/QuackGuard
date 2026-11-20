"""Local decision, review, and execution lifecycle for registered email tools."""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from threading import Lock
from time import monotonic, perf_counter
from uuid import uuid4

from .scanners import Scanner
from .tools import AlignmentCheck, EmailTool
from .types import (
    Alignment, AlignmentResult, Classification, Decision, Direction, Event, Execution,
    Permission, ScanResult, Stats, ToolRequest, ToolResult, UserDecision, Verdict,
)


@dataclass
class _Proposal:
    request: ToolRequest
    name: str | None
    deadline: float
    id: str = field(default_factory=lambda: uuid4().hex)
    result: ToolResult | None = None
    lock: Lock = field(default_factory=Lock)


_SCAN = {
    Verdict.SAFE: (Decision.ALLOW, "content_safe", "The supplied content passed the scan."),
    Verdict.UNSAFE: (Decision.BLOCK, "content_flagged", "The scanner flagged the supplied content."),
    Verdict.UNAVAILABLE: (Decision.REVIEW, "scanner_unavailable", "The content check could not be completed."),
}


class Guard:
    def __init__(self, scanner: Scanner, *, tools: Mapping[str, EmailTool] | None = None,
                 alignment: AlignmentCheck | None = None, review_ttl: float = 300,
                 clock: Callable[[], float] = monotonic):
        self._scanner = scanner
        self._tools = dict(tools or {})
        if any(type(name) is not str or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name)
               or type(tool) is not EmailTool for name, tool in self._tools.items()):
            raise ValueError("Register email tools using short application-defined identifiers.")
        if type(review_ttl) not in (int, float) or not math.isfinite(review_ttl) or review_ttl <= 0:
            raise ValueError("Review lifetime must be finite and positive.")
        self._alignment = alignment
        self._clock = clock
        self._ttl = review_ttl
        self._lock = Lock()
        self._pending: dict[str, _Proposal] = {}
        self._events: list[Event] = []
        self._decisions: Counter[Decision] = Counter()

    @property
    def events(self) -> tuple[Event, ...]:
        with self._lock:
            return tuple(self._events)

    def scan_input(self, text: str) -> ScanResult:
        return self._scan(Direction.INPUT, text)

    def scan_output(self, text: str) -> ScanResult:
        return self._scan(Direction.OUTPUT, text)

    def _scan(self, direction: Direction, text: str) -> ScanResult:
        start = perf_counter()
        try:
            if type(text) is not str:
                raise ValueError
            classification = self._scanner.scan(direction, text)
            if type(classification) is not Classification:
                raise ValueError
        except Exception:
            classification = Classification(Verdict.UNAVAILABLE)
        decision, reason, explanation = _SCAN[classification.verdict]
        with self._lock:
            self._events.append(Event(datetime.now(timezone.utc), direction.value, decision, reason,
                                      (perf_counter() - start) * 1000))
        return ScanResult(direction, classification.verdict, decision, reason, explanation,
                          classification.categories)

    def run_tool(self, request: ToolRequest) -> ToolResult:
        """Submit a new proposal. The application registers the callable in advance."""
        if type(request) is not ToolRequest:
            raise TypeError("Expected a validated ToolRequest.")
        # Only a trusted registry identifier may enter events.
        name = request.tool_name if request.tool_name in self._tools else None
        proposal = _Proposal(request, name, self._clock() + self._ttl)
        with proposal.lock:
            return self._evaluate(proposal)

    def review(self, proposal_id: str, request: ToolRequest,
               decision: UserDecision | None = None) -> ToolResult:
        """Resolve one pending proposal using an application-authenticated local decision."""
        return self._resolve(proposal_id, request, decision, recheck=False)

    def recheck(self, proposal_id: str, request: ToolRequest) -> ToolResult:
        """Refresh required checks. Successful recovery still requires a fresh user decision."""
        return self._resolve(proposal_id, request, None, recheck=True)

    def _resolve(self, proposal_id: str, request: ToolRequest, decision: UserDecision | None,
                 *, recheck: bool) -> ToolResult:
        with self._lock:
            proposal = self._pending.get(proposal_id)
        if proposal is None:
            raise ValueError("No pending proposal with that ID.")
        with proposal.lock:
            previous = proposal.result
            if previous is None or previous.decision is not Decision.REVIEW:
                raise ValueError("No pending proposal with that ID.")
            start = perf_counter()
            if self._clock() >= proposal.deadline:
                reason, message = "approval_expired", "The review period expired."
            elif type(request) is not ToolRequest or request != proposal.request:
                reason, message = "proposal_changed", "The submitted request differs from the reviewed action."
            elif decision is UserDecision.DENY:
                reason, message = "user_denied", "The user declined this action."
            elif recheck:
                return self._evaluate(proposal, reviewing=True)
            elif decision is not UserDecision.ALLOW or previous.reason_code in {
                    "alignment_unavailable", "permission_unavailable"}:
                return previous
            else:
                return self._evaluate(proposal, reviewing=True, approved=True)
            return self._record(proposal, start, "approval", Decision.BLOCK, reason, message,
                                previous.permission, previous.alignment)

    def _evaluate(self, proposal: _Proposal, *, reviewing: bool = False,
                  approved: bool = False) -> ToolResult:
        start = perf_counter()
        permission = None
        alignment = None

        def record(stage: str, decision: Decision, reason: str, explanation: str) -> ToolResult:
            return self._record(proposal, start, stage, decision, reason, explanation,
                                permission, alignment)

        if proposal.name is None:
            return record("permission", Decision.BLOCK, "unregistered_tool", "This tool is not registered.")
        tool = self._tools[proposal.name]
        try:
            permission = tool.permission(proposal.request.arguments)
            if type(permission) is not Permission:
                permission = None
        except Exception:
            permission = None
        if permission is None:
            return record("permission", Decision.REVIEW, "permission_unavailable",
                          "The permission check must complete before this email can be sent.")
        if permission is Permission.DENY:
            return record("permission", Decision.BLOCK, "policy_denied",
                          "The application policy denies this email.")
        try:
            result = self._alignment.check(proposal.request) if self._alignment else None
            alignment = result.verdict if type(result) is AlignmentResult else Alignment.UNAVAILABLE
        except Exception:
            alignment = Alignment.UNAVAILABLE
        if alignment is Alignment.MISALIGNED:
            return record("alignment", Decision.BLOCK, "misaligned_action",
                          "Sending this email conflicts with the original user request.")
        if alignment is Alignment.UNAVAILABLE:
            return record("alignment", Decision.REVIEW, "alignment_unavailable",
                          "The intent check must complete before this email can be sent.")
        if reviewing and self._clock() >= proposal.deadline:
            return record("approval", Decision.BLOCK, "approval_expired", "The review period expired.")
        if not approved:
            if alignment is Alignment.UNCERTAIN:
                return record("alignment", Decision.REVIEW, "alignment_uncertain",
                              "Confirm that this exact email is what you intended.")
            if permission is Permission.ASK or reviewing:
                return record("approval", Decision.REVIEW, "user_approval_required",
                              "The user must approve this exact email before it can be sent.")
        if approved:
            allowed = record("approval", Decision.ALLOW, "user_approved",
                             "The user approved the reviewed email.")
        else:
            allowed = record("alignment", Decision.ALLOW, "aligned_action",
                             "Policy permits the email and it matches the user request.")
        # Admission is recorded and pending approval consumed before invoking external code.
        start = perf_counter()
        try:
            tool.send(proposal.request.arguments)
            execution = Execution.SUCCEEDED
        except Exception:
            execution = Execution.FAILED
        with self._lock:
            self._events.append(Event(datetime.now(timezone.utc), "execution", Decision.ALLOW,
                                      f"execution_{execution.value}", (perf_counter() - start) * 1000,
                                      proposal.name, proposal.id, execution))
        if execution is Execution.FAILED:
            return replace(allowed, execution=execution,
                           explanation=f"{allowed.explanation} The sender raised an error.")
        return replace(allowed, execution=execution)

    def _record(self, proposal: _Proposal, start: float, stage: str, decision: Decision,
                reason: str, explanation: str, permission: Permission | None,
                alignment: Alignment | None) -> ToolResult:
        summary = None
        if decision is Decision.REVIEW:
            summary = f"Send email via {proposal.name}: {proposal.request.arguments.preview()}"
        result = ToolResult(proposal.id, stage, proposal.name, decision, reason, explanation,
                            permission, alignment, review_summary=summary)
        with self._lock:
            if proposal.result:
                self._decisions[proposal.result.decision] -= 1
            self._decisions[decision] += 1
            proposal.result = result
            if decision is Decision.REVIEW:
                self._pending[proposal.id] = proposal
            else:
                self._pending.pop(proposal.id, None)
            self._events.append(Event(datetime.now(timezone.utc), stage, decision, reason,
                                      (perf_counter() - start) * 1000, proposal.name, proposal.id,
                                      permission=permission, alignment=alignment))
        return result

    def stats(self) -> Stats:
        with self._lock:
            stages = Counter(event.stage for event in self._events)
            reasons = Counter(event.reason_code for event in self._events)
            return Stats(stages["input"], stages["output"], reasons["content_flagged"],
                         reasons["scanner_unavailable"], self._decisions[Decision.ALLOW],
                         self._decisions[Decision.BLOCK], self._decisions[Decision.REVIEW],
                         reasons["execution_succeeded"], reasons["execution_failed"])
