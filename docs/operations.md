# Running QuackGuard in an application

## Deployment boundary

This version supports synchronous Python tools and one host with a durable local SQLite volume. Several worker processes on that host can use the same execution ledger. Pending reviews, scan results, counters and event buffers belong to one guard instance. Route reviews back to the original process and guard. For multiple hosts, choose a single execution service on the supported host or integrate a separately reviewed coordination system before distributing tool execution.

The application authenticates callers, defines resource ownership and tenant scope, and authorizes the human who approves a pending operation. Use one guard per authenticated principal or workflow. A proposal ID identifies an item inside a guard. The application must authorize access before displaying a review card or accepting a decision. Keep registration, approval handlers and original user intent outside the model's tool catalogue. A direct callable or a compromised application process bypasses this wrapper.

## Register a tool

1. Register an application-owned ASCII name, such as `create_ticket`, with `Tool`. The executor receives one dictionary of JSON fields. Wrap existing functions or SDK methods with a small adapter when their calling conventions differ.
2. Validate the complete argument schema, including allowed keys, lengths, resource identifiers and semantic constraints. Return a boolean. Do not coerce or modify values in the validator. Put any normalization into application code before constructing both the trusted intent and proposal.
3. Check application permissions using the authenticated principal and current resource state. Return `Permission.ALLOW`, `ASK` or `DENY`. Make the validator, permission and alignment checks side-effect free. They can run more than once. Perform any resource-level authorization that must be atomic with a write inside the tool's own transaction.
4. Establish intent independently of the proposal. `ExpectedAction` supports a command or form whose exact fields the user explicitly selected. An application-specific classifier may return `Alignment.UNCERTAIN` or `UNAVAILABLE` when intent cannot be established.
5. Make the executor synchronous, set downstream deadlines, and raise an exception for a failed operation. If a provider returns an error object, translate that error into an exception in the adapter. Tool execution may return `None`. Enable `return_text` only when a string should pass through output scanning. Use `output_text` as the release boundary.

A schema rejection produces `invalid_arguments` before any permission or alignment call. A broken validator produces `validator_unavailable`. The latter needs a successful recheck and a fresh user decision. Tool inputs should reference credentials through application configuration. Never place tokens in proposed arguments or the review UI.

## Provision the execution ledger

Create a private directory on a persistent local filesystem. Have your secret manager generate at least 32 random bytes once and inject their hex representation as `QUACKGUARD_LEDGER_SECRET`. Use the same secret for every scope in that database. The example also expects the path in `QUACKGUARD_LEDGER_PATH`.

```python
import os
from quackguard import SQLiteLedger

path = os.environ["QUACKGUARD_LEDGER_PATH"]
secret = bytes.fromhex(os.environ["QUACKGUARD_LEDGER_SECRET"])

# Provision once during setup. Existing files cause FileExistsError.
SQLiteLedger.create(path, secret=secret, scope="provisioning")
```

Open the existing file when a worker starts. Compute a stable scope from authenticated application identifiers. Build keys at the application's logical operation boundary, persist them there, and reuse them on retries. Parallel legitimate operations with identical arguments each need their own key.

```python
from quackguard import Guard, SQLiteLedger

ledger = SQLiteLedger(path, secret=secret, scope="notes-app/tenant-42/user-17")
guard = Guard(scanner, tools=registered_tools, alignment=alignment, ledger=ledger)
result = guard.run_tool(proposal, idempotency_key=operation_id)
```

Use separate keys for separate intended actions. Keep the same scope, key, trusted request and exact arguments for every retry of one action. With a ledger configured every proposal needs a key. Without one every submission is independent. Idempotency applies to execution attempts. Two submissions may create separate review cards or decision events. Only one can reserve the operation. Duplicate proposals return a block reason and do not replay the prior response body.

Each reservation commits before external code runs. The callback and the ledger use separate transactions. A crash can leave a reservation even when the external system performed no action. This provides at most one wrapper invocation under a stable scope and key. Downstream SDK retries, queues and a tool's own side effects have their own delivery semantics. Propagate the operation ID to downstream services through trusted application context when they support idempotency.

