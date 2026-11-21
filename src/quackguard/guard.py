"""Local decision, review, and execution lifecycle for application-owned tools."""

from __future__ import annotations

import inspect
import math
import re
from collections import Counter, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from time import monotonic, perf_counter
from uuid import uuid4

from ._snapshot import write_snapshot
from .ledger import LedgerUnavailable, SQLiteLedger
from .scanners import Scanner
from .tools import AlignmentCheck, BoundTool, EmailTool, Tool
from .types import (
    Alignment, AlignmentResult, Classification, Decision, Direction, Event, Execution,
    Permission, ScanResult, Stats, ToolRequest, ToolResult, UserDecision, Verdict,
)


@dataclass
class _Proposal:
    request: ToolRequest
    name: str | None
    key: str | None = field(repr=False)
    deadline: float = math.inf
    id: str = field(default_factory=lambda: uuid4().hex)
    result: ToolResult | None = None
    lock: Lock = field(default_factory=Lock)


_SCAN = {
    Verdict.SAFE: (Decision.ALLOW, "content_safe", "The supplied content passed the scan."),
    Verdict.UNSAFE: (Decision.BLOCK, "content_flagged", "The scanner flagged the supplied content."),
    Verdict.UNAVAILABLE: (Decision.REVIEW, "scanner_unavailable", "The content check could not be completed."),
}


