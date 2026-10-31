"""Releasing an INDETERMINATE action needs an outcome and evidence; a reconciler may answer only from the
receiver's records. Every test here proves a guard REJECTS (or that nothing ran), using the fake server's counter
where a server is involved."""
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from oncegate import EvidenceRequired, Gate, Indeterminate, action_hash, key_for  # noqa: E402
from oncegate.proxy import Proxy  # noqa: E402
from oncegate.store import CLAIMED, DONE, FAILED, Store  # noqa: E402

FAKE = str(Path(__file__).with_name("fake_server.py"))
EVIDENCE = "Stripe dashboard: no charge for order 17 between 10:02 and 10:20"
ARGS = {"order": 17, "amount": 5}


def call(i, name, args):
    return {"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": name, "arguments": args}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = str(self.tmp / "c.db")
        self.ledger = self.tmp / "l.jsonl"
        self.counter = self.tmp / "count.json"
        self.calls = 0

    def act(self):
        self.calls += 1
        return {"ok": self.calls}

    def cli(self, *args):
        out = subprocess.run([sys.executable, "-m", "oncegate", *args], capture_output=True, cwd=str(ROOT),
                             stdin=subprocess.DEVNULL, timeout=60)
        return out.returncode, out.stdout.decode(), out.stderr.decode()

    def release_cli(self, key, *extra):
        return self.cli("release", "--db", self.db, "--ledger", str(self.ledger), key, *extra)

    def proxy_cli(self, messages):
        """A real proxy process in front of the fake server."""
        cmd = [sys.executable, "-m", "oncegate", "proxy", "--db", self.db, "--ledger", str(self.ledger), "--",
               sys.executable, FAKE, str(self.counter)]
        inp = "".join(json.dumps(m) + "\n" for m in messages)
        out = subprocess.run(cmd, input=inp.encode(), capture_output=True, cwd=str(ROOT), timeout=60)
        return {r["id"]: r for r in (json.loads(l) for l in out.stdout.decode().splitlines() if l.strip())}

    def proxy_with(self, gate, messages):
        """The same proxy in this process, so the gate can carry a reconciler (a library-only option)."""
        inp = io.BytesIO("".join(json.dumps(m) + "\n" for m in messages).encode())
        out = io.BytesIO()
        Proxy([sys.executable, FAKE, str(self.counter)], gate, None, stdin=inp, stdout=out).run()
        return {r["id"]: r for r in (json.loads(l) for l in out.getvalue().decode().splitlines() if l.strip())}

    def runs(self, tool):
        return (json.loads(self.counter.read_text()) if self.counter.exists() else {}).get(tool, 0)

    def events(self, name=None):
        ev = [json.loads(l) for l in self.ledger.read_text(encoding="utf-8").splitlines()] \
            if self.ledger.exists() else []
        return [e for e in ev if name is None or e["event"] == name]

    def stuck(self, tool="send", args=ARGS, state=CLAIMED):
        """Simulate a crash mid-call: the key is claimed (and maybe failed) but the server never answered."""
        store = Store(self.db)
        k = key_for(tool, args)
        store.claim(k, tool, action_hash(tool, args))
        if state == FAILED:
            store.fail(k, "TimeoutError: no answer")
        return k


