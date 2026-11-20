# QuackGuard

QuackGuard is a Python package for students and independent developers building their own AI agents. Scan a prompt or reply, then check a proposed tool call against application permissions and the user's original request. The first supported tool sends a plaintext email to one recipient.

## Run the student assistant

From the repository root, use Python 3.11 or later. The core has no third-party runtime dependencies.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python examples/offline_demo.py
python -m pytest
```

The demo runs locally without network access or credentials. It scans a syllabus request, releases one reply, and withholds two others. A fake mail sender then receives an aligned proposal. A second proposal would email a fictional roster even though the student only asked for a syllabus summary. The alignment check blocks it before the sender runs.

At the two local review prompts, enter `allow` for the study session and `deny` for the reminder, or replay these explicit decisions in a shell.

```sh
printf 'allow\ndeny\n' | python examples/offline_demo.py
```

| What the demo counts | Expected with those answers |
| --- | --- |
| Inputs / outputs scanned | 1 / 3 |
| Flagged / unavailable scans | 1 / 1 |
| Allowed / blocked / held proposals | 2 / 2 / 1 |
| Successful / failed executions | 2 / 0 |

The fifth proposal has no alignment fixture and stays pending. Pressing Enter or reaching end of input at an approval prompt also leaves that proposal pending. Fixtures demonstrate package behavior. They do not measure a model's detection accuracy.

## Integrate an email tool

```python
from quackguard import (
    Alignment, Decision, DemoScanner, Email, EmailTool, FixtureAlignment,
    Guard, Permission, ToolRequest,
)

message = Email("team@example.test", "Quiz date", "The sample quiz is on Friday.")
request = ToolRequest("Email the quiz date to the team.", "send_email", message)
mailbox = []
guard = Guard(
    DemoScanner(),
    tools={"send_email": EmailTool(mailbox.append, lambda email:
           Permission.ALLOW if email.recipient == "team@example.test" else Permission.DENY)},
    alignment=FixtureAlignment({request: Alignment.ALIGNED}),
)

incoming = guard.scan_input("Summarize the sample syllabus.")
if incoming.decision is Decision.ALLOW:
    candidate = "The sample quiz is on Friday."
    if guard.scan_output(candidate).decision is Decision.ALLOW:
        print(candidate)

result = guard.run_tool(request)
print(result.decision, result.reason_code, result.execution)
print(guard.stats())
```

Supply the original request from your application's trusted user interaction. Pass the proposed tool name and validated `Email` separately. Treat documents, tool responses, and agent-generated text as untrusted. Register the callable at construction so a proposal cannot substitute a different sender. Each call to `run_tool` starts a new proposal.

### Decisions and review

A `SAFE` scan clears only the supplied content in that direction. `UNSAFE` blocks release and `UNAVAILABLE` holds it for review. The caller releases a reply only on `ALLOW`.

| Tool check, in order | Result |
| --- | --- |
| Unregistered tool or denied permission | Block before alignment |
| Permission check unavailable | Hold until the check recovers |
| Clear conflict with the user request | Block, including when policy asks for approval |
| Required alignment check unavailable or malformed | Hold until the check recovers |
| Policy asks for approval or alignment is uncertain | Hold for the user's decision |
| Policy allows and alignment confirms the exact proposal | Call the registered tool once |

A tool result includes its stage, registered tool name, permission, alignment, final decision, reason code, explanation, and execution outcome. An unregistered tool name is omitted from results and events. Supply a custom `AlignmentCheck.check(request) -> AlignmentResult` for your application. `FixtureAlignment` matches the complete request and email exactly.

For a held result, show `result.explanation` and `result.review_summary` to the authenticated requesting user. The summary contains the destination and exact JSON-escaped subject and body. Render it as plain text in a private approval UI. Collect consent through that UI and pass the original request back to the guard.

```python
from quackguard import UserDecision

# In a trusted approval handler, after the user explicitly chooses Allow:
result = guard.review(result.proposal_id, request, UserDecision.ALLOW)
```

Pass `UserDecision.DENY` to decline or `None` to keep waiting. Raw strings, model output, and text in a document cannot approve a call. The integrating application owns authentication and routing to the correct user's guard. A changed request or email invalidates the pending proposal. Reviews expire after 300 seconds by default, configurable with `review_ttl`. The guard enforces expiration when the proposal is next reviewed or rechecked. An unknown, blocked, or already consumed ID raises `ValueError`.

When a required check was unavailable, call `guard.recheck(id, request)` after recovery. A successful recheck returns a new review prompt. A fresh affirmative decision is required. The guard revalidates permission and alignment before execution. Sender exceptions produce `execution == Execution.FAILED` while retaining the policy decision. Inspect the outcome before taking further action. There is no automatic retry.

## Optional Llama Guard scanner

Install the extra only when using a remote provider.

```sh
python -m pip install -e '.[llama]'
```

Configure `QUACKGUARD_ENDPOINT` as the full HTTPS chat-completions URL, `QUACKGUARD_MODEL` as the deployed model ID, and `QUACKGUARD_API_KEY` in the calling process's environment. Constructing the adapter explicitly selects remote scanning. The key is read at scan time. The adapter does not discover a provider or contact one in the offline demo.

```python
import os
from quackguard import Guard, LlamaGuardScanner

guard = Guard(LlamaGuardScanner(
    endpoint=os.environ["QUACKGUARD_ENDPOINT"],
    model=os.environ["QUACKGUARD_MODEL"],
    timeout=8.0,
))
result = guard.scan_input("Summarize the sample syllabus.")
```

The adapter sends one HTTPS POST with a bearer authorization header and a JSON body containing only `model` and `messages`. An input scan sends the supplied text as one user message. An output scan sends the fixed user message `Review the following reply.` followed by the supplied text as an assistant message. No conversation history, email proposals, or approval records are attached. Use a deployment that applies the Llama Guard chat template to the final message and returns a classification for that role. The configured timeout applies to connection and socket reads. Redirects are disabled.

The supported response is HTTP 200 with one chat-completion choice. Its `message.content` must be a string containing lowercase `safe`, `unsafe`, or `unsafe` followed by a newline and comma-separated unique category codes S1 through S14. Surrounding whitespace is ignored. A supplied message role must be `assistant` and a supplied `finish_reason` must be `stop`. Extra prose, invalid categories, tool calls, truncated completions, transport failures, and missing credentials produce `UNAVAILABLE`. Provider responses and exceptions are excluded from QuackGuard events. See the [Llama Guard 3 model card](https://huggingface.co/meta-llama/Llama-Guard-3-8B).

## Coverage and activity

QuackGuard sees only the text you explicitly scan and the actions you route through `run_tool`. Calling the sender directly bypasses QuackGuard. The package provides no process isolation, network interception, or sandbox for agent code. The email wrapper accepts a single ASCII mailbox and bounded plaintext subject and body. Use a sender with matching semantics.

`guard.events` returns a snapshot of in-memory events with UTC timestamps, stage, decision, reason code, elapsed milliseconds, registered tool name and proposal ID where applicable. Decision events record permission and alignment. Execution events report the sender outcome separately. Raw inputs, replies, arguments, addresses, API keys, and provider bodies are excluded. Keep review previews out of your application logs. Pending requests and previews remain in process memory for review, and all state is lost on restart.

`guard.stats()` counts scans, outcomes, and each proposal's latest decision. Reviewing the same proposal moves it between totals. The execution counts track actual sender attempts.
