"""At-most-once gate for side effects, usable as a library.

Order of checks for one call (see Gate.admit):
  1. claim table: already DONE -> replay the stored result (nothing runs, no approval needed);
     CLAIMED/FAILED -> refuse as INDETERMINATE;
  2. approval (only for tools in require_approval): an unused, unexpired human approval for the EXACT action hash;
  3. claim the key, spend the approval;
  4. STOP file, checked last, immediately before the caller forwards/runs the action. If it trips, the claim and
     the approval are rolled back, because the action provably never left.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from .store import CLAIMED, DONE, FAILED, Store

# Any argument (or envelope field) whose name contains this is treated as an attempt to carry an approval in-band.
RELAY_MARKER = "approv"


class Indeterminate(Exception):
    """The action was started before and never finished cleanly. It may or may not have happened. A human must
    check the real world and then `oncegate release KEY` (or leave it)."""

    def __init__(self, key: str, row: dict):
        super().__init__(f"INDETERMINATE: {key} is {row['state']} since {row['claimed_at']:.0f}; check by hand, "
                         f"then release it if the action did not happen")
        self.key, self.row = key, row


class ApprovalRequired(Exception):
    """This exact action needs a human approval that does not exist (or is used up, or expired)."""

    def __init__(self, tool: str, arguments: dict, action_hash: str):
        self.tool, self.arguments, self.action_hash = tool, arguments, action_hash
        self.summary = summarize(tool, arguments)
        super().__init__(f"APPROVAL_REQUIRED: {self.summary} [action {action_hash}]. A human must run "
                         f"`oncegate approve --pending` on the machine that runs the gate.")


class Stopped(Exception):
    """The STOP file exists, or checking it failed (fail-closed)."""

    def __init__(self, tool: str, action_hash: str, reason: str):
        self.tool, self.action_hash, self.reason = tool, action_hash, reason
        super().__init__(f"STOPPED: {tool} was not run ({reason})")


def canonical_json(arguments) -> str:
    return json.dumps(arguments if arguments is not None else {}, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def key_for(tool: str, arguments: dict | None, explicit: str | None = None) -> str:
    """Explicit idempotency key wins. Otherwise the key is the tool name plus canonical JSON of the arguments, so
    the same call with the same arguments is the same action."""
    if explicit:
        return f"{tool}:{explicit}"
    return f"{tool}:{hashlib.sha256(canonical_json(arguments).encode('utf-8')).hexdigest()[:32]}"


def action_hash(tool: str, arguments: dict | None) -> str:
    """sha256 over the tool name and the canonical JSON of the full arguments (same canonicalization as key_for).
    Any change to any argument, even one character, gives a different hash."""
    return hashlib.sha256((tool + "\n" + canonical_json(arguments)).encode("utf-8")).hexdigest()


def summarize(tool: str, arguments: dict | None) -> str:
    return f"{tool} {canonical_json(arguments)}"


def find_relayed_approvals(obj, path: str = "") -> list[str]:
    """Paths of every key, at any depth, that looks like an approval. They grant nothing; they are only logged."""
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else str(k)
            if RELAY_MARKER in str(k).lower():
                found.append(p)
            found.extend(find_relayed_approvals(v, p))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            found.extend(find_relayed_approvals(v, f"{path}[{i}]"))
    return found


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Ledger:
    """Append-only JSONL record of every gate decision, including refusals.

    Every record carries: event, decision ("allowed" | "replayed" | "refused", or null for outcome/informational
    records), reason, action_hash, tool, at (RFC 3339 UTC). tool_name and issued_at repeat tool and at under the
    names used by the ACTA signed-receipts Internet-Draft; see README for what is and is not aligned."""

    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path else None

    def write(self, event: str, decision: str | None = None, reason: str | None = None,
              action_hash: str | None = None, tool: str | None = None, **extra) -> None:
        if not self.path:
            return
        at = _iso(time.time())
        rec = {"event": event, "decision": decision, "reason": reason, "action_hash": action_hash, "tool": tool,
               "at": at, "tool_name": tool, "issued_at": at}
        rec.update(extra)
        with open(self.path, "ab") as f:
            f.write((json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8"))


class Gate:
    def __init__(self, db: str | Path, ledger: str | Path | None = None, require_approval=None,
                 stop_file: str | Path | None = None):
        self.store = Store(db)
        self.ledger = Ledger(ledger)
        self.require_approval = set(require_approval or ())
        self.stop_file = str(stop_file) if stop_file else None

    def needs_approval(self, tool: str) -> bool:
        return tool in self.require_approval

    def stop_reason(self) -> str | None:
        """None if clear to act. Fail-closed: a configured STOP path that cannot be checked counts as stopped."""
        if not self.stop_file:
            return None
        try:
            os.stat(self.stop_file)
        except FileNotFoundError:
            return None
        except Exception as exc:  # permission denied, invalid path, I/O error...: we cannot prove it is clear
            return f"stop_check_error: {type(exc).__name__}: {exc}"
        return "stop_file_present"

    def _check_approval(self, h: str) -> int | None:
        """The approval guard: id of an unused, unexpired approval for exactly this action hash, else None."""
        return self.store.find_approval(h)

    def decide(self, key: str, tool: str, h: str | None = None):
        """Claim-table decision only (no approval, no STOP). Returns ("run", None) or ("replay", result); raises
        Indeterminate. Kept for 0.1 callers; new code should use admit()."""
        won, row = self.store.claim(key, tool)
        if won:
            self.ledger.write("claimed", "allowed", "first_run", h, tool, key=key)
            return "run", None
        return self._existing(key, tool, h, row)

    def _existing(self, key, tool, h, row):
        if row["state"] == DONE:
            self.ledger.write("replayed", "replayed", "already_done", h, tool, key=key)
            return "replay", row["result"]
        self.ledger.write("refused_indeterminate", "refused", "indeterminate", h, tool, key=key, state=row["state"])
        raise Indeterminate(key, row)

    def admit(self, tool: str, arguments: dict | None = None, key: str | None = None, envelope: dict | None = None):
        """Decide one call. Returns ("replay", result, None) or ("run", None, ticket). On "run" the caller must
        act immediately and then call done()/failed() with the ticket. Raises Indeterminate, ApprovalRequired or
        Stopped, in which case nothing may run."""
        arguments = arguments if arguments is not None else {}
        k = key_for(tool, arguments, key)
        h = action_hash(tool, arguments)
        relayed = find_relayed_approvals(arguments, "arguments") + find_relayed_approvals(envelope or {})
        if relayed:
            # Approvals never travel in the message stream. Whatever these fields say, they grant nothing.
            self.ledger.write("relayed_approval_ignored", None, "relayed_approval_ignored", h, tool, key=k,
                              fields=relayed)

        row = self.store.get(k)
        if row is not None:
            action, result = self._existing(k, tool, h, row)
            return action, result, None

        approval_id = None
        if self.needs_approval(tool):
            approval_id = self._check_approval(h)
            if approval_id is None:  # APPROVAL GUARD
                self.store.note_pending(h, tool, canonical_json(arguments))
                self.ledger.write("approval_required", "refused", "approval_required", h, tool, key=k)
                raise ApprovalRequired(tool, arguments, h)

        won, row = self.store.claim(k, tool)
        if not won:
            action, result = self._existing(k, tool, h, row)
            return action, result, None
        if approval_id is not None and not self.store.consume_approval(approval_id, h):
            self.store.unclaim(k)  # lost a race for the approval, or it expired this instant
            self.ledger.write("approval_required", "refused", "approval_used_or_expired", h, tool, key=k)
            raise ApprovalRequired(tool, arguments, h)

        stop = self.stop_reason()  # last check before acting
        if stop:
            self.store.unclaim(k)
            if approval_id is not None:
                self.store.unconsume_approval(approval_id)
            self.ledger.write("stopped", "refused", stop, h, tool, key=k)
            raise Stopped(tool, h, stop)

        self.ledger.write("claimed", "allowed", "approved" if approval_id is not None else "first_run", h, tool,
                          key=k, **({"approval_id": approval_id} if approval_id is not None else {}))
        return "run", None, {"key": k, "tool": tool, "action_hash": h}

    def done(self, key: str, tool: str, result, action_hash: str | None = None) -> None:
        self.store.complete(key, result)
        self.ledger.write("done", None, "completed", action_hash, tool, key=key)

    def failed(self, key: str, tool: str, error: str, action_hash: str | None = None) -> None:
        self.store.fail(key, error)
        self.ledger.write("failed", None, "error", action_hash, tool, key=key, error=error[:300])

    def run(self, tool: str, fn, arguments: dict | None = None, key: str | None = None):
        """Run fn() at most once for this (tool, arguments) or explicit key; replay the stored result after.
        Tools in require_approval also need an exact, unused, unexpired human approval; the STOP file is checked
        immediately before fn() runs."""
        action, result, t = self.admit(tool, arguments, key)
        if action == "replay":
            return result
        try:
            result = fn()
        except Exception as exc:
            self.failed(t["key"], tool, f"{type(exc).__name__}: {exc}", t["action_hash"])
            raise
        self.done(t["key"], tool, result, t["action_hash"])
        return result


__all__ = ["Gate", "Indeterminate", "ApprovalRequired", "Stopped", "key_for", "action_hash", "canonical_json",
           "summarize", "find_relayed_approvals", "CLAIMED", "DONE", "FAILED"]