class EvidenceGuard(Base):
    def test_release_without_outcome_or_evidence_is_refused(self):
        k = self.stuck()
        for extra in ([], ["--outcome", "did-not-happen"], ["--evidence", EVIDENCE],
                      ["--outcome", "happened"]):
            code, out, err = self.release_cli(k, *extra)
            self.assertEqual(code, 2, extra)
            self.assertIn("--outcome did-not-happen", err)
            self.assertIn("--evidence", err)
        gate = Gate(self.db)
        for outcome, evidence in ((None, EVIDENCE), ("did-not-happen", None), ("maybe", EVIDENCE),
                                  ("", EVIDENCE), ("happened", ""), ("did-not-happen", 12345678901234567)):
            with self.assertRaises(EvidenceRequired):
                gate.release(k, outcome, evidence)
        # Nothing changed: still stuck, still refused, no history, no receipt, nothing ran.
        self.assertEqual(gate.store.get(k)["state"], CLAIMED)
        self.assertEqual(gate.store.releases(), [])
        self.assertEqual(self.events("released"), [])
        with self.assertRaises(Indeterminate):
            gate.run("send", self.act, ARGS)
        self.assertEqual(self.calls, 0)

    def test_too_short_evidence_is_refused(self):
        k = self.stuck()
        gate = Gate(self.db)
        for evidence in ("checked", "x" * 14, "   " + "y" * 14 + "   \n", " " * 40):
            with self.assertRaises(EvidenceRequired):
                gate.release(k, "did-not-happen", evidence)
            code, _, err = self.release_cli(k, "--outcome", "did-not-happen", "--evidence", evidence)
            self.assertEqual(code, 2)
            self.assertIn("at least 15 characters", err)
        self.assertEqual(gate.store.get(k)["state"], CLAIMED)
        self.assertEqual(gate.store.releases(), [])
        self.assertIsNotNone(gate.release(k, "did-not-happen", "z" * 15))  # the boundary is accepted

    def test_store_level_release_has_the_same_guard(self):
        k = self.stuck()
        with self.assertRaises(EvidenceRequired):
            Store(self.db).release(k, "did-not-happen", "")
        self.assertEqual(Store(self.db).get(k)["state"], CLAIMED)


class Outcomes(Base):
    def test_did_not_happen_allows_exactly_one_new_run_and_keeps_history(self):
        r = self.proxy_cli([call(1, "crash", ARGS)])  # server dies mid-call: CLAIMED, INDETERMINATE
        self.assertEqual(self.runs("crash"), 1)
        k = key_for("crash", ARGS)
        code, out, _ = self.release_cli(k, "--outcome", "did-not-happen", "--evidence", EVIDENCE, "--by", "ops")
        self.assertEqual(code, 0, out)
        r = self.proxy_cli([call(2, "crash", ARGS)])
        self.assertEqual(self.runs("crash"), 2)  # exactly one new run...
        r = self.proxy_cli([call(3, "crash", ARGS)])
        self.assertEqual(self.runs("crash"), 2)  # ...and it does not unlock a third
        self.assertIn("INDETERMINATE", r[3]["error"]["message"])
        code, out, _ = self.cli("list", "--db", self.db, "--releases")
        hist = [json.loads(l) for l in out.splitlines()]
        self.assertEqual(len(hist), 1)
        self.assertEqual((hist[0]["key"], hist[0]["outcome"], hist[0]["evidence"], hist[0]["released_by"],
                          hist[0]["prior_state"]), (k, "did-not-happen", EVIDENCE, "ops", CLAIMED))
        self.assertEqual(hist[0]["action_hash"], action_hash("crash", ARGS))

    def test_did_not_happen_library_runs_once_then_replays(self):
        k = self.stuck(state=FAILED)
        gate = Gate(self.db, self.ledger)
        self.assertIsNotNone(gate.release(k, "did-not-happen", EVIDENCE, "ops"))
        self.assertIsNone(gate.release(k, "did-not-happen", EVIDENCE))  # nothing left to release
        gate.run("send", self.act, ARGS)
        gate.run("send", self.act, ARGS)
        self.assertEqual(self.calls, 1)
        self.assertEqual(gate.store.releases()[0]["prior_state"], FAILED)
        self.assertEqual(gate.store.releases()[0]["prior_error"], "TimeoutError: no answer")

    def test_happened_makes_a_retry_replay_and_the_server_is_not_called(self):
        self.proxy_cli([call(1, "crash", ARGS)])
        self.assertEqual(self.runs("crash"), 1)
        k = key_for("crash", ARGS)
        code, out, _ = self.release_cli(k, "--outcome", "happened", "--evidence",
                                        "Stripe dashboard: charge ch_123 for order 17 at 10:03")
        self.assertEqual(code, 0, out)
        r = self.proxy_cli([call(2, "crash", ARGS), call(3, "crash", ARGS)])
        self.assertEqual(self.runs("crash"), 1)  # nothing re-ran
        for i in (2, 3):
            self.assertTrue(r[i]["result"]["reconciled"])
            self.assertEqual(r[i]["result"]["outcome"], "happened")
            self.assertIn("ch_123", r[i]["result"]["evidence"])
        self.assertEqual(Store(self.db).get(k)["state"], DONE)

    def test_done_keys_cannot_be_released(self):
        self.proxy_cli([call(1, "send", ARGS)])
        k = key_for("send", ARGS)
        before = Store(self.db).get(k)
        for outcome in ("did-not-happen", "happened"):
            code, _, err = self.release_cli(k, "--outcome", outcome, "--evidence", EVIDENCE)
            self.assertEqual(code, 1)
            self.assertIn("DONE", err)
            self.assertIsNone(Gate(self.db).release(k, outcome, EVIDENCE))
        self.assertEqual(Store(self.db).get(k), before)
        self.proxy_cli([call(2, "send", ARGS)])
        self.assertEqual(self.runs("send"), 1)
        self.assertEqual(Store(self.db).releases(), [])
        code, _, _ = self.release_cli("send:no-such-key", "--outcome", "did-not-happen", "--evidence", EVIDENCE)
        self.assertEqual(code, 1)

    def test_release_only_takes_the_claim_it_looked_at(self):
        k = self.stuck()
        store = Store(self.db)
        seen = store.get(k)["claimed_at"]
        self.assertIsNone(store.release(k, "did-not-happen", EVIDENCE, expect_claimed_at=seen - 1))
        self.assertEqual(store.get(k)["state"], CLAIMED)
        self.assertIsNotNone(store.release(k, "did-not-happen", EVIDENCE, expect_claimed_at=seen))

    def test_a_020_database_without_action_hash_still_releases(self):
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE claims (key TEXT PRIMARY KEY, tool TEXT NOT NULL, state TEXT NOT NULL, "
                    "result TEXT, error TEXT, claimed_at REAL NOT NULL, updated_at REAL NOT NULL)")
        con.execute("INSERT INTO claims VALUES ('send:old', 'send', 'CLAIMED', NULL, NULL, 1, 1)")
        con.commit()
        con.close()
        rel = Gate(self.db, self.ledger).release("send:old", "did-not-happen", EVIDENCE)
        self.assertEqual((rel["key"], rel["action_hash"]), ("send:old", None))
        self.assertIsNone(Store(self.db).get("send:old"))


