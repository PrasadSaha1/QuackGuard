# QuackGuard

QuackGuard checks the text and tool calls that an application routes through it. Register any synchronous tool that accepts a JSON object, define its argument validator and permission rule, and check whether each proposed action matches the user's intent. Applications get a decision, a reason, and a separate execution outcome.

Use it for operations such as saving notes, booking calendar events, or sending mail. The optional SQLite execution ledger coordinates workers on one host and preserves duplicate protection across restarts. [Deployment and recovery guide](docs/operations.md)

## Install and run locally

From the repository root, use Python 3.11 or later. The core has no third-party runtime dependencies.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python examples/tool_workflow.py
python examples/offline_demo.py
python -m pytest
```

The general tool demo saves a note, prompts for a calendar entry, then opens a second guard on the same ledger and retries the note with the same operation key. Enter `allow` to record one calendar entry or `deny` to record zero. In both cases, the note executes once. Both tools use in-memory fixtures. The temporary ledger is removed when the demo exits. [General tool workflow](examples/tool_workflow.py)

The email demo runs locally without network access or credentials. It scans a syllabus request, releases one reply, and withholds two others. A fake mail sender then receives an aligned proposal. A second proposal would email a fictional roster even though the student only asked for a syllabus summary. The alignment check blocks it before the sender runs.

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

## Register an application tool

This example assumes the user submitted a structured form to save a note. The application keeps the accepted form fields separately from the proposal produced by the agent.

```python
from quackguard import (
    DemoScanner, ExpectedAction, Guard, Permission, Tool, ToolRequest,
)

notes = []
user_request = "Save the quiz date."
trusted_form = {"text": "The sample quiz is on Friday."}
expected = ToolRequest(user_request, "save_note", trusted_form)
guard = Guard(
    DemoScanner(),
    tools={"save_note": Tool(
        execute=notes.append,
        validate=lambda a: set(a) == {"text"} and isinstance(a["text"], str),
        permission=lambda a: Permission.ALLOW if len(a["text"]) <= 500 else Permission.DENY,
    )},
    alignment=ExpectedAction(expected),
)
proposal = ToolRequest(user_request, "save_note", {"text": "The sample quiz is on Friday."})
result = guard.run_tool(proposal)
print(result.decision, result.reason_code, result.execution)
```

Use `Tool(execute=..., validate=..., permission=...)` once per registered action. Each callback receives a fresh ordinary dictionary with the same argument values. Return exactly `True` or `False` from validation. Reject unknown keys and validate values according to the tool's own semantics. The guard snapshots nested objects and arrays on request construction. JSON object order is immaterial. Array order and the types of values are significant. Arguments may have up to 32 levels of nesting and 64 KiB of canonical ASCII-escaped JSON. The original user request can contain up to 65,536 characters.

To return a text value from the tool, register it with `return_text=True`. QuackGuard scans the returned string before setting `result.output_text`. Inspect `result.output_scan` for a flagged, unavailable, or malformed return. An allowed side effect may have a withheld output. With the default setting, the return value is discarded. To expose structured results, have your application tool serialize the public fields to text for scanning.

`ExpectedAction` compares one independently captured, structured user intent with a proposed tool and arguments. An edited action conflicts with that intent. An unfamiliar user request needs another source of intent. For open-ended agent workflows, supply an application-specific `AlignmentCheck.check(request) -> AlignmentResult`. For offline tests, `FixtureAlignment` matches whole requests. Build trusted intent from an authenticated interaction. Treat documents, tool responses and agent-generated text as data.

The existing `Email` and `EmailTool` API uses the same decision pipeline. [Email integration example](examples/offline_demo.py)

### Decisions and review

A `SAFE` scan clears only the supplied content in that direction. `UNSAFE` blocks release and `UNAVAILABLE` holds it for review. The caller releases a reply only on `ALLOW`.

| Tool check, in order | Result |
| --- | --- |
| Unregistered tool, argument schema mismatch, or denied permission | Block before alignment |
| Validator or permission callback unavailable | Hold until the check recovers |
| Clear conflict with the user request | Block, including when policy asks for approval |
| Required alignment check unavailable or malformed | Hold until the check recovers |
| Policy asks for approval or alignment is uncertain | Hold for the user's decision |
| Policy allows and alignment confirms the exact proposal | Reserve the operation if a ledger is configured, then call the tool |
| Previously reserved key or key reused for different arguments | Block at reservation |

A tool result includes its stage, registered tool name, permission, alignment, final decision, reason code, explanation, and execution outcome. An unregistered tool name is omitted from results and events. The output scan, when requested, is available separately.

For a held result, show `result.explanation` and `result.review_summary` to the authenticated requesting user. The summary contains the registered tool name and exact JSON-escaped fields. Use `guard.pending_reviews()` to retrieve current private review cards for this guard. Render it as plain text in a private approval UI. Collect consent through that UI and pass the original request back to the guard.

```python
from quackguard import UserDecision

