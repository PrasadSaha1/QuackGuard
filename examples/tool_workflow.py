"""Two different application tools with one registration and review API."""

from datetime import date
from pathlib import Path
from secrets import token_bytes
from tempfile import TemporaryDirectory

from quackguard import (
    Alignment, Decision, DemoScanner, FixtureAlignment, Guard, JSONObject, Permission,
    SQLiteLedger, Tool, ToolRequest, UserDecision,
)


def valid_event(fields: JSONObject) -> bool:
    if set(fields) != {"title", "date"} or not isinstance(fields["date"], str):
        return False
    title = fields["title"]
    if not isinstance(title, str) or not 1 <= len(title) <= 120:
        return False
    try:
        return date.fromisoformat(fields["date"]).isoformat() == fields["date"]
    except ValueError:
        return False


def main() -> None:
    notes: dict[str, str] = {}
    calendar: list[JSONObject] = []
    note = ToolRequest("Save the sample quiz date in my notes.", "save_note",
                       {"text": "The sample quiz is on Friday."})
    event = ToolRequest("Schedule the sample study session.", "add_event",
                        {"title": "Sample study session", "date": "2030-05-10"})

    def save_note(fields: JSONObject) -> str:
        text = fields["text"]
        assert isinstance(text, str)
        notes["quiz"] = text
        return text

    tools = {
        "save_note": Tool(execute=save_note,
                          validate=lambda a: set(a) == {"text"} and isinstance(a["text"], str),
                          permission=lambda _: Permission.ALLOW, return_text=True),
        "add_event": Tool(execute=calendar.append, validate=valid_event,
                          permission=lambda _: Permission.ASK),
    }
    with TemporaryDirectory() as directory:
        path = Path(directory) / "executions.sqlite"
        secret = token_bytes(32)  # Disposable demo. Production workers reuse a managed secret.
        scope = "demo-workspace/student"
        ledger = SQLiteLedger.create(path, secret=secret, scope=scope)
        checks = FixtureAlignment({note: Alignment.ALIGNED, event: Alignment.ALIGNED})
        guard = Guard(DemoScanner(), tools=tools, alignment=checks, ledger=ledger)
        saved = guard.run_tool(note, idempotency_key="note-001")
        print("Note reply:", saved.output_text)
        held = guard.run_tool(event, idempotency_key="calendar-001")
        print("Calendar proposal:", held.decision, held.reason_code)
        for pending in guard.pending_reviews():
            print("Private review:", pending.review_summary)
        try:
            answer = input("Add this event? [allow/deny, Enter to leave pending] ").strip().lower()
        except EOFError:
            answer = ""
        choice = {"allow": UserDecision.ALLOW, "deny": UserDecision.DENY}.get(answer)
        result = guard.review(held.proposal_id, event, choice)
        print("Calendar result:", result.decision, result.execution)
        reopened = SQLiteLedger(path, secret=secret, scope=scope)
        worker = Guard(DemoScanner(), tools=tools, alignment=checks, ledger=reopened)
        replay = worker.run_tool(note, idempotency_key="note-001")
        print("Note retry on a new guard:", replay.decision, replay.reason_code)
        print("Stored notes:", len(notes), "| Calendar entries:", len(calendar))
        assert replay.decision is Decision.BLOCK


if __name__ == "__main__":
    main()