class Reconciler(Base):
    def gate(self, answer, **kw):
        self.asked = []

        def reconciler(tool, arguments, key):
            self.asked.append((tool, arguments, key))
            if isinstance(answer, Exception):
                raise answer
            return answer
        return Gate(self.db, self.ledger, reconciler=reconciler, reconcile_min_age=kw.pop("min_age", 0), **kw)

    def test_unknown_or_error_keeps_the_call_refused_and_the_server_is_not_called(self):
        k = self.stuck()
        for answer in ("unknown", RuntimeError("provider lookup timed out"), None, "yes", "HAPPENED", True):
            r = self.proxy_with(self.gate(answer), [call(1, "send", ARGS)])
            self.assertEqual(self.runs("send"), 0, f"server was called after reconciler answered {answer!r}")
            self.assertIn("error", r[1], f"call was answered, not refused, after {answer!r}")
            self.assertIn("INDETERMINATE", r[1]["error"]["message"])
            self.assertEqual(len(self.asked), 1)
            self.assertEqual(Store(self.db).get(k)["state"], CLAIMED)
        with self.assertRaises(Indeterminate):
            self.gate("unknown").run("send", self.act, ARGS)
        self.assertEqual(self.calls, 0)
        self.assertEqual(Store(self.db).releases(), [])
        refused = self.events("reconciled")
        self.assertEqual(len(refused), 7)
        self.assertTrue(all(e["decision"] == "refused" and e["reason"] == "unknown" for e in refused))
        self.assertIn("provider lookup timed out", refused[1]["error"])

    def test_happened_replays_with_no_run(self):
        k = self.stuck()
        r = self.proxy_with(self.gate("happened"), [call(1, "send", ARGS), call(2, "send", ARGS)])
        self.assertEqual(self.runs("send"), 0)
        self.assertEqual(len(self.asked), 1)  # the second call found DONE and did not ask again
        self.assertEqual(self.asked[0], ("send", ARGS, k))
        for i in (1, 2):
            self.assertTrue(r[i]["result"]["reconciled"])
            self.assertEqual(r[i]["result"]["evidence"], "reconciler: happened")
        rel = Store(self.db).releases()
        self.assertEqual([(x["outcome"], x["evidence"], x["released_by"]) for x in rel],
                         [("happened", "reconciler: happened", "reconciler")])

    def test_did_not_happen_runs_once(self):
        k = self.stuck(state=FAILED)
        r = self.proxy_with(self.gate("did-not-happen"), [call(1, "send", ARGS)])
        self.assertEqual(self.runs("send"), 1)
        self.assertIn("did send", r[1]["result"]["content"][0]["text"])
        r = self.proxy_with(self.gate("did-not-happen"), [call(2, "send", ARGS)])
        self.assertEqual(self.runs("send"), 1)  # replayed, not run again
        self.assertEqual(self.asked, [])  # DONE: the reconciler is not consulted
        self.assertEqual(Store(self.db).get(k)["state"], DONE)
        self.assertEqual(Store(self.db).releases()[0]["evidence"], "reconciler: did-not-happen")

    def test_did_not_happen_still_needs_approval(self):
        self.stuck()
        g = self.gate("did-not-happen", require_approval={"send"})
        r = self.proxy_with(g, [call(1, "send", ARGS)])
        self.assertEqual(r[1]["error"]["data"]["code"], "APPROVAL_REQUIRED")
        self.assertEqual(self.runs("send"), 0)

    def test_a_young_claim_is_not_reconciled(self):
        self.stuck()
        g = self.gate("did-not-happen", min_age=3600)
        r = self.proxy_with(g, [call(1, "send", ARGS)])
        self.assertIn("INDETERMINATE", r[1]["error"]["message"])
        self.assertEqual(self.asked, [])
        self.assertEqual(self.runs("send"), 0)

    def test_default_min_age_protects_a_fresh_claim(self):
        self.stuck()
        asked = []
        g = Gate(self.db, reconciler=lambda *a: asked.append(a) or "did-not-happen")
        with self.assertRaises(Indeterminate):
            g.run("send", self.act, ARGS)
        self.assertEqual((asked, self.calls), ([], 0))


