"""oncegate CLI.

  oncegate proxy --db claims.db [--ledger log.jsonl] [--tools send_email,charge_card]
                 [--require-approval send_email] [--stop-file STOP] -- <mcp server command...>
  oncegate approve --db claims.db --tool send_email --args-json '{"to": "a@example.com"}' [--ttl-hours 24]
  oncegate approve --db claims.db --pending [--hash HASH]
  oncegate list --db claims.db [--approvals | --pending]
  oncegate release --db claims.db KEY

Approvals are created ONLY here, by a person at a terminal. On an interactive terminal the person must type the
first 8 characters of the action hash. Without a terminal, approval is refused unless --i-am-a-human is given.
This is a speed bump against an agent approving its own calls, not a cryptographic guarantee: anything that can run
this command with --i-am-a-human, or write to the database file, can create an approval.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from .gate import Gate, action_hash, canonical_json, find_relayed_approvals
from .proxy import Proxy

CONFIRM_CHARS = 8


def _is_terminal(stream) -> bool:
    try:
        if not stream.isatty():
            return False
        if os.name == "nt":
            # On Windows the NUL device also reports isatty(); only a real console has a console mode.
            import ctypes
            import msvcrt
            mode = ctypes.c_uint32()
            handle = msvcrt.get_osfhandle(stream.fileno())
            return bool(ctypes.windll.kernel32.GetConsoleMode(ctypes.c_void_p(handle), ctypes.byref(mode)))
        return True
    except (AttributeError, ValueError, OSError):
        return False


def _isatty() -> bool:
    """A person must be able both to read the payload and to type the confirmation."""
    return _is_terminal(sys.stdin) and _is_terminal(sys.stdout)


def _input(prompt: str) -> str:
    return input(prompt)


def _show(tool: str, args, h: str, expires_at: float) -> None:
    print("You are approving exactly this one call, once:")
    print(f"  tool:        {tool}")
    print("  arguments:")
    for line in json.dumps(args, indent=2, sort_keys=True, ensure_ascii=False).splitlines():
        print(f"    {line}")
    print(f"  exact bytes: {canonical_json(args)}")
    print(f"  action hash: {h}")
    print(f"  expires:     {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(expires_at))} (single use)")
    relayed = find_relayed_approvals(args, "arguments")
    if relayed:
        print(f"  note: {', '.join(relayed)} look like approval fields. They grant nothing; only this command does.")


def _grant(gate: Gate, tool: str, args, ttl_hours: float, human_flag: bool) -> int:
    h = action_hash(tool, args)
    expires_at = time.time() + ttl_hours * 3600
    _show(tool, args, h, expires_at)
    if _isatty():
        typed = _input(f"Type the first {CONFIRM_CHARS} characters of the action hash to approve: ").strip().lower()
        if typed != h[:CONFIRM_CHARS]:
            print("not approved: confirmation did not match", file=sys.stderr)
            return 1
    elif not human_flag:
        print("not approved: no interactive terminal. Approvals must come from a person at a terminal. If you are a "
              "person approving from a script, add --i-am-a-human.", file=sys.stderr)
        return 2
    approval_id = gate.store.add_approval(h, tool, canonical_json(args), expires_at)
    print(f"approved: approval {approval_id} for action {h}")
    return 0


def _approve(gate: Gate, a) -> int:
    if a.ttl_hours <= 0:
        print("--ttl-hours must be positive", file=sys.stderr)
        return 2
    if not a.pending:
        if not a.tool or a.args_json is None:
            print("give --tool and --args-json, or --pending", file=sys.stderr)
            return 2
        try:
            args = json.loads(a.args_json)
        except ValueError as exc:
            print(f"--args-json is not valid JSON: {exc}", file=sys.stderr)
            return 2
        if not isinstance(args, dict):
            print("--args-json must be a JSON object", file=sys.stderr)
            return 2
        return _grant(gate, a.tool, args, a.ttl_hours, a.i_am_a_human)

    pending = gate.store.pending()
    if not pending:
        print("no blocked calls waiting for approval")
        return 0
    for i, p in enumerate(pending, 1):
        print(f"[{i}] {p['action_hash'][:16]}  {p['tool']}  {p['args_json'][:120]}  (attempts: {p['attempts']})")
    if a.hash:
        chosen = [p for p in pending if p["action_hash"].startswith(a.hash.lower())]
        if len(chosen) != 1:
            print("--hash must match exactly one pending call", file=sys.stderr)
            return 2
        p = chosen[0]
    elif _isatty():
        pick = _input("Number to review (blank to stop): ").strip()
        if not pick:
            return 0
        if not pick.isdigit() or not 1 <= int(pick) <= len(pending):
            print("no such number", file=sys.stderr)
            return 2
        p = pending[int(pick) - 1]
    else:
        print("not approved: no interactive terminal; pick one with --hash (and --i-am-a-human)", file=sys.stderr)
        return 2
    args = json.loads(p["args_json"])
    if action_hash(p["tool"], args) != p["action_hash"]:
        print("not approved: pending record does not match its hash", file=sys.stderr)
        return 2
    return _grant(gate, p["tool"], args, a.ttl_hours, a.i_am_a_human)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    server_cmd = []
    if "--" in argv:
        i = argv.index("--")
        argv, server_cmd = argv[:i], argv[i + 1:]
    p = argparse.ArgumentParser(prog="oncegate", description="At-most-once gate for agent side effects.")
    sub = p.add_subparsers(dest="cmd", required=True)
    px = sub.add_parser("proxy", help="run an MCP server behind the gate (server command after --)")
    px.add_argument("--db", required=True)
    px.add_argument("--ledger")
    px.add_argument("--tools", help="comma-separated tool names to gate (default: all tools)")
    px.add_argument("--key-arg", default="idempotency_key")
    px.add_argument("--require-approval", help="comma-separated tools that need an exact human approval per call")
    px.add_argument("--stop-file", help="if this path exists (or cannot be checked), refuse gated calls")
    ap = sub.add_parser("approve", help="approve one exact call (human only)")
    ap.add_argument("--db", required=True)
    ap.add_argument("--tool")
    ap.add_argument("--args-json")
    ap.add_argument("--pending", action="store_true", help="list blocked calls and approve one")
    ap.add_argument("--hash", help="with --pending: the action hash (or a unique prefix) to approve")
    ap.add_argument("--ttl-hours", type=float, default=24.0)
    ap.add_argument("--i-am-a-human", action="store_true",
                    help="allow approval without an interactive terminal (a speed bump, not a guarantee)")
    ls = sub.add_parser("list", help="show claims")
    ls.add_argument("--db", required=True)
    ls.add_argument("--approvals", action="store_true", help="show approvals instead")
    ls.add_argument("--pending", action="store_true", help="show calls blocked for approval instead")
    rl = sub.add_parser("release", help="forget a CLAIMED/FAILED key after checking by hand")
    rl.add_argument("--db", required=True)
    rl.add_argument("key")
    a = p.parse_args(argv)

    def csv(v):
        return set(t.strip() for t in v.split(",") if t.strip()) if v else None

    if a.cmd == "proxy":
        if not server_cmd:
            p.error("give the MCP server command after --")
        gate = Gate(a.db, a.ledger, require_approval=csv(a.require_approval), stop_file=a.stop_file)
        return Proxy(server_cmd, gate, csv(a.tools), a.key_arg).run()
    gate = Gate(a.db)
    if a.cmd == "approve":
        return _approve(gate, a)
    if a.cmd == "list":
        if a.approvals:
            rows = gate.store.approvals()
        elif a.pending:
            rows = gate.store.pending()
        else:
            rows = [{k: r[k] for k in ("key", "tool", "state", "error")} for r in gate.store.all()]
        for row in rows:
            print(json.dumps(row, ensure_ascii=False))
        return 0
    if a.cmd == "release":
        ok = gate.store.release(a.key)
        print("released" if ok else "not released (unknown key, or already DONE)")
        return 0 if ok else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
