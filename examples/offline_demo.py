"""Student assistant walkthrough. Every message and mailbox here is synthetic."""

from dataclasses import asdict

from quackguard import (
    Alignment, Decision, DemoScanner, Email, EmailTool, FixtureAlignment, Guard,
    Permission, ToolRequest, ToolResult, UserDecision,
)


def release_reply(guard: Guard, candidate: str) -> str | None:
    scan = guard.scan_output(candidate)
    print(f"{scan.stage} | {scan.decision} | {scan.reason_code} | {scan.explanation}")
    if scan.decision is Decision.ALLOW:
        return candidate
    return None


def show(result: ToolResult) -> None:
    print(f"{result.stage} | {result.tool_name} | {result.decision} | {result.reason_code}"
          f" | {result.explanation} | execution={result.execution}")
    if result.review_summary:
        print("  Proposed action:", result.review_summary)


def ask_user(guard: Guard, request: ToolRequest) -> None:
    held = guard.run_tool(request)
    show(held)
    if held.decision is not Decision.REVIEW:
        return
    try:
        answer = input("Local decision [allow/deny, Enter to leave pending]: ").strip().lower()
    except EOFError:
        answer = ""
    choice = {"allow": UserDecision.ALLOW, "deny": UserDecision.DENY}.get(answer)
    show(guard.review(held.proposal_id, request, choice))


def main() -> None:
    inbox: list[Email] = []
    team = Email("team@example.test", "Quiz date", "The sample quiz is on Friday.")
    guest = Email("guest@example.test", "Study session", "The sample session starts at noon.")
    reminder = Email("guest@example.test", "Reminder", "Bring the sample worksheet.")
    outside = Email("archive@example.test", "Sample roster", "Fictional record A-001.")
    recap = ToolRequest("Email the quiz date to the team.", "send_email", team)
    # An untrusted syllabus suggested exporting a roster. The app keeps the user's
    # original intent even if the agent proposes an email based on the document.
    conflict = ToolRequest("Summarize the sample syllabus.", "send_email", outside)
    invite = ToolRequest("Email the guest the study session time.", "send_email", guest)
    followup = ToolRequest("Email the guest a worksheet reminder.", "send_email", reminder)
    unchecked = ToolRequest("Email the team another update.", "send_email", team)

    def permission(email: Email) -> Permission:
        return {"team@example.test": Permission.ALLOW, "archive@example.test": Permission.ALLOW,
                "guest@example.test": Permission.ASK}.get(email.recipient, Permission.DENY)

    guard = Guard(DemoScanner(), tools={"send_email": EmailTool(inbox.append, permission)},
                  alignment=FixtureAlignment({recap: Alignment.ALIGNED, conflict: Alignment.MISALIGNED,
                                              invite: Alignment.ALIGNED, followup: Alignment.ALIGNED}))

    print("Student assistant | local fixtures and an in-memory mailbox")
    scanned = guard.scan_input(conflict.user_request)
    print(f"{scanned.stage} | {scanned.decision} | {scanned.reason_code} | {scanned.explanation}")
    if scanned.decision is not Decision.ALLOW:
        return
    for candidate in ("The sample quiz is on Friday.", "[flagged output fixture]", "[unseen reply]"):
        released = release_reply(guard, candidate)
        if released is not None:
            print("  Reply:", released)

    show(guard.run_tool(recap))
    print("Syllabus scenario: the user requested a summary, but the agent proposed a roster email.")
    show(guard.run_tool(conflict))
    ask_user(guard, invite)
    ask_user(guard, followup)
    show(guard.run_tool(unchecked))

    print("\nActivity")
    for name, count in asdict(guard.stats()).items():
        print(f"  {name}: {count}")
    print(f"Messages in the fake mailbox: {len(inbox)}")


if __name__ == "__main__":
    main()