class Receipts(Base):
    def test_release_and_reconcile_write_receipts(self):
        k = self.stuck()
        code, _, _ = self.release_cli(k, "--outcome", "did-not-happen", "--evidence", EVIDENCE, "--by", "ops")
        self.assertEqual(code, 0)
        k2 = self.stuck("charge", {"order": 18})
        self.proxy_with(Reconciler.gate(self, "happened"), [call(1, "charge", {"order": 18})])
        ev = self.events("released") + self.events("reconciled")
        self.assertEqual(len(ev), 2)
        rel, rec = ev
        self.assertEqual((rel["decision"], rel["reason"], rel["evidence"], rel["by"], rel["tool"], rel["key"]),
                         ("allowed", "did-not-happen", EVIDENCE, "ops", "send", k))
        self.assertEqual(rel["action_hash"], action_hash("send", ARGS))
        self.assertEqual((rec["decision"], rec["reason"], rec["evidence"], rec["by"], rec["tool"], rec["key"]),
                         ("replayed", "happened", "reconciler: happened", "reconciler", "charge", k2))
        self.assertEqual(rec["action_hash"], action_hash("charge", {"order": 18}))
        for e in ev:
            self.assertRegex(e["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
            self.assertEqual(e["tool_name"], e["tool"])

    def test_refused_release_writes_no_receipt(self):
        k = self.stuck()
        self.release_cli(k, "--outcome", "did-not-happen", "--evidence", "too short")
        self.release_cli(k)
        self.assertEqual(self.events(), [])


if __name__ == "__main__":
    unittest.main()