# In a trusted approval handler, after the user explicitly chooses Allow:
result = guard.review(result.proposal_id, request, UserDecision.ALLOW)
```

Pass `UserDecision.DENY` to decline or `None` to keep waiting. Raw strings, model output, and text in a document cannot approve a call. The integrating application owns authentication and routing to the correct user's guard. A changed request, tool, or argument invalidates the pending proposal. Reviews expire after 300 seconds by default, configurable with `review_ttl`. The guard enforces the deadline at review and admission. Calls to `run_tool`, `pending_reviews`, and `stats` also reclaim expired idle entries. An unknown, blocked, or already consumed ID raises `ValueError`.

When a required check was unavailable, call `guard.recheck(id, request)` after recovery. A successful recheck returns a new review prompt. A fresh affirmative decision is required. The guard revalidates permission and alignment before execution. Tool exceptions produce `execution == Execution.FAILED` while retaining the policy decision. Inspect the outcome before taking further action. There is no automatic retry.

## Durable duplicate protection

A regular `run_tool` call starts a new proposal. For side effects that must survive process restarts, pass a provisioned `SQLiteLedger` to `Guard` and supply the same application-issued `idempotency_key` on every retry of the logical operation. Each worker must use the same database, secret and authenticated scope. A committed reservation prevents a repeated invocation even if the first process died before recording the outcome. [Provisioning, retry semantics and recovery](docs/operations.md)

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

The adapter sends one HTTPS POST with a bearer authorization header and a JSON body containing only `model` and `messages`. An input scan sends the supplied text as one user message. An output scan sends the fixed user message `Review the following reply.` followed by the supplied text as an assistant message. No conversation history, email proposals, or approval records are attached. Use a deployment that applies the Llama Guard chat template to the final message and returns a classification for that role. The configured timeout applies to connection and socket reads. Redirects are disabled. The adapter reads at most 64 KiB of classification data into its response buffer. An oversized response holds the item for review.

The supported response is HTTP 200 with one chat-completion choice. Its `message.content` must be a string containing lowercase `safe`, `unsafe`, or `unsafe` followed by a newline and comma-separated unique category codes S1 through S14. Surrounding whitespace is ignored. A supplied message role must be `assistant` and a supplied `finish_reason` must be `stop`. Extra prose, invalid categories, tool calls, truncated completions, transport failures, and missing credentials produce `UNAVAILABLE`. Provider responses and exceptions are excluded from QuackGuard events. See the [Llama Guard 3 model card](https://huggingface.co/meta-llama/Llama-Guard-3-8B).

## Coverage and activity

QuackGuard sees only the text you explicitly scan and the actions you route through `run_tool`. Calling the sender directly bypasses QuackGuard. The package provides no process isolation, network interception, or sandbox for agent code. Tool registration supports synchronous JSON-object callbacks and the existing single-recipient email adapter. The application controls the tools, their credentials, authentication and network access.

`guard.events` returns a snapshot of in-memory events with UTC timestamps, stage, decision, reason code, elapsed milliseconds, registered tool name and proposal ID where applicable. Decision events record permission and alignment. Execution events report the sender outcome separately. Raw inputs, replies, arguments, addresses, API keys, and provider bodies are excluded. Keep review previews out of your application logs. Pending requests and previews remain in process memory. By default each guard retains 1,000 recent events and up to 256 pending reviews. The optional ledger persists execution reservations and outcomes.

`guard.stats()` counts scans, outcomes, and each proposal's latest decision. Reviewing the same proposal moves it between totals. Counts cover the life of that guard, including events that have aged out of its buffer. Execution counts track actual tool attempts.
