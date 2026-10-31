"""Claim table: the one place that decides whether a side effect may run.

A key moves CLAIMED -> DONE (result stored) or CLAIMED -> FAILED. Nothing moves a key back to "free" except an
explicit release that states what was checked. A key left CLAIMED (crash mid-call) or FAILED is INDETERMINATE: the
side effect may or may not have happened, so the gate refuses to run it again on its own.

A release needs an outcome and evidence (see Store.release). "did-not-happen" moves the claim row into the releases
table, so the key is free again but the history is kept; "happened" turns the key DONE with a reconciled result, so
a retry replays instead of running. Every release, of either kind, is kept in the releases table.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

CLAIMED, DONE, FAILED = "CLAIMED", "DONE", "FAILED"
HAPPENED, DID_NOT_HAPPEN = "happened", "did-not-happen"
OUTCOMES = (DID_NOT_HAPPEN, HAPPENED)
MIN_EVIDENCE_CHARS = 15

SCHEMA = """
CREATE TABLE IF NOT EXISTS claims (
    key TEXT PRIMARY KEY,
    tool TEXT NOT NULL,
    state TEXT NOT NULL,
    result TEXT,
    error TEXT,
    claimed_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_hash TEXT NOT NULL,
    tool TEXT NOT NULL,
    args_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    used_at REAL
);
CREATE INDEX IF NOT EXISTS approvals_hash ON approvals (action_hash);
CREATE TABLE IF NOT EXISTS pending (
    action_hash TEXT PRIMARY KEY,
    tool TEXT NOT NULL,
    args_json TEXT NOT NULL,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    attempts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS releases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    tool TEXT NOT NULL,
    action_hash TEXT,
    prior_state TEXT NOT NULL,
    prior_error TEXT,
    claimed_at REAL NOT NULL,
    outcome TEXT NOT NULL,
    evidence TEXT NOT NULL,
    released_by TEXT,
    released_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS releases_key ON releases (key);
"""


class EvidenceRequired(ValueError):
    """A release was asked for without an outcome or without enough evidence. Nothing was changed."""


def check_release(outcome, evidence) -> str:
    """The evidence guard. Returns the cleaned evidence text, or raises EvidenceRequired. Releasing an
    indeterminate action without saying what was checked would just be a retry."""
    if outcome not in OUTCOMES:
        raise EvidenceRequired(f"outcome must be one of {', '.join(OUTCOMES)}, got {outcome!r}")
    text = evidence.strip() if isinstance(evidence, str) else ""
    if len(text) < MIN_EVIDENCE_CHARS:
        raise EvidenceRequired(f"evidence must say what was checked, in at least {MIN_EVIDENCE_CHARS} characters "
                               f"(got {len(text)})")
    return text


def reconciled_result(evidence: str, by: str | None, at: float) -> dict:
    """The stored result for a key reconciled as "happened": what a retry replays instead of running again. It
    carries MCP-style text content so a proxy client gets an ordinary tool result."""
    return {"reconciled": True, "outcome": HAPPENED, "evidence": evidence, "by": by, "at": at,
            "content": [{"type": "text", "text": "Already done (reconciled, not re-run). Evidence: " + evidence}]}


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=30)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Older databases have no claims.action_hash column; add it (another process may do so at the same time)."""
        cols = [r[1] for r in self._db.execute("PRAGMA table_info(claims)").fetchall()]
        if "action_hash" not in cols:
            try:
                self._db.execute("ALTER TABLE claims ADD COLUMN action_hash TEXT")
            except sqlite3.OperationalError:
                cols = [r[1] for r in self._db.execute("PRAGMA table_info(claims)").fetchall()]
                if "action_hash" not in cols:
                    raise

    def claim(self, key: str, tool: str, action_hash: str | None = None) -> tuple[bool, dict | None]:
        """Try to claim key. Returns (won, existing_row). Exactly one caller can win a key, across threads and
        processes, because the INSERT on the primary key is the claim."""
        now = time.time()
        with self._lock:
            try:
                self._db.execute("INSERT INTO claims (key, tool, state, claimed_at, updated_at, action_hash) "
                                 "VALUES (?,?,?,?,?,?)", (key, tool, CLAIMED, now, now, action_hash))
                return True, None
            except sqlite3.IntegrityError:
                return False, self.get(key)

    def complete(self, key: str, result) -> None:
        with self._lock:
            cur = self._db.execute("UPDATE claims SET state=?, result=?, updated_at=? WHERE key=? AND state=?",
                                   (DONE, json.dumps(result), time.time(), key, CLAIMED))
            if cur.rowcount != 1:
                raise RuntimeError(f"complete() on a key that is not CLAIMED: {key}")

    def fail(self, key: str, error: str) -> None:
        with self._lock:
            self._db.execute("UPDATE claims SET state=?, error=?, updated_at=? WHERE key=? AND state=?",
                             (FAILED, error[:2000], time.time(), key, CLAIMED))

    def get(self, key: str) -> dict | None:
        row = self._db.execute("SELECT key, tool, state, result, error, claimed_at, updated_at, action_hash "
                               "FROM claims WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        k, tool, state, result, error, c, u, h = row
        return {"key": k, "tool": tool, "state": state, "result": json.loads(result) if result else None,
                "error": error, "claimed_at": c, "updated_at": u, "action_hash": h}

    def release(self, key: str, outcome: str, evidence: str, by: str | None = None,
                expect_claimed_at: float | None = None) -> dict | None:
        """Resolve a CLAIMED/FAILED (indeterminate) key after someone checked the real world.

        outcome "did-not-happen": the claim row moves to the releases table, so the key may be claimed (and run)
        again, and the history is kept. outcome "happened": the key becomes DONE with a reconciled result, so a
        retry replays it and nothing runs again. Either way a releases row records outcome, evidence, by and when.

        Raises EvidenceRequired, changing nothing, without a valid outcome and at least MIN_EVIDENCE_CHARS of
        evidence. Returns the releases row, or None if the key is unknown, DONE, or (with expect_claimed_at) no
        longer the claim the caller looked at. It is all one transaction."""
        evidence = check_release(outcome, evidence)  # EVIDENCE GUARD
        now = time.time()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self.get(key)
                if row is None or row["state"] not in (CLAIMED, FAILED) or (
                        expect_claimed_at is not None and row["claimed_at"] != expect_claimed_at):
                    self._db.execute("ROLLBACK")
                    return None
                cur = self._db.execute(
                    "INSERT INTO releases (key, tool, action_hash, prior_state, prior_error, claimed_at, outcome, "
                    "evidence, released_by, released_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (key, row["tool"], row["action_hash"], row["state"], row["error"], row["claimed_at"], outcome,
                     evidence, by, now))
                release_id = cur.lastrowid
                if outcome == DID_NOT_HAPPEN:
                    self._db.execute("DELETE FROM claims WHERE key=?", (key,))
                else:
                    self._db.execute("UPDATE claims SET state=?, result=?, error=NULL, updated_at=? WHERE key=?",
                                     (DONE, json.dumps(reconciled_result(evidence, by, now)), now, key))
                self._db.execute("COMMIT")
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise
        return self.releases(release_id)[0]

    def releases(self, release_id: int | None = None) -> list[dict]:
        """Release history, oldest first (or just the row with this id)."""
        cols = ("id", "key", "tool", "action_hash", "prior_state", "prior_error", "claimed_at", "outcome", "evidence",
                "released_by", "released_at")
        sql = f"SELECT {', '.join(cols)} FROM releases"
        rows = (self._db.execute(sql + " WHERE id=?", (release_id,)) if release_id is not None
                else self._db.execute(sql + " ORDER BY id")).fetchall()
        return [dict(zip(cols, r)) for r in rows]

    def unclaim(self, key: str) -> bool:
        """Drop a CLAIMED key that the caller KNOWS was never forwarded (for example a last-second STOP)."""
        with self._lock:
            cur = self._db.execute("DELETE FROM claims WHERE key=? AND state=?", (key, CLAIMED))
            return cur.rowcount == 1

    # --- approvals: written only by the human CLI, consumed (single use) by the gate ---

    def add_approval(self, action_hash: str, tool: str, args_json: str, expires_at: float) -> int:
        now = time.time()
        with self._lock:
            cur = self._db.execute("INSERT INTO approvals (action_hash, tool, args_json, created_at, expires_at) "
                                   "VALUES (?,?,?,?,?)", (action_hash, tool, args_json, now, expires_at))
            self._db.execute("DELETE FROM pending WHERE action_hash=?", (action_hash,))
            return cur.lastrowid

    def find_approval(self, action_hash: str, now: float | None = None) -> int | None:
        """Id of an unused, unexpired approval for exactly this action hash, or None. Does not consume it."""
        now = time.time() if now is None else now
        row = self._db.execute("SELECT id FROM approvals WHERE action_hash=? AND used_at IS NULL AND expires_at > ? "
                               "ORDER BY created_at LIMIT 1", (action_hash, now)).fetchone()
        return row[0] if row else None

    def consume_approval(self, approval_id: int, action_hash: str, now: float | None = None) -> bool:
        """Mark one approval used. A single UPDATE guarded on used_at IS NULL, so two callers cannot both use it."""
        now = time.time() if now is None else now
        with self._lock:
            cur = self._db.execute("UPDATE approvals SET used_at=? WHERE id=? AND action_hash=? AND used_at IS NULL "
                                   "AND expires_at > ?", (now, approval_id, action_hash, now))
            return cur.rowcount == 1

    def unconsume_approval(self, approval_id: int) -> None:
        """Give an approval back when the action it was spent on was provably never forwarded."""
        with self._lock:
            self._db.execute("UPDATE approvals SET used_at=NULL WHERE id=?", (approval_id,))

    def approvals(self) -> list[dict]:
        cols = ("id", "action_hash", "tool", "args_json", "created_at", "expires_at", "used_at")
        rows = self._db.execute(f"SELECT {', '.join(cols)} FROM approvals ORDER BY id").fetchall()
        return [dict(zip(cols, r)) for r in rows]

    def note_pending(self, action_hash: str, tool: str, args_json: str) -> None:
        now = time.time()
        with self._lock:
            self._db.execute("INSERT INTO pending (action_hash, tool, args_json, first_seen, last_seen, attempts) "
                             "VALUES (?,?,?,?,?,1) ON CONFLICT(action_hash) DO UPDATE SET last_seen=excluded.last_seen,"
                             " attempts=attempts+1", (action_hash, tool, args_json, now, now))

    def pending(self) -> list[dict]:
        cols = ("action_hash", "tool", "args_json", "first_seen", "last_seen", "attempts")
        rows = self._db.execute(f"SELECT {', '.join(cols)} FROM pending ORDER BY first_seen").fetchall()
        return [dict(zip(cols, r)) for r in rows]

    def all(self) -> list[dict]:
        keys = [r[0] for r in self._db.execute("SELECT key FROM claims ORDER BY claimed_at").fetchall()]
        return [self.get(k) for k in keys]

    def close(self) -> None:
        self._db.close()
