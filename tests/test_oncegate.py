import json
import multiprocessing
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from oncegate import Gate, Indeterminate, key_for  # noqa: E402
from oncegate.store import Store  # noqa: E402

FAKE = str(Path(__file__).with_name("fake_server.py"))


def _claim_worker(db, key, q):
    won, _ = Store(db).claim(key, "t")
    q.put(won)


class LibraryGate(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.gate = Gate(self.tmp / "c.db", self.tmp / "log.jsonl")
        self.calls = 0

    def act(self):
        self.calls += 1
        return {"ok": self.calls}

    def test_second_identical_call_replays_without_running(self):
        a = self.gate.run("send_email", self.act, {"to": "x@y.z", "body": "hi"})
        b = self.gate.run("send_email", self.act, {"body": "hi", "to": "x@y.z"})  # same args, other order
        self.assertEqual(self.calls, 1)
        self.assertEqual(a, b)

    def test_different_arguments_are_different_actions(self):
        self.gate.run("send_email", self.act, {"to": "a"})
        self.gate.run("send_email", self.act, {"to": "b"})
        self.assertEqual(self.calls, 2)

    def test_explicit_key_overrides_arguments(self):
        self.gate.run("charge", self.act, {"amount": 5}, key="order-17")
        self.gate.run("charge", self.act, {"amount": 5, "note": "retry"}, key="order-17")
        self.assertEqual(self.calls, 1)

    def test_failure_is_indeterminate_not_retried(self):
        def explode():
            self.calls += 1
            raise TimeoutError("network died after sending?")
        with self.assertRaises(TimeoutError):
            self.gate.run("charge", explode, {"amount": 5})
        with self.assertRaises(Indeterminate):
            self.gate.run("charge", explode, {"amount": 5})
        self.assertEqual(self.calls, 1)

    def test_release_allows_rerun_only_after_human_release(self):
        k = key_for("charge", {"amount": 5})
        self.gate.store.claim(k, "charge")  # simulate a crash: claimed, never finished
        with self.assertRaises(Indeterminate):
            self.gate.run("charge", self.act, {"amount": 5})
        self.assertTrue(self.gate.store.release(k))
        self.gate.run("charge", self.act, {"amount": 5})
        self.assertEqual(self.calls, 1)

    def test_done_keys_cannot_be_released(self):
        self.gate.run("charge", self.act, {"amount": 5})
        self.assertFalse(self.gate.store.release(key_for("charge", {"amount": 5})))

    def test_ledger_records_every_decision(self):
        self.gate.run("x", self.act, {})
        self.gate.run("x", self.act, {})
        events = [json.loads(l)["event"] for l in (self.tmp / "log.jsonl").read_text().splitlines()]
        self.assertEqual(events, ["claimed", "done", "replayed"])


class ConcurrentClaims(unittest.TestCase):
    def test_exactly_one_process_wins_a_key(self):
        tmp = Path(tempfile.mkdtemp())
        db = str(tmp / "c.db")
        Store(db).close()
        q = multiprocessing.Queue()
        procs = [multiprocessing.Process(target=_claim_worker, args=(db, "same-key", q)) for _ in range(8)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(30)
        wins = [q.get(timeout=5) for _ in procs]
        self.assertEqual(sum(wins), 1)


class ProxyEndToEnd(unittest.TestCase):
    def run_proxy(self, messages, tools=None):
        tmp = Path(tempfile.mkdtemp())
        counter = tmp / "count.json"
        cmd = [sys.executable, "-m", "oncegate", "proxy", "--db", str(tmp / "c.db"), "--ledger", str(tmp / "l.jsonl")]
        if tools:
            cmd += ["--tools", tools]
        cmd += ["--", sys.executable, FAKE, str(counter)]
        inp = "".join(json.dumps(m) + "\n" for m in messages)
        out = subprocess.run(cmd, input=inp.encode(), capture_output=True, cwd=str(ROOT), timeout=60)
        replies = [json.loads(l) for l in out.stdout.decode().splitlines() if l.strip()]
        counts = json.loads(counter.read_text()) if counter.exists() else {}
        return replies, counts, tmp

    @staticmethod
    def call(i, name, **args):
        return {"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": name, "arguments": args}}

    def test_duplicate_call_is_answered_without_reaching_server(self):
        replies, counts, _ = self.run_proxy([self.call(1, "send", to="a"), self.call(2, "send", to="a")])
        self.assertEqual(counts.get("send"), 1)
        by_id = {r["id"]: r for r in replies}
        self.assertEqual(by_id[1]["result"], by_id[2]["result"])

    def test_ungated_tools_pass_straight_through(self):
        _, counts, _ = self.run_proxy([self.call(1, "read", q="x"), self.call(2, "read", q="x")], tools="send")
        self.assertEqual(counts.get("read"), 2)

    def test_crash_mid_call_makes_the_next_attempt_indeterminate(self):
        replies, counts, tmp = self.run_proxy([self.call(1, "crash", n=1)])
        self.assertEqual(counts.get("crash"), 1)
        # New proxy session against the same claim table: the retry must NOT reach the server.
        counter = tmp / "count.json"
        cmd = [sys.executable, "-m", "oncegate", "proxy", "--db", str(tmp / "c.db"), "--",
               sys.executable, FAKE, str(counter)]
        out = subprocess.run(cmd, input=(json.dumps(self.call(9, "crash", n=1)) + "\n").encode(),
                             capture_output=True, cwd=str(ROOT), timeout=60)
        reply = json.loads(out.stdout.decode().splitlines()[0])
        self.assertIn("INDETERMINATE", reply["error"]["message"])
        self.assertEqual(json.loads(counter.read_text()).get("crash"), 1)

    def test_server_error_is_not_retried_automatically(self):
        replies, counts, _ = self.run_proxy([self.call(1, "boom"), self.call(2, "boom")])
        self.assertEqual(counts.get("boom"), 1)
        # The duplicate either waited for the first answer (same error) or arrived after it (INDETERMINATE);
        # either way it is an error and the server ran once.
        self.assertIn("error", [r for r in replies if r["id"] == 2][0])

    def test_explicit_idempotency_key_argument(self):
        _, counts, _ = self.run_proxy([self.call(1, "pay", idempotency_key="inv-9", amount=5),
                                       self.call(2, "pay", idempotency_key="inv-9", amount=5, retry=True)])
        self.assertEqual(counts.get("pay"), 1)


if __name__ == "__main__":
    unittest.main()
