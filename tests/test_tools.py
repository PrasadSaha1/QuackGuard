from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from unittest.mock import Mock

import pytest

from quackguard import (
    Alignment, AlignmentResult, Decision, DemoScanner, Email, EmailTool, Execution,
    FixtureAlignment, Guard, Permission, ToolRequest, UserDecision,
)

EMAIL = Email("student@example.test", "Sample plan", "Fixture body.")
REQUEST = ToolRequest("Email the student the sample plan.", "send_email", EMAIL)


def setup_guard(permission=Permission.ALLOW, alignment=Alignment.ALIGNED, **guard_options):
    sender = Mock()
    check = Mock()
    check.check.return_value = AlignmentResult(alignment)
    policy = Mock(return_value=permission)
    guard = Guard(DemoScanner(), tools={"send_email": EmailTool(sender, policy)},
                  alignment=check, **guard_options)
    return guard, sender, check, policy


def test_safe_content_cannot_override_permission():
    guard, sender, check, _ = setup_guard(Permission.DENY)
    assert guard.scan_input("Summarize the sample syllabus.").decision is Decision.ALLOW
    blocked = guard.run_tool(REQUEST)
    assert (blocked.decision, blocked.permission, blocked.alignment, blocked.reason_code) == (
        Decision.BLOCK, Permission.DENY, None, "policy_denied")
    with pytest.raises(ValueError):
        guard.review(blocked.proposal_id, REQUEST, UserDecision.ALLOW)
    check.check.assert_not_called()
    sender.assert_not_called()


def test_unregistered_name_never_enters_events():
    guard, sender, check, policy = setup_guard()
    blocked = guard.run_tool(replace(REQUEST, tool_name="secret@example.test"))
    assert blocked.reason_code == "unregistered_tool"
    assert blocked.tool_name is None
    assert "secret@" not in repr(guard.events)
    sender.assert_not_called()
    check.check.assert_not_called()
    policy.assert_not_called()


@pytest.mark.parametrize("permission", [Permission.ALLOW, Permission.ASK])
def test_conflict_blocks_even_if_policy_would_ask(permission):
    guard, sender, _, _ = setup_guard(permission, Alignment.MISALIGNED)
    blocked = guard.run_tool(REQUEST)
    assert (blocked.decision, blocked.alignment, blocked.reason_code) == (
        Decision.BLOCK, Alignment.MISALIGNED, "misaligned_action")
    with pytest.raises(ValueError):
        guard.review(blocked.proposal_id, REQUEST, UserDecision.ALLOW)
    sender.assert_not_called()


def test_alignment_checks_exact_intent_and_arguments():
    sender = Mock()
    guard = Guard(DemoScanner(), tools={"send_email": EmailTool(sender, lambda _: Permission.ALLOW)},
                  alignment=FixtureAlignment({REQUEST: Alignment.ALIGNED}))
    held = guard.run_tool(replace(REQUEST, user_request="Only summarize the syllabus."))
    assert held.reason_code == "alignment_unavailable"
    sender.assert_not_called()
    result = guard.run_tool(REQUEST)
    assert (result.decision, result.execution) == (Decision.ALLOW, Execution.SUCCEEDED)
    sender.assert_called_once_with(EMAIL)


@pytest.mark.parametrize("permission,alignment,reason", [
    (Permission.ASK, Alignment.ALIGNED, "user_approval_required"),
    (Permission.ALLOW, Alignment.UNCERTAIN, "alignment_uncertain"),
])
def test_missing_and_denied_approval(permission, alignment, reason):
    guard, sender, _, _ = setup_guard(permission, alignment)
    held = guard.run_tool(REQUEST)
    assert held.reason_code == reason
    assert all(text in held.review_summary for text in (EMAIL.recipient, EMAIL.subject, EMAIL.body))
    for missing in (None, "allow", "user approved", True, Alignment.ALIGNED, Permission.ALLOW):
        assert guard.review(held.proposal_id, REQUEST, missing).decision is Decision.REVIEW
        sender.assert_not_called()
    denied = guard.review(held.proposal_id, REQUEST, UserDecision.DENY)
    assert (denied.decision, denied.reason_code) == (Decision.BLOCK, "user_denied")
    with pytest.raises(ValueError):
        guard.review(held.proposal_id, REQUEST, UserDecision.ALLOW)
    sender.assert_not_called()


@pytest.mark.parametrize("changed", [
    replace(REQUEST, arguments=replace(EMAIL, recipient="elsewhere@example.test")),
    replace(REQUEST, arguments=replace(EMAIL, subject="Other subject")),
    replace(REQUEST, arguments=replace(EMAIL, body="Changed body.")),
    replace(REQUEST, tool_name="another_tool"),
    replace(REQUEST, user_request="An edited request."),
])
def test_changed_request_invalidates_review(changed):
    guard, sender, _, _ = setup_guard(Permission.ASK)
    held = guard.run_tool(REQUEST)
    assert guard.review(held.proposal_id, changed, UserDecision.ALLOW).reason_code == "proposal_changed"
    with pytest.raises(ValueError):
        guard.review(held.proposal_id, REQUEST, UserDecision.ALLOW)
    sender.assert_not_called()


def test_expired_approval():
    now = [5.0]
    guard, sender, _, _ = setup_guard(Permission.ASK, review_ttl=2, clock=lambda: now[0])
    held = guard.run_tool(REQUEST)
    now[0] = 7.0
    assert guard.review(held.proposal_id, REQUEST, UserDecision.ALLOW).reason_code == "approval_expired"
    sender.assert_not_called()


