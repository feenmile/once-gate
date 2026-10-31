"""oncegate CLI.

  oncegate proxy --db claims.db [--ledger log.jsonl] [--tools send_email,charge_card] -- <mcp server command...>
  oncegate list --db claims.db
  oncegate release --db claims.db KEY
"""
from __future__ import annotations

import argparse
import json
import sys

from .gate import Gate
from .proxy import Proxy


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    server_cmd = []
    if "--" in argv:
        i = argv.index("--")
        argv, server_cmd = argv[:i], argv[i + 1:]
    p = argparse.ArgumentParser(prog="oncegate", description="Exactly-once gate for agent side effects.")
    sub = p.add_subparsers(dest="cmd", required=True)
    px = sub.add_parser("proxy", help="run an MCP server behind the gate (server command after --)")
    px.add_argument("--db", required=True)
    px.add_argument("--ledger")
    px.add_argument("--tools", help="comma-separated tool names to gate (default: all tools)")
    px.add_argument("--key-arg", default="idempotency_key")
    ls = sub.add_parser("list", help="show claims")
    ls.add_argument("--db", required=True)
    rl = sub.add_parser("release", help="forget a CLAIMED/FAILED key after checking by hand")
    rl.add_argument("--db", required=True)
    rl.add_argument("key")
    a = p.parse_args(argv)

    if a.cmd == "proxy":
        if not server_cmd:
            p.error("give the MCP server command after --")
        gate = Gate(a.db, a.ledger)
        tools = set(t.strip() for t in a.tools.split(",") if t.strip()) if a.tools else None
        return Proxy(server_cmd, gate, tools, a.key_arg).run()
    gate = Gate(a.db)
    if a.cmd == "list":
        for row in gate.store.all():
            print(json.dumps({k: row[k] for k in ("key", "tool", "state", "error")}))
        return 0
    if a.cmd == "release":
        ok = gate.store.release(a.key)
        print("released" if ok else "not released (unknown key, or already DONE)")
        return 0 if ok else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
