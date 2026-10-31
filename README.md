# once-gate

**Make your AI agent's side effects happen at most once, and only the exact call a person approved.**

Agents retry. Frameworks replay. Networks drop the answer after the action already happened. The result is the
email sent twice, the card charged twice, the order placed twice. once-gate sits between your agent and its tools
and makes each real-world action run **at most once**.

```
agent  ──►  once-gate  ──►  your MCP server (send_email, charge_card, create_order …)
             │
             └─ claim table (SQLite) + append-only log
```

## What it does

For each gated tool call:

| Situation | What once-gate does |
|---|---|
| First time this action is seen | Claims it, forwards it, stores the result |
| Same action again (same tool + same arguments, or same `idempotency_key`) | Returns the stored result. **The server is not called.** |
| Same action arrives while the first is still running | Waits and gives it the first call's answer |
| Action started but never finished (crash, timeout, server error) | Refuses with **INDETERMINATE**. The action may or may not have happened, so a human checks and then runs `oncegate release KEY` |

The last row is the point. Most retry logic assumes a failed call didn't happen. For side effects, that's how you
charge someone twice. once-gate never re-runs an unfinished action on its own.

## New in 0.2.0

- **Exact-content human approval** for the tools you choose (`--require-approval`).
- **Relay rejection**: nothing inside the MCP message stream can approve anything.
- **Last-second STOP file** (`--stop-file`), checked immediately before a gated call is forwarded, fail-closed.
- **Receipts**: every ledger record carries `decision`, `reason`, `action_hash`, `tool` and `at`, so refusals are
  recorded too.

## Use it as an MCP proxy

```bash
python -m oncegate proxy --db claims.db --ledger actions.jsonl --tools send_email,charge_card -- <your MCP server command>
```

Point your agent's MCP config at that command instead of the server's. Only the tools you list are gated (omit
`--tools` to gate everything). Every message passes through unchanged except gated `tools/call` requests.

Pass an `idempotency_key` argument to define "the same action" yourself (for example an order number). Otherwise
the key is the tool name plus the canonical JSON of the arguments.

## Exact-content human approval

```bash
python -m oncegate proxy --db claims.db --ledger actions.jsonl \
    --require-approval send_email,charge_card --stop-file ./STOP -- <your MCP server command>
```

For a tool in `--require-approval`, a call is forwarded only if a person approved **that exact call**: the
approval is bound to the *action hash*, sha256 over the tool name and the canonical JSON of the full arguments
(sorted keys, no whitespace; the same canonicalization as the claim key). Change one character of any argument and
the hash changes, so it needs a new approval. Approvals are:

- **single use**: spent when the call runs;
- **time-limited**: 24 hours by default (`--ttl-hours`);
- **created only by the CLI**, never by the proxy.

Without a valid approval the proxy does not forward the call. It answers with a JSON-RPC error (code `-32011`)
whose `data` holds `code: "APPROVAL_REQUIRED"`, the `action_hash`, the tool, the exact arguments and a one-line
summary, and it records the call as pending. Tools in `--require-approval` are also gated at-most-once, even if
they are not in `--tools`. A repeat of an action that already completed is answered from the stored result and
needs no new approval, because nothing runs.

A person approves:

```bash
python -m oncegate approve --db claims.db --pending            # list blocked calls, pick one, review, confirm
python -m oncegate approve --db claims.db --tool send_email --args-json '{"to": "a@example.com", "body": "hi"}'
python -m oncegate list --db claims.db --approvals             # approvals and whether they were used
```

`approve` prints the full exact payload and the action hash, then asks the person to type the first 8 characters
of the hash. With no interactive terminal (stdin and stdout must both be a terminal; on Windows, a real console)
it refuses, unless `--i-am-a-human` is given (with `--pending`, pick the call with `--hash`).