def test_required_check_recovers_without_carrying_old_consent():
    guard, sender, check, _ = setup_guard(alignment=Alignment.UNAVAILABLE)
    held = guard.run_tool(REQUEST)
    assert held.reason_code == "alignment_unavailable"
    assert guard.review(held.proposal_id, REQUEST, UserDecision.ALLOW).decision is Decision.REVIEW
    check.check.return_value = AlignmentResult(Alignment.ALIGNED)
    assert guard.review(held.proposal_id, REQUEST, UserDecision.ALLOW).decision is Decision.REVIEW
    assert guard.recheck(held.proposal_id, REQUEST).reason_code == "user_approval_required"
    sender.assert_not_called()
    approved = guard.review(held.proposal_id, REQUEST, UserDecision.ALLOW)
    assert approved.execution is Execution.SUCCEEDED
    sender.assert_called_once_with(EMAIL)


def test_policy_and_alignment_revalidated_on_approval():
    guard, sender, check, policy = setup_guard(Permission.ASK)
    held = guard.run_tool(REQUEST)
    policy.return_value = Permission.DENY
    assert guard.review(held.proposal_id, REQUEST, UserDecision.ALLOW).reason_code == "policy_denied"
    assert check.check.call_count == 1
    sender.assert_not_called()


def test_unusable_policy_or_alignment_is_held():
    guard, sender, check, policy = setup_guard()
    policy.return_value = "allow"
    held = guard.run_tool(REQUEST)
    assert held.reason_code == "permission_unavailable"
    check.check.assert_not_called()
    policy.side_effect = RuntimeError("synthetic-secret")
    assert guard.recheck(held.proposal_id, REQUEST).reason_code == "permission_unavailable"
    policy.side_effect = None
    policy.return_value = Permission.ALLOW
    check.check.return_value = "aligned"
    assert guard.recheck(held.proposal_id, REQUEST).reason_code == "alignment_unavailable"
    check.check.side_effect = TimeoutError("synthetic-secret")
    assert guard.recheck(held.proposal_id, REQUEST).reason_code == "alignment_unavailable"
    assert "synthetic-secret" not in repr(guard.events)
    sender.assert_not_called()


def test_racing_approvals_consume_only_once():
    guard, sender, _, _ = setup_guard(Permission.ASK)
    held = guard.run_tool(REQUEST)
    barrier = Barrier(2)

    def approve():
        barrier.wait(timeout=5)
        try:
            return guard.review(held.proposal_id, REQUEST, UserDecision.ALLOW).execution
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        runs = [pool.submit(approve) for _ in range(2)]
        outcomes = [run.result(timeout=5) for run in runs]
    assert outcomes.count(Execution.SUCCEEDED) == 1
    assert outcomes.count(None) == 1
    sender.assert_called_once_with(EMAIL)
    assert guard.stats().allowed_proposals == 1
    assert guard.stats().held_proposals == 0


def test_failure_is_recorded_after_admission_and_is_never_retried():
    guard, sender, _, _ = setup_guard(Permission.ASK)
    held = guard.run_tool(REQUEST)

    def fail(_email):
        assert guard.events[-1].reason_code == "user_approved"
        raise RuntimeError("private-provider-reply")

    sender.side_effect = fail
    result = guard.review(held.proposal_id, REQUEST, UserDecision.ALLOW)
    assert (result.decision, result.execution) == (Decision.ALLOW, Execution.FAILED)
    with pytest.raises(ValueError):
        guard.review(held.proposal_id, REQUEST, UserDecision.ALLOW)
    sender.assert_called_once_with(EMAIL)
    assert guard.stats().executions_failed == 1
    assert guard.stats().executions_succeeded == 0
    assert guard.events[-1].reason_code == "execution_failed"
    assert "private-provider-reply" not in repr(guard.events)


def test_events_and_totals_are_redacted_and_count_each_proposal_once():
    guard, sender, _, policy = setup_guard(Permission.ASK)
    guard.scan_input("Summarize the sample syllabus.")
    guard.scan_output("[flagged output fixture]")
    guard.scan_output("private output")
    held = guard.run_tool(REQUEST)
    guard.review(held.proposal_id, REQUEST, UserDecision.ALLOW)
    other = guard.run_tool(REQUEST)
    guard.review(other.proposal_id, REQUEST, UserDecision.DENY)
    policy.return_value = Permission.ASK
    guard.run_tool(REQUEST)
    stats = guard.stats()
    assert (stats.scanned_inputs, stats.scanned_outputs, stats.flagged_scans,
            stats.unavailable_scans) == (1, 2, 1, 1)
    assert (stats.allowed_proposals, stats.blocked_proposals, stats.held_proposals,
            stats.executions_succeeded, stats.executions_failed) == (1, 1, 1, 1, 0)
    sender.assert_called_once_with(EMAIL)
    assert all(e.timestamp.utcoffset().total_seconds() == 0 and e.duration_ms >= 0 for e in guard.events)
    for private in (EMAIL.recipient, EMAIL.subject, EMAIL.body, REQUEST.user_request,
                    "private output", "[flagged output fixture]", "Summarize the sample syllabus."):
        assert private not in repr(guard.events)


@pytest.mark.parametrize("recipient", ["bad", "one@example.test,two@example.test",
                                      "one@example.test\r\nCc: two@example.test"])
def test_email_rejects_ambiguous_destinations(recipient):
    with pytest.raises(ValueError):
        replace(EMAIL, recipient=recipient)


def test_review_summary_escapes_terminal_controls():
    guard, sender, _, _ = setup_guard(Permission.ASK)
    request = replace(REQUEST, arguments=replace(EMAIL, body="Line one\nLine two\x1b[31m"))
    held = guard.run_tool(request)
    assert "\n" not in held.review_summary and "\x1b" not in held.review_summary
    sender.assert_not_called()
