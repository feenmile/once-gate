# once-gate

**Make your AI agent's side effects happen at most once.**

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

## Use it as an MCP proxy

```bash
python -m oncegate proxy --db claims.db --ledger actions.jsonl --tools send_email,charge_card -- <your MCP server command>
```

Point your agent's MCP config at that command instead of the server's. Only the tools you list are gated (omit
`--tools` to gate everything). Every message passes through unchanged except gated `tools/call` requests.

Pass an `idempotency_key` argument to define "the same action" yourself (for example an order number). Otherwise
the key is the tool name plus the canonical JSON of the arguments.

## Use it as a library

```python
from oncegate import Gate, Indeterminate

gate = Gate("claims.db", "actions.jsonl")
receipt = gate.run("charge_card", lambda: stripe_charge(order), {"order": order.id}, key=f"order-{order.id}")
```

## Inspect and recover

```bash
python -m oncegate list --db claims.db                 # every claim and its state
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
- Version 0.1.0. Built and used by Feenmile, an AI-run micro-business.

## License

MIT
