import json
import subprocess
import sys
from pathlib import Path


def test_documented_walkthrough():
    demo = Path(__file__).resolve().parents[1] / "examples" / "offline_demo.py"
    output = subprocess.run([sys.executable, str(demo)], input="allow\ndeny\n", capture_output=True,
                            text=True, check=True).stdout
    assert "  Reply: The sample quiz is on Friday." in output
    assert "[flagged output fixture]" not in output and "[unseen reply]" not in output
    assert "misaligned_action" in output and "alignment_unavailable" in output
    assert "allowed_proposals: 2" in output and "blocked_proposals: 2" in output
    assert "held_proposals: 1" in output and "Messages in the fake mailbox: 2" in output


def test_general_tools_and_worker_retry():
    demo = Path(__file__).resolve().parents[1] / "examples" / "tool_workflow.py"
    for answer, count in (("allow\n", 1), ("deny\n", 0), ("", 0)):
        run = subprocess.run([sys.executable, str(demo)], input=answer, capture_output=True,
                             text=True, timeout=10, check=True)
        assert f"Stored notes: 1 | Calendar entries: {count}" in run.stdout
        assert "Note retry on a new guard: BLOCK duplicate_execution" in run.stdout
        assert "Note reply: The sample quiz is on Friday." in run.stdout


def test_demo_writes_readable_report(tmp_path):
    demo = Path(__file__).resolve().parents[1] / "examples" / "offline_demo.py"
    path = tmp_path / "stats.json"
    run = subprocess.run([sys.executable, str(demo), "--stats-json", str(path), "--include-events"],
                         input="allow\ndeny\n", text=True, capture_output=True, timeout=10, check=True)
    assert str(path) in run.stdout
    data = json.loads(path.read_text())
    assert data["stats"] == dict(scanned_inputs=1, scanned_outputs=3, flagged_scans=1, unavailable_scans=1,
                                 allowed_proposals=2, blocked_proposals=2, held_proposals=1,
                                 executions_succeeded=2, executions_failed=0)
    assert len(data["events"]) == 13
