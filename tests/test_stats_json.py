import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta
from threading import Event
from unittest.mock import Mock

import pytest

from quackguard import (
    Decision, DemoScanner, Execution, ExpectedAction, Guard, Permission, Tool, ToolRequest, UserDecision,
)


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_live_counts_and_explicit_export(tmp_path):
    live = tmp_path / "activity" / "live.json"
    guard = Guard(DemoScanner(), stats_path=live, max_events=1)
    initial = read(live)
    assert set(initial) == {"schema_version", "updated_at", "stats"}
    assert initial["schema_version"] == 1 and set(initial["stats"].values()) == {0}
    assert datetime.fromisoformat(initial["updated_at"]).utcoffset() == timedelta(0)
    guard.scan_input("private input")
    guard.scan_output("[flagged output fixture]")
    assert read(live)["stats"] == asdict(guard.stats())
    assert guard.stats().flagged_scans == guard.stats().unavailable_scans == 1
    archive = guard.save_stats(tmp_path / "archive.json", include_events=True)
    assert [event["reason_code"] for event in read(archive)["events"]] == ["content_flagged"]
    assert "events" not in read(live)
    guard.scan_input("more private input")
    assert read(archive)["stats"]["scanned_inputs"] == 1
    assert read(live)["stats"]["scanned_inputs"] == 2


def test_manual_mode_and_restart(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    guard = Guard(DemoScanner())
    guard.scan_input("private")
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(ValueError, match="path"):
        guard.save_stats()
    with pytest.raises(TypeError):
        guard.save_stats("live.json", include_events="false")
    with pytest.raises(TypeError):
        Guard(DemoScanner(), stats_path="live.json", stats_include_events="false")
    path = guard.save_stats("live.json")
    assert path == tmp_path / "live.json" and read(path)["stats"]["scanned_inputs"] == 1
    Guard(DemoScanner(), stats_path=path)
    assert read(path)["stats"]["scanned_inputs"] == 0


def test_events_and_proposals_match_live_state_without_private_data(tmp_path):
    path = tmp_path / "stats.json"
    clock = Mock(return_value=0)
    request = ToolRequest("private intent", "store", {"text": "private@example.test"})
    execute = Mock(return_value="private tool response")
    tool = Tool(execute=execute, validate=lambda _: True, permission=lambda _: Permission.ASK, return_text=True)
    guard = Guard(DemoScanner(), tools={"store": tool},
                  alignment=ExpectedAction(request), stats_path=path, stats_include_events=True, clock=clock)
    pending = guard.run_tool(request)
    assert read(path)["stats"]["held_proposals"] == 1
    execute.assert_not_called()
    result = guard.review(pending.proposal_id, request, UserDecision.ALLOW)
    assert result.execution is Execution.SUCCEEDED and result.output_text is None
    data = read(path)
    assert data["stats"] == asdict(guard.stats())
    assert data["stats"]["held_proposals"] == 0 and data["stats"]["allowed_proposals"] == 1
    assert data["stats"]["executions_succeeded"] == data["stats"]["unavailable_scans"] == 1
    assert [e["stage"] for e in data["events"]] == ["approval", "approval", "execution", "output"]
    assert all(e["proposal_id"] == pending.proposal_id for e in data["events"])
    assert set(data["events"][0]) == {
        "timestamp", "stage", "decision", "reason_code", "duration_ms", "tool_name",
        "proposal_id", "execution", "permission", "alignment",
    }
    guard.run_tool(ToolRequest("other intent", "private unregistered name", {}))
    assert "private" not in path.read_text()
    guard.run_tool(request)
    clock.return_value = 301
    assert guard.pending_reviews() == ()
    assert read(path)["stats"] == asdict(guard.stats())
    assert read(path)["events"][-1]["reason_code"] == "approval_expired"
    execute.assert_called_once()


def test_failed_publication_preserves_file_and_execution_result(tmp_path, monkeypatch):
    path = tmp_path / "stats.json"
    request = ToolRequest("Store the sample.", "store", {})
    execute = Mock(side_effect=RuntimeError("private failure"))
    tool = Tool(execute=execute, validate=lambda _: True, permission=lambda _: Permission.ALLOW)
    guard = Guard(DemoScanner(), tools={"store": tool},
                  alignment=ExpectedAction(request), stats_path=path)
    original = path.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", Mock(side_effect=OSError("disk full")))
        result = guard.run_tool(request)
        assert (result.decision, result.execution) == (Decision.ALLOW, Execution.FAILED)
        assert guard.stats_write_failed and path.read_bytes() == original
        with pytest.raises(OSError):
            guard.save_stats()
        assert list(tmp_path.iterdir()) == [path]
    guard.save_stats()
    assert not guard.stats_write_failed
    assert read(path)["stats"]["executions_failed"] == 1
    execute.assert_called_once()


def test_configured_path_is_checked_at_startup(tmp_path):
    with pytest.raises(OSError):
        Guard(DemoScanner(), stats_path=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_reader_sees_previous_document_until_new_one_is_complete(tmp_path, monkeypatch):
    path = tmp_path / "stats.json"
    guard = Guard(DemoScanner(), stats_path=path)
    ready, resume = Event(), Event()
    replace_file = os.replace

    def pause_before_replace(source, destination):
        ready.set()
        assert resume.wait(5)
        replace_file(source, destination)

    monkeypatch.setattr(os, "replace", pause_before_replace)
    with ThreadPoolExecutor() as workers:
        written = workers.submit(guard.scan_output, "sample")
        try:
            assert ready.wait(5)
            assert read(path)["stats"]["scanned_outputs"] == 0
            assert guard.stats().scanned_outputs == 1
        finally:
            resume.set()
        written.result()
    assert read(path)["stats"]["scanned_outputs"] == 1


def test_concurrent_publications_include_all_counts(tmp_path):
    path = tmp_path / "stats.json"
    guard = Guard(DemoScanner(), stats_path=path, stats_include_events=True, max_events=3)
    with ThreadPoolExecutor(max_workers=6) as workers:
        list(workers.map(guard.scan_input, ["synthetic input"] * 32))
    assert read(path)["stats"] == asdict(guard.stats())
    assert read(path)["stats"]["scanned_inputs"] == 32 and len(read(path)["events"]) == 3
