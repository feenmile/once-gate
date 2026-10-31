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
)
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=30)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(SCHEMA)

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

    def all(self) -> list[dict]:
        keys = [r[0] for r in self._db.execute("SELECT key FROM claims ORDER BY claimed_at").fetchall()]
        return [self.get(k) for k in keys]

    def close(self) -> None:
        self._db.close()
