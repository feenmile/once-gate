"""Claim table: the one place that decides whether a side effect may run.

A key moves CLAIMED -> DONE (result stored) or CLAIMED -> FAILED. Nothing moves a key back to "free" except an
explicit human release. A key left CLAIMED (crash mid-call) or FAILED is INDETERMINATE: the side effect may or may
not have happened, so the gate refuses to run it again on its own.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

CLAIMED, DONE, FAILED = "CLAIMED", "DONE", "FAILED"

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
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=30)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)

    def claim(self, key: str, tool: str) -> tuple[bool, dict | None]:
        """Try to claim key. Returns (won, existing_row). Exactly one caller can win a key, across threads and
        processes, because the INSERT on the primary key is the claim."""
        now = time.time()
        with self._lock:
            try:
                self._db.execute("INSERT INTO claims (key, tool, state, claimed_at, updated_at) VALUES (?,?,?,?,?)",
                                 (key, tool, CLAIMED, now, now))
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
        row = self._db.execute("SELECT key, tool, state, result, error, claimed_at, updated_at FROM claims WHERE key=?",
                               (key,)).fetchone()
        if not row:
            return None
        k, tool, state, result, error, c, u = row
        return {"key": k, "tool": tool, "state": state, "result": json.loads(result) if result else None,
                "error": error, "claimed_at": c, "updated_at": u}

    def release(self, key: str) -> bool:
        """Human-only escape hatch: forget a CLAIMED/FAILED key after checking the real world by hand."""
        with self._lock:
            cur = self._db.execute("DELETE FROM claims WHERE key=? AND state IN (?,?)", (key, CLAIMED, FAILED))
            return cur.rowcount == 1

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