## Recover from an interruption

Query `ledger.status(operation_id)` under the original authenticated scope. The ledger stores keyed digests of the operation key and action identity, plus the state. It contains no prompts, tool arguments, raw operation keys or provider bodies.

| State or signal | Meaning and operator action |
| --- | --- |
| No row | No reservation exists in this scope. Resubmit the original proposal with the same key if the user still wants it. |
| `started` | A worker reserved the action. It may still be running, or may have stopped at any point around the side effect. Inspect the worker and downstream system before making a business decision. |
| `succeeded` | The tool returned successfully. Recover any needed result from the application's system of record. |
| `failed` | The tool raised an error. A partial external effect is possible. Reconcile it before arranging any follow-up. |
| `expired` | Approval expired while acquiring the reservation. The guard prevented execution and retained the key. Obtain a new explicit operation if the action is still needed. |
| `outcome_recorded is False` | The wrapper observed an outcome but could not persist it. The execution event uses `outcome_unrecorded`. Reconcile the downstream system. |
| `ledger_unavailable` | The gate could not establish a reservation. Execution is held. Restore the original database and secret, recheck, and collect a fresh decision. |
| `idempotency_conflict` | Another action used this key. Inspect the operation mapping in the application. |

Reserve `SQLiteLedger.create` for provisioning. Startup opens the existing file in read/write mode and checks the secret. Retain the ledger and secret for the full retry horizon. A new database, a changed scope, or restoring an old backup can discard duplicate protection for later operations. Stop workers, back up the database consistently and reconcile all external operations completed after the backup before resuming. This version has no automatic key pruning or secret rotation.

## Handle reviews and outputs

Read `guard.pending_reviews()` inside the authenticated review handler. The private preview includes all action fields as escaped text. Render it using your UI's plain-text escaping. Bind the submitted choice to that proposal ID, original request and user session. Only `UserDecision.ALLOW` approves a reviewed action. `recheck` reruns checks and keeps a recovered action pending for fresh consent.

A pending review ends after `review_ttl` seconds from its first hold (default 300). Reading the queue or stats, or submitting another proposal, reclaims idle expired entries. In-flight checks can finish before their final status is visible. A restart discards pending cards. Reconstruct the proposal from trusted application state and start a new review using its original operation key. A prior approval is never restored.

| Value | Integration behavior |
| --- | --- |
| `result.decision` | Dispatch has already been handled by the wrapper. Never call the tool a second time in an ALLOW branch. |
| `result.execution` | Distinguishes an uncalled tool, successful return and exception. |
| `result.output_scan` | A separate content decision for a requested text result. Withhold the result on BLOCK or REVIEW. |
| `result.output_text` | Contains a string only after a successful tool return and a SAFE output scan. |
| `result.outcome_recorded` | Reports whether a completed attempt's outcome was persisted. It is `None` for in-memory execution or an uncalled tool. |

## Monitoring and acceptance

Set `max_events` and `max_pending` to suit the process memory budget. A full review queue blocks new holds with `review_queue_full`. Older events leave the ring buffer in insertion order. `stats()` retains per-guard lifetime counts. Monitor unavailable checks, queue saturation, execution failures and unrecorded outcomes. Keep private previews and request objects out of telemetry. Aggregate counts across workers in your application if needed.

Before enabling a real tool, run the automated checks with synthetic data and exercise its integration in staging. Test your identity mapping, schema, resource permissions, approval UI, downstream timeouts and reconciliation procedure. For a remote scanner, also validate role-specific classifications, timeout behavior, model category compatibility, provider privacy settings and latency against the actual deployment. The offline fixtures exercise control flow. Detection accuracy and service capacity require measurements on your workload.

```sh
python -m pip install -e '.[dev]'
python -m pytest
printf 'allow\n' | python examples/tool_workflow.py
printf 'allow\ndeny\n' | python examples/offline_demo.py
```
