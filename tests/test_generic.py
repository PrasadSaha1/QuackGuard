from dataclasses import replace
from unittest.mock import Mock

import pytest

from quackguard import (
    Decision, DemoScanner, Email, EmailTool, Execution, ExpectedAction, Guard,
    Permission, Tool, ToolRequest, UserDecision, Verdict,
)


def setup_action(data=None, *, permission=Permission.ALLOW, **options):
    request = ToolRequest("Store the synthetic note.", "store", data if data is not None else {"id": 1})
    execute = Mock(return_value="The sample quiz is on Friday.")
    validate = Mock(return_value=True)
    check = Mock(wraps=ExpectedAction(request))
    rule = Mock(return_value=permission)
    tool = Tool(execute=execute, validate=validate, permission=rule, return_text=True)
    guard = Guard(DemoScanner(), tools={"store": tool}, alignment=check, **options)
    return guard, request, execute, validate, rule, check


def test_nested_snapshot_and_each_callback_have_isolated_values():
    raw = {"participants": ["member"], "meta": {"private": True}}
    guard, request, execute, validate, rule, _ = setup_action(raw, permission=Permission.ASK)
    raw["participants"].append("other")

    def validator(values):
        values["participants"].clear()
        return True

    def policy(values):
        assert values["participants"] == ["member"]
        values["meta"]["private"] = False
        return Permission.ASK

    validate.side_effect = validator
    rule.side_effect = policy
    held = guard.run_tool(request)
    assert held.decision is Decision.REVIEW
    assert "other" not in held.review_summary and "member" in held.review_summary
    request.arguments["participants"].append("mutated")
    result = guard.review(held.proposal_id, request, UserDecision.ALLOW)
    assert result.execution is Execution.SUCCEEDED
    execute.assert_called_once_with({"participants": ["member"], "meta": {"private": True}})
    assert "member" not in repr(guard.events)


@pytest.mark.parametrize("original,changed", [(1, True), (1, 1.0), ([1, 2], [2, 1])])
def test_json_types_and_array_order_are_bound_to_approval(original, changed):
    guard, request, execute, *_ = setup_action({"n": original}, permission=Permission.ASK)
    held = guard.run_tool(request)
    result = guard.review(held.proposal_id, replace(request, arguments={"n": changed}), UserDecision.ALLOW)
    assert result.reason_code == "proposal_changed"
    execute.assert_not_called()


def test_canonical_object_order_and_structured_intent():
    guard, request, execute, *_ = setup_action({"a": 1, "b": 2})
    same = replace(request, arguments={"b": 2, "a": 1})
    assert request == same and hash(request) == hash(same)
    assert guard.run_tool(same).decision is Decision.ALLOW
    assert guard.run_tool(replace(request, arguments={})).reason_code == "misaligned_action"
    assert guard.run_tool(replace(request, user_request="Unknown intent")).reason_code == "alignment_unavailable"
    execute.assert_called_once()


@pytest.mark.parametrize("bad", [{"n": float("nan")}, {"n": float("inf")}, {"n": object()},
                                 {"n": (1, 2)}, {1: "coercion"}, {"s": "a" * 66_000}])
def test_invalid_json_is_rejected_without_echoing_payload(bad):
    with pytest.raises(ValueError):
        ToolRequest("Store it.", "store", bad)


def test_recursive_container_is_rejected():
    cyclic = []
    cyclic.append(cyclic)
    with pytest.raises(ValueError, match="depth"):
        ToolRequest("Store it.", "store", {"value": cyclic})


def test_validation_precedes_policy_alignment_and_execution():
    guard, request, execute, validate, rule, check = setup_action()
    validate.return_value = False
    result = guard.run_tool(request)
    assert (result.stage, result.decision, result.reason_code) == (
        "validation", Decision.BLOCK, "invalid_arguments")
    rule.assert_not_called()
    check.check.assert_not_called()
    execute.assert_not_called()