class Guard:
    def __init__(self, scanner: Scanner, *, tools: Mapping[str, Tool | EmailTool] | None = None,
                 alignment: AlignmentCheck | None = None, review_ttl: float = 300,
                 clock: Callable[[], float] = monotonic, ledger: SQLiteLedger | None = None,
                 max_events: int = 1000, max_pending: int = 256,
                 stats_path: str | Path | None = None, stats_include_events: bool = False):
        self._scanner = scanner
        self._tools = dict(tools or {})
        if any(type(name) is not str or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name)
               or type(tool) not in (Tool, EmailTool) for name, tool in self._tools.items()):
            raise ValueError("Register tools using short application-defined identifiers.")
        if type(review_ttl) not in (int, float) or not math.isfinite(review_ttl) or review_ttl <= 0:
            raise ValueError("Review lifetime must be finite and positive.")
        if any(type(limit) is not int or limit < 1 for limit in (max_events, max_pending)):
            raise ValueError("Activity and pending review limits must be positive integers.")
        self._max_pending = max_pending
        self._ledger = ledger
        self._alignment = alignment
        self._clock = clock
        self._ttl = review_ttl
        self._lock = Lock()
        self._pending: dict[str, _Proposal] = {}
        self._events: deque[Event] = deque(maxlen=max_events)
        self._scan_directions: Counter[Direction] = Counter()
        self._verdicts: Counter[Verdict] = Counter()
        self._executions: Counter[Execution] = Counter()
        self._decisions: Counter[Decision] = Counter()
        self._stats_path = Path(stats_path).absolute() if stats_path is not None else None
        if type(stats_include_events) is not bool:
            raise TypeError("stats_include_events must be a boolean.")
        self._stats_include_events = stats_include_events
        self._snapshot_lock = Lock()
        self._stats_write_failed = False
        if self._stats_path is not None:
            self._write_stats(self._stats_path, self._stats_include_events)

    @property
    def events(self) -> tuple[Event, ...]:
        with self._lock:
            return tuple(self._events)

    def scan_input(self, text: str) -> ScanResult:
        return self._scan(Direction.INPUT, text)

    def scan_output(self, text: str) -> ScanResult:
        return self._scan(Direction.OUTPUT, text)

    def _scan(self, direction: Direction, text: object, *, proposal: _Proposal | None = None) -> ScanResult:
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
                                      (perf_counter() - start) * 1000,
                                      proposal.name if proposal else None, proposal.id if proposal else None))
            self._scan_directions[direction] += 1
            self._verdicts[classification.verdict] += 1
        self._publish_stats()
        return ScanResult(direction, classification.verdict, decision, reason, explanation,
                          classification.categories)

    def run_tool(self, request: ToolRequest, *, idempotency_key: str | None = None) -> ToolResult:
        """Submit a new proposal. The application registers the callable in advance."""
        if type(request) is not ToolRequest:
            raise TypeError("Expected a validated ToolRequest.")
        if self._ledger is not None:
            self._ledger.validate_key(idempotency_key)
        elif idempotency_key is not None:
            raise ValueError("Configure a ledger to use idempotency keys.")
        self._expire_pending()
        # Only a trusted registry identifier may enter events.
        name = request.tool_name if request.tool_name in self._tools else None
        proposal = _Proposal(request, name, idempotency_key)
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
                    "alignment_unavailable", "permission_unavailable", "validator_unavailable", "ledger_unavailable"}:
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
        try:
            tool = self._tools[proposal.name].bind(proposal.request.arguments)
        except Exception:
            return record("validation", Decision.REVIEW, "validator_unavailable",
                          "The application could not complete argument validation.")
        if tool is None:
            return record("validation", Decision.BLOCK, "invalid_arguments",
                          "The proposed arguments do not match this tool's schema.")
        try:
            permission = tool.permission()
            if type(permission) is not Permission:
                permission = None
        except Exception:
            permission = None
        if permission is None:
            return record("permission", Decision.REVIEW, "permission_unavailable",
                          "The permission check must complete before this tool can run.")
        if permission is Permission.DENY:
            return record("permission", Decision.BLOCK, "policy_denied",
                          "The application policy denies this action.")
        try:
            result = self._alignment.check(proposal.request) if self._alignment else None
            alignment = result.verdict if type(result) is AlignmentResult else Alignment.UNAVAILABLE
        except Exception:
            alignment = Alignment.UNAVAILABLE
        if alignment is Alignment.MISALIGNED:
            return record("alignment", Decision.BLOCK, "misaligned_action",
                          "This action conflicts with the original user request.")
        if alignment is Alignment.UNAVAILABLE:
            return record("alignment", Decision.REVIEW, "alignment_unavailable",
                          "The intent check must complete before this tool can run.")
        if reviewing and self._clock() >= proposal.deadline:
            return record("approval", Decision.BLOCK, "approval_expired", "The review period expired.")
        if self._ledger is not None and proposal.key is not None and reviewing:
            try:
                self._ledger.status(proposal.key)
            except LedgerUnavailable:
                return record("admission", Decision.REVIEW, "ledger_unavailable",
                              "The execution ledger must be available before this tool can run.")
        if not approved:
            if alignment is Alignment.UNCERTAIN:
                return record("alignment", Decision.REVIEW, "alignment_uncertain",
                              "Confirm that this exact action is what you intended.")
            if permission is Permission.ASK or reviewing:
                return record("approval", Decision.REVIEW, "user_approval_required",
                              "The user must approve this exact action before the tool can run.")
        if self._ledger is not None and proposal.key is not None:
            try:
                conflict = self._ledger.claim(proposal.key, proposal.request)
            except LedgerUnavailable:
                return record("admission", Decision.REVIEW, "ledger_unavailable",
                              "The execution ledger could not reserve this action.")
            if conflict == "duplicate_execution":
                return record("admission", Decision.BLOCK, conflict,
                              "An earlier attempt already reserved this action.")
            if conflict == "idempotency_conflict":
                return record("admission", Decision.BLOCK, conflict,
                              "This operation key belongs to a different action.")
            if reviewing and self._clock() >= proposal.deadline:
                try:
                    self._ledger.finish(proposal.key, "expired")
                except LedgerUnavailable:
                    pass  # A surviving reservation still prevents a later invocation.
                return record("approval", Decision.BLOCK, "approval_expired", "The review period expired.")
        if approved:
            allowed = record("approval", Decision.ALLOW, "user_approved",
                             "The user approved the reviewed action.")
        else:
            allowed = record("alignment", Decision.ALLOW, "aligned_action",
                             "Policy permits the action and it matches the user request.")
        return self._execute(proposal, tool, allowed)

    def _execute(self, proposal: _Proposal, tool: BoundTool, allowed: ToolResult) -> ToolResult:
        start = perf_counter()
        output = None
        try:
            output = tool.execute()
            if inspect.isawaitable(output):
                if inspect.iscoroutine(output):
                    output.close()
                raise TypeError("Tool execution must be synchronous.")
            execution = Execution.SUCCEEDED
        except Exception:
            execution = Execution.FAILED
        reason = f"execution_{execution.value}"
        explanation = allowed.explanation
        if execution is Execution.FAILED:
            explanation += " The tool raised an error or returned an unsupported awaitable."
        recorded = None
        if self._ledger is not None and proposal.key is not None:
            try:
                self._ledger.finish(proposal.key, execution)
                recorded = True
            except LedgerUnavailable:
                recorded = False
                reason = "outcome_unrecorded"
                explanation += " The outcome could not be written to the ledger. Reconcile the operation."
        with self._lock:
            self._executions[execution] += 1
            self._events.append(Event(datetime.now(timezone.utc), "execution", Decision.ALLOW, reason,
                                      (perf_counter() - start) * 1000, proposal.name, proposal.id,
                                      execution))
        self._publish_stats()
        result = replace(allowed, execution=execution, explanation=explanation, outcome_recorded=recorded)
        if tool.return_text and execution is Execution.SUCCEEDED:
            scan = self._scan(Direction.OUTPUT, output, proposal=proposal)
            return replace(result, output_scan=scan,
                           output_text=output if scan.decision is Decision.ALLOW else None)
        return result

    def _record(self, proposal: _Proposal, start: float, stage: str, decision: Decision,
                reason: str, explanation: str, permission: Permission | None,
                alignment: Alignment | None) -> ToolResult:
        with self._lock:
            summary = None
            if decision is Decision.REVIEW:
                if proposal.result is not None and self._clock() >= proposal.deadline:
                    stage, decision, reason = "approval", Decision.BLOCK, "approval_expired"
                    explanation = "The review period expired."
                elif proposal.result is None and len(self._pending) >= self._max_pending:
                    stage, decision, reason = "admission", Decision.BLOCK, "review_queue_full"
                    explanation = "There is no room for another pending review."
                else:
                    if proposal.result is None:
                        proposal.deadline = self._clock() + self._ttl
                    summary = f"{proposal.name} {proposal.request.arguments.preview()}"
            result = ToolResult(proposal.id, stage, proposal.name, decision, reason, explanation,
                                permission, alignment, review_summary=summary)
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
        self._publish_stats()
        return result

    def _expire_pending(self) -> None:
        now = self._clock()
        with self._lock:
            expired = [p for p in self._pending.values() if p.deadline <= now]
        for proposal in expired:
            # Never block an observer on an application callback or a concurrent approval.
            if proposal.lock.acquire(blocking=False):
                try:
                    result = proposal.result
                    if result is not None and result.decision is Decision.REVIEW:
                        self._record(proposal, perf_counter(), "approval", Decision.BLOCK,
                                     "approval_expired", "The review period expired.",
                                     result.permission, result.alignment)
                finally:
                    proposal.lock.release()

    def pending_reviews(self) -> tuple[ToolResult, ...]:
        """Private review snapshots for this guard's authenticated owner."""
        self._expire_pending()
        with self._lock:
            now = self._clock()
            return tuple(p.result for p in self._pending.values()
                         if p.result is not None and p.deadline > now)

    def stats(self) -> Stats:
        self._expire_pending()
        with self._lock:
            return self._stats_locked()

    def _stats_locked(self) -> Stats:
        return Stats(self._scan_directions[Direction.INPUT], self._scan_directions[Direction.OUTPUT],
                     self._verdicts[Verdict.UNSAFE], self._verdicts[Verdict.UNAVAILABLE],
                     self._decisions[Decision.ALLOW], self._decisions[Decision.BLOCK],
                     self._decisions[Decision.REVIEW], self._executions[Execution.SUCCEEDED],
                     self._executions[Execution.FAILED])

    @property
    def stats_write_failed(self) -> bool:
        """Whether the latest write to the configured live snapshot failed."""
        with self._snapshot_lock:
            return self._stats_write_failed

    def save_stats(self, path: str | Path | None = None, *, include_events: bool | None = None) -> Path:
        """Export current counts and optionally recent redacted events. Raises OSError on I/O failure."""
        destination = self._stats_path if path is None else Path(path).absolute()
        if destination is None:
            raise ValueError("Supply a snapshot path or configure stats_path on the guard.")
        if include_events is not None and type(include_events) is not bool:
            raise TypeError("include_events must be a boolean.")
        self._expire_pending()
        self._write_stats(destination, self._stats_include_events if include_events is None else include_events)
        return destination

    def _publish_stats(self) -> None:
        if self._stats_path is not None:
            try:
                self._write_stats(self._stats_path, self._stats_include_events)
            except OSError:
                # Monitoring failure must not turn a completed tool call into a retry.
                pass

    def _write_stats(self, path: Path, include_events: bool) -> None:
        # Capture *after* taking the writer lock so a slower writer cannot publish older counts.
        with self._snapshot_lock:
            with self._lock:
                captured = datetime.now(timezone.utc)
                stats = self._stats_locked()
                events = tuple(self._events) if include_events else None
            try:
                write_snapshot(path, captured, stats, events)
            except OSError:
                if path == self._stats_path:
                    self._stats_write_failed = True
                raise
            if path == self._stats_path:
                self._stats_write_failed = False
