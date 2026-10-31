"""A tiny fake MCP server for tests. Every tools/call increments a counter file, so tests can prove whether the
real side effect ran. Tool "crash" exits without answering (simulates a crash mid-call). Tool "boom" returns a
JSON-RPC error. Tool "slow" counts the call, then does not answer until a file named <counter>.release exists
(a test creates it), so a test can hold a call in flight for as long as it needs."""
import json
import sys
import time
from pathlib import Path

counter = Path(sys.argv[1])
release = Path(str(counter) + ".release")


def bump(name):
    data = json.loads(counter.read_text()) if counter.exists() else {}
    data[name] = data.get(name, 0) + 1
    counter.write_text(json.dumps(data))


for raw in sys.stdin:
    if not raw.strip():
        continue
    msg = json.loads(raw)
    if msg.get("method") == "initialize":
        out = {"jsonrpc": "2.0", "id": msg["id"], "result": {"protocolVersion": "2025-06-18", "capabilities": {},
                                                              "serverInfo": {"name": "fake", "version": "0"}}}
    elif msg.get("method") == "tools/call":
        name = msg["params"]["name"]
        bump(name)
        if name == "crash":
            sys.exit(3)
        if name == "slow":
            deadline = time.monotonic() + 60  # never hang a test run forever
            while not release.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
        if name == "boom":
            out = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -1, "message": "boom"}}
        else:
            out = {"jsonrpc": "2.0", "id": msg["id"],
                   "result": {"content": [{"type": "text", "text": f"did {name} {json.dumps(msg['params'].get('arguments'))}"}]}}
    elif "id" in msg:
        out = {"jsonrpc": "2.0", "id": msg["id"], "result": {}}
    else:
        continue
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()