def test_validator_outage_requires_recheck_and_new_user_decision():
    guard, request, execute, validate, rule, _ = setup_action()
    validate.side_effect = RuntimeError("secret detail")
    held = guard.run_tool(request)
    assert held.reason_code == "validator_unavailable"
    rule.assert_not_called()
    validate.side_effect = None
    assert guard.review(held.proposal_id, request, UserDecision.ALLOW).decision is Decision.REVIEW
    assert guard.recheck(held.proposal_id, request).reason_code == "user_approval_required"
    execute.assert_not_called()
    assert guard.review(held.proposal_id, request, UserDecision.ALLOW).execution is Execution.SUCCEEDED
    assert "secret detail" not in repr(guard.events)


def test_argument_models_do_not_cross_tool_boundaries():
    guard, request, execute, *_ = setup_action()
    email = Email("team@example.test", "Example", "Fixture.")
    assert guard.run_tool(replace(request, arguments=email)).reason_code == "invalid_arguments"
    guard = Guard(DemoScanner(), tools={"store": EmailTool(execute, lambda _: Permission.ALLOW)})
    assert guard.run_tool(request).reason_code == "invalid_arguments"
    execute.assert_not_called()


@pytest.mark.parametrize("output,verdict", [
    ("The sample quiz is on Friday.", Verdict.SAFE), ("[flagged output fixture]", Verdict.UNSAFE),
    ("unknown private output", Verdict.UNAVAILABLE), ({"sensitive": 1}, Verdict.UNAVAILABLE),
])
def test_tool_output_is_released_only_after_a_safe_scan(output, verdict):
    guard, request, execute, *_ = setup_action()
    execute.return_value = output
    result = guard.run_tool(request)
    assert result.execution is Execution.SUCCEEDED
    assert result.output_scan.verdict is verdict
    assert result.output_text == (output if verdict is Verdict.SAFE else None)
    assert guard.events[-1].proposal_id == result.proposal_id
    assert str(output) not in repr((guard.events, result))
    assert guard.stats().executions_succeeded == guard.stats().scanned_outputs == 1


def test_bounded_review_queue_reclaims_expired_entries_and_events_keep_lifetime_totals():
    now = [0.]
    guard, request, execute, *_ = setup_action(permission=Permission.ASK, max_pending=1, max_events=2,
                                               clock=lambda: now[0], review_ttl=5)
    first = guard.run_tool(request)
    assert guard.pending_reviews() == (first,)
    assert guard.run_tool(request).reason_code == "review_queue_full"
    now[0] = 5.
    assert not guard.pending_reviews()
    assert guard.stats().blocked_proposals == 2 and guard.stats().held_proposals == 0
    assert guard.events[-1].reason_code == "approval_expired"
    assert guard.run_tool(request).decision is Decision.REVIEW
    for _ in range(8):
        guard.scan_output("[flagged output fixture]")
    assert len(guard.events) == 2 and guard.stats().flagged_scans == 8
    assert guard.stats().scanned_outputs == 8 and guard.stats().held_proposals == 1
    execute.assert_not_called()


def test_unexpected_async_execution_is_visible_as_failure():
    async def future():
        return "ignored"

    guard, request, execute, *_ = setup_action()
    execute.side_effect = lambda _: future()
    result = guard.run_tool(request)
    assert result.execution is Execution.FAILED and result.output_scan is None


def test_malformed_validator_cannot_block_or_authorize_as_a_schema_verdict():
    guard, request, execute, validate, rule, _ = setup_action()
    validate.return_value = 1
    assert guard.run_tool(request).reason_code == "validator_unavailable"
    rule.assert_not_called()
    execute.assert_not_called()


def test_annotations_are_resolvable_for_application_integrations():
    from typing import get_type_hints

    assert "arguments" in get_type_hints(ToolRequest.__init__)
    assert "validate" in get_type_hints(Tool.__init__)
