"""MCP stdio proxy. Sits between an agent and an MCP server and makes chosen side-effect tools run at most once.

The agent talks to this proxy exactly as it would talk to the server (newline-delimited JSON-RPC on stdin/stdout).
Every message passes through unchanged, except `tools/call` requests for a gated tool:
- first time: the call is claimed in the claim table, then forwarded; the server's reply is stored;
- repeat (same tool + same arguments, or same `idempotency_key` argument): the stored reply is returned and the
  server is NOT called;
- started but never finished (crash, timeout, server error): the proxy answers with an error saying the action is
  INDETERMINATE and does not call the server. A human checks and runs `oncegate release KEY`.

Tools listed in `require_approval` also need a human approval for the EXACT call (tool + canonical arguments),
created only with `oncegate approve`. Without one the proxy answers APPROVAL_REQUIRED and does not forward.
Nothing in the message stream can approve: approval-looking fields are ignored and logged.
With a STOP file configured, the proxy checks it immediately before forwarding and answers STOPPED if it exists
or cannot be checked.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading

from .gate import ApprovalRequired, Gate, Indeterminate, Stopped, action_hash, key_for

INDETERMINATE_CODE = -32010
APPROVAL_REQUIRED_CODE = -32011
STOPPED_CODE = -32012


def _error(msg_id, code, message, data=None):
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": err}


class Proxy:
    def __init__(self, command: list[str], gate: Gate, gated_tools: set[str] | None, key_arg: str = "idempotency_key",
                 stdin=None, stdout=None):
        self.command = command
        self.gate = gate
        self.gated = gated_tools  # None = gate every tool
        self.key_arg = key_arg
        self.inp = stdin or sys.stdin.buffer
        self.out = stdout or sys.stdout.buffer
        self.pending: dict = {}  # request id -> (key, tool, action_hash) for calls forwarded under a claim
        self.inflight: dict = {}  # key -> request ids of duplicates waiting for the first call's answer
        self.wlock = threading.Lock()
        self.slock = threading.Lock()

    def _send_client(self, obj) -> None:
        with self.wlock:
            self.out.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
            self.out.flush()

    def _is_gated(self, name: str) -> bool:
        return self.gated is None or name in self.gated or self.gate.needs_approval(name)

    def handle_client_line(self, line: bytes, server_stdin) -> None:
        """Decide what to do with one message from the agent. Returns nothing; writes to server or client."""
        try:
            msg = json.loads(line)
        except ValueError:
            server_stdin.write(line)
            server_stdin.flush()
            return
        if isinstance(msg, dict) and msg.get("method") == "tools/call" and "id" in msg:
            params = msg.get("params") or {}
            name = params.get("name", "")
            if self._is_gated(name):
                args = params.get("arguments") or {}
                explicit = args.get(self.key_arg) if isinstance(args, dict) else None
                explicit = str(explicit) if explicit is not None else None
                envelope = {k: v for k, v in params.items() if k != "arguments"}
                key = key_for(name, args, explicit)
                with self.slock:
                    if key in self.inflight:
                        # Same action already running in this session: wait for its answer, don't call again.
                        self.inflight[key].append(msg["id"])
                        self.gate.ledger.write("replayed", "replayed", "joined_inflight", action_hash(name, args),
                                               name, key=key)
                        return
                    try:
                        action, stored, ticket = self.gate.admit(name, args, explicit, envelope)
                    except Indeterminate as exc:
                        self._send_client(_error(msg["id"], INDETERMINATE_CODE, str(exc), {"key": exc.key}))
                        return
                    except ApprovalRequired as exc:
                        self._send_client(_error(msg["id"], APPROVAL_REQUIRED_CODE, str(exc), {
                            "code": "APPROVAL_REQUIRED", "action_hash": exc.action_hash, "tool": exc.tool,
                            "arguments": exc.arguments, "summary": exc.summary}))
                        return
                    except Stopped as exc:
                        self._send_client(_error(msg["id"], STOPPED_CODE, str(exc), {
                            "code": "STOPPED", "action_hash": exc.action_hash, "tool": exc.tool,
                            "reason": exc.reason}))
                        return
                    if action == "replay":
                        self._send_client({"jsonrpc": "2.0", "id": msg["id"], "result": stored})
                        return
                    self.pending[msg["id"]] = (key, name, ticket["action_hash"])
                    self.inflight[key] = []
        server_stdin.write(line if line.endswith(b"\n") else line + b"\n")
        server_stdin.flush()

    def handle_server_line(self, line: bytes) -> None:
        try:
            msg = json.loads(line)
        except ValueError:
            with self.wlock:
                self.out.write(line)
                self.out.flush()
            return
        if isinstance(msg, dict) and "id" in msg and msg["id"] in self.pending and (
                "result" in msg or "error" in msg):
            with self.slock:
                key, name, h = self.pending.pop(msg["id"])
                result = msg.get("result")
                if "error" in msg or (isinstance(result, dict) and result.get("isError")):
                    self.gate.failed(key, name, json.dumps(msg.get("error") or result)[:2000], h)
                else:
                    self.gate.done(key, name, result, h)
                waiters = self.inflight.pop(key, [])
            for wid in waiters:
                reply = dict(msg)
                reply["id"] = wid
                self._send_client(reply)
        with self.wlock:
            self.out.write(line if line.endswith(b"\n") else line + b"\n")
            self.out.flush()

    def run(self) -> int:
        server = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE)

        def pump_server():
            for line in server.stdout:
                self.handle_server_line(line)

        t = threading.Thread(target=pump_server, daemon=True)
        t.start()
        try:
            for line in self.inp:
                if line.strip():
                    self.handle_client_line(line, server.stdin)
        finally:
            try:
                server.stdin.close()
            except OSError:
                pass
            server.wait(timeout=30)
            t.join(timeout=5)
        # Anything still pending never got an answer: leave it CLAIMED, i.e. INDETERMINATE.
        return server.returncode or 0