**This is a speed bump, not a cryptographic guarantee.** It stops an agent from approving its own calls through
the tool channel, and makes a careless approval harder. It does not stop a process that can run `oncegate approve
--i-am-a-human`, or write to the SQLite file, from creating an approval. If that matters to you, run the agent so
it can do neither (separate OS user, no shell on the gate's machine, database outside its reach).

### Relay rejection

Approvals never travel through the MCP message stream. Any field in a `tools/call` request whose name contains
`approv` (for example `approval`, `approved`, `approval_token`, `human_approved`), at any depth in the arguments or
elsewhere in `params` such as `_meta`, grants nothing. It is logged as `relayed_approval_ignored` with the field
paths. Because such a field is part of the arguments, it is also part of the action hash, so a person sees it
when approving.

## STOP file

With `--stop-file PATH`, the proxy checks the path immediately before forwarding each gated call (after the claim
and approval checks, as the last step). If the file exists it answers `STOPPED` (code `-32012`) and does not
forward. The check is **fail-closed**: if it cannot be completed (permission denied, invalid path, I/O error) the
call is treated as stopped. Because a stopped call was provably never forwarded, its claim and its approval are
rolled back; once the file is removed the same call can run. Ungated tools are not affected.

## Use it as a library

```python
from oncegate import ApprovalRequired, Gate, Indeterminate, Stopped

gate = Gate("claims.db", "actions.jsonl", require_approval={"charge_card"}, stop_file="STOP")
receipt = gate.run("charge_card", lambda: stripe_charge(order), {"order": order.id}, key=f"order-{order.id}")
```

`run` raises `ApprovalRequired`, `Stopped` or `Indeterminate` without calling the function. Approvals for library
use are created with the same `oncegate approve` command against the same database.

## Receipts (the ledger)

`--ledger` is an append-only JSONL file. Every record has:

| Field | Meaning |
|---|---|
| `event` | `claimed`, `done`, `failed`, `replayed`, `refused_indeterminate`, `approval_required`, `stopped`, `relayed_approval_ignored` |
| `decision` | `allowed`, `replayed` or `refused`; `null` on outcome records (`done`, `failed`) and informational ones |
| `reason` | for example `first_run`, `approved`, `already_done`, `joined_inflight`, `indeterminate`, `approval_required`, `stop_file_present`, `stop_check_error: ...` |
| `action_hash` | the exact-content hash described above |
| `tool` | tool name |
| `at` | RFC 3339 UTC timestamp |

**Relation to the ACTA draft.** The IETF Internet-Draft *Signed Decision Receipts for Machine-to-Machine Access
Control* ([draft-farley-acta-signed-receipts-03](https://www.ietf.org/archive/id/draft-farley-acta-signed-receipts-03.html);
an individual submission, not an IETF standard) defines receipt fields including `decision`, `reason` and
`tool_name` (section 3.1.1), and `issued_at` and `action_ref` (section 2.2). once-gate uses the same names for
`decision` and `reason`, and writes `tool_name` and `issued_at` alongside `tool` and `at` with the same values.
Not aligned: once-gate's decision values (`allowed`/`replayed`/`refused`) differ from the draft's
(`allow`/`deny`/`rate_limit`/`require_approval`); `action_hash` is not called `action_ref` because the draft
defines a different hash preimage; and once-gate records are **not signed** and lack the draft's envelope and
issuer fields. The ledger is a plain local log, not an ACTA receipt.

## Inspect and recover

```bash
python -m oncegate list --db claims.db                 # every claim and its state
python -m oncegate list --db claims.db --pending       # calls blocked waiting for approval
python -m oncegate release --db claims.db "charge_card:order-17"   # after checking by hand that it did NOT happen
```

`DONE` actions can never be released.

## Guarantees and limits

- **Exactly one winner per key.** The claim is a primary-key insert in SQLite (tested across 8 concurrent
  processes).
- **At most once, not exactly once.** If the server did the work but the answer was lost, once-gate says
  INDETERMINATE instead of guessing. That is deliberate.
- **Tool-level only.** It can't see side effects a tool performs outside the call.
- **Single machine.** One SQLite file per deployment; it is not a distributed lock.
- **Approval is a speed bump, not cryptography.** Approvals are not signed and are only as safe as access to the
  database file and the `oncegate` command.
- **Approval matches bytes, not meaning.** Two calls that mean the same thing but differ in any argument need
  separate approvals, and an approval says nothing about whether the call is wise.
- **The ledger is not tamper-evident.** It is append-only by convention, not hash-chained or signed.
- **STOP is checked per gated call**, before forwarding. It does not interrupt a call already running and does not
  apply to ungated tools.

Version 0.2.0. Built by Feenmile.

## License

MIT
