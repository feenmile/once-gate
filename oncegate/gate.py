"""Exactly-once gate for side effects, usable as a library."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from .store import CLAIMED, DONE, FAILED, Store


class Indeterminate(Exception):
    """The action was started before and never finished cleanly. It may or may not have happened. A human must
    check the real world and then `oncegate release KEY` (or leave it)."""

    def __init__(self, key: str, row: dict):
        super().__init__(f"INDETERMINATE: {key} is {row['state']} since {row['claimed_at']:.0f}; check by hand, "
                         f"then release it if the action did not happen")
        self.key, self.row = key, row


def key_for(tool: str, arguments: dict | None, explicit: str | None = None) -> str:
    """Explicit idempotency key wins. Otherwise the key is the tool name plus canonical JSON of the arguments, so
    the same call with the same arguments is the same action."""
    if explicit:
        return f"{tool}:{explicit}"
    canon = json.dumps(arguments or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"{tool}:{hashlib.sha256(canon.encode('utf-8')).hexdigest()[:32]}"


class Ledger:
    """Append-only JSONL record of every gate decision."""

    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path else None

    def write(self, **event) -> None:
        if not self.path:
            return
        event["at"] = time.time()
        with open(self.path, "ab") as f:
            f.write((json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8"))


class Gate:
    def __init__(self, db: str | Path, ledger: str | Path | None = None):
        self.store = Store(db)
        self.ledger = Ledger(ledger)

    def decide(self, key: str, tool: str):
        """Returns ("run", None) if this caller won the claim, ("replay", result) if it already completed.
        Raises Indeterminate if it was started and never cleanly finished."""
        won, row = self.store.claim(key, tool)
        if won:
            self.ledger.write(event="claimed", key=key, tool=tool)
            return "run", None
        if row["state"] == DONE:
            self.ledger.write(event="replayed", key=key, tool=tool)
            return "replay", row["result"]
        self.ledger.write(event="refused_indeterminate", key=key, tool=tool, state=row["state"])
        raise Indeterminate(key, row)

    def done(self, key: str, tool: str, result) -> None:
        self.store.complete(key, result)
        self.ledger.write(event="done", key=key, tool=tool)

    def failed(self, key: str, tool: str, error: str) -> None:
        self.store.fail(key, error)
        self.ledger.write(event="failed", key=key, tool=tool, error=error[:300])

    def run(self, tool: str, fn, arguments: dict | None = None, key: str | None = None):
        """Run fn() at most once for this (tool, arguments) or explicit key; replay the stored result after."""
        k = key_for(tool, arguments, key)
        action, result = self.decide(k, tool)
        if action == "replay":
            return result
        try:
            result = fn()
        except Exception as exc:
            self.failed(k, tool, f"{type(exc).__name__}: {exc}")
            raise
        self.done(k, tool, result)
        return result


__all__ = ["Gate", "Indeterminate", "key_for", "CLAIMED", "DONE", "FAILED"]
