import multiprocessing
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from quackguard import (
    Decision, DemoScanner, Execution, ExpectedAction, Guard, LedgerUnavailable, Permission,
    SQLiteLedger, Tool, ToolRequest, UserDecision,
)

KEY = b"x" * 32
SCOPE = "workspace/person-A"
REQUEST = ToolRequest("Save the synthetic draft.", "save", {"text": "private fixture"})
OP = "synthetic-operation"


def make_guard(ledger, execute, request=REQUEST, permission=Permission.ALLOW, **kwargs):
    tool = Tool(execute=execute, validate=lambda _: True, permission=lambda _: permission)
    return Guard(DemoScanner(), tools={"save": tool}, alignment=ExpectedAction(request),
                 ledger=ledger, **kwargs)


@pytest.fixture
def ledger(tmp_path):
    return SQLiteLedger.create(tmp_path / "ops.db", secret=KEY, scope=SCOPE)


def test_key_required_and_no_silent_downgrade(ledger):
    send = Mock()
    for key in (None, "", 12, "a" * 257):
        with pytest.raises(ValueError):
            make_guard(ledger, send).run_tool(REQUEST, idempotency_key=key)
    with pytest.raises(ValueError):
        make_guard(None, send).run_tool(REQUEST, idempotency_key=OP)
    send.assert_not_called()


def test_restart_replay_changed_request_scope_and_redaction(ledger, tmp_path):
    execute = Mock()
    path = tmp_path / "ops.db"
    first = make_guard(ledger, execute).run_tool(REQUEST, idempotency_key=OP)
    assert first.outcome_recorded and ledger.status(OP) == "succeeded"
    reopened = SQLiteLedger(path, secret=KEY, scope=SCOPE)
    assert make_guard(reopened, execute).run_tool(REQUEST, idempotency_key=OP).reason_code == "duplicate_execution"
    changed = ToolRequest(REQUEST.user_request, "save", {"text": "other"})
    assert make_guard(reopened, execute, changed).run_tool(changed, idempotency_key=OP).reason_code == "idempotency_conflict"
    execute.assert_called_once()
    other = SQLiteLedger(path, secret=KEY, scope="workspace/person-B")
    assert make_guard(other, execute).run_tool(REQUEST, idempotency_key=OP).decision is Decision.ALLOW
    raw = path.read_bytes()
    for field in (REQUEST.user_request, "private fixture", OP, SCOPE, KEY.decode()):
        assert field.encode() not in raw


def test_missing_existing_file_wrong_secret_and_reprovision_fail(ledger, tmp_path):
    path = tmp_path / "ops.db"
    with pytest.raises(FileExistsError):
        SQLiteLedger.create(path, secret=KEY, scope=SCOPE)
    with pytest.raises(LedgerUnavailable):
        SQLiteLedger(path, secret=b"y" * 32, scope=SCOPE)
    path.unlink()
    with pytest.raises(LedgerUnavailable):
        SQLiteLedger(path, secret=KEY, scope=SCOPE)
    assert not path.exists()


def test_storage_outage_never_calls_tool_and_recovery_requires_consent(ledger, monkeypatch):
    execute = Mock()
    guard = make_guard(ledger, execute)
    with monkeypatch.context() as patch:
        patch.setattr(ledger, "claim", Mock(side_effect=LedgerUnavailable()))
        held = guard.run_tool(REQUEST, idempotency_key=OP)
        assert held.reason_code == "ledger_unavailable"
    assert guard.review(held.proposal_id, REQUEST, UserDecision.ALLOW).decision is Decision.REVIEW
    assert guard.recheck(held.proposal_id, REQUEST).reason_code == "user_approval_required"
    execute.assert_not_called()
    guard.review(held.proposal_id, REQUEST, UserDecision.ALLOW)
    execute.assert_called_once()


def test_failed_execution_and_failed_outcome_write_are_never_retried(ledger, monkeypatch):
    execute = Mock(side_effect=RuntimeError("private error"))
    guard = make_guard(ledger, execute)
    failed = guard.run_tool(REQUEST, idempotency_key=OP)
    assert failed.execution is Execution.FAILED and ledger.status(OP) == "failed"
    assert guard.run_tool(REQUEST, idempotency_key=OP).reason_code == "duplicate_execution"
    execute.side_effect = None
    monkeypatch.setattr(ledger, "finish", Mock(side_effect=LedgerUnavailable()))
    result = guard.run_tool(REQUEST, idempotency_key="another")
    assert result.execution is Execution.SUCCEEDED and result.outcome_recorded is False
    assert guard.events[-1].reason_code == "outcome_unrecorded"
    assert ledger.status("another") == "started"
    assert guard.run_tool(REQUEST, idempotency_key="another").reason_code == "duplicate_execution"
    assert execute.call_count == 2


def test_review_expiring_during_reservation_cannot_execute(ledger, monkeypatch):
    now = [0.]
    original = ledger.claim

    def slow_claim(key, request):
        result = original(key, request)
        now[0] = 60.
        return result

    monkeypatch.setattr(ledger, "claim", slow_claim)
    execute = Mock()
    guard = make_guard(ledger, execute, permission=Permission.ASK, clock=lambda: now[0], review_ttl=30)
    held = guard.run_tool(REQUEST, idempotency_key=OP)
    expired = guard.review(held.proposal_id, REQUEST, UserDecision.ALLOW)
    assert expired.reason_code == "approval_expired" and ledger.status(OP) == "expired"
    execute.assert_not_called()


def _worker(directory, mode, barrier=None):
    path = Path(directory)
    ledger = SQLiteLedger(path / "ops.db", secret=KEY, scope=SCOPE)

    def effect(_):
        with (path / "effects").open("a") as file:
            file.write("one attempt\n")
        if mode == "crash":
            os._exit(23)

    if barrier:
        barrier.wait(timeout=10)
    result = make_guard(ledger, effect).run_tool(REQUEST, idempotency_key=OP)
    if result.decision is Decision.BLOCK and result.reason_code != "duplicate_execution":
        raise AssertionError(result)


def test_two_processes_reserve_one_execution(ledger, tmp_path):
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(2)
    workers = [ctx.Process(target=_worker, args=(str(tmp_path), "normal", barrier)) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=15)
        assert worker.exitcode == 0
    assert (tmp_path / "effects").read_text().splitlines() == ["one attempt"]
    assert ledger.status(OP) == "succeeded"


def test_crash_after_side_effect_stays_ambiguous_and_is_never_replayed(ledger, tmp_path):
    ctx = multiprocessing.get_context("spawn")
    worker = ctx.Process(target=_worker, args=(str(tmp_path), "crash"))
    worker.start()
    worker.join(timeout=15)
    assert worker.exitcode == 23
    assert ledger.status(OP) == "started"
    _worker(str(tmp_path), "normal")
    assert (tmp_path / "effects").read_text().splitlines() == ["one attempt"]
