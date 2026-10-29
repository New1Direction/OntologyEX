"""The stale-view check: a decision applies only to what the person saw, or nothing is written."""
import importlib.util
import json
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ontology-extraction/scripts"))
import business_view as bv
import decisions
import stale_view as sv

spec = importlib.util.spec_from_file_location("ontologyex_stale_demo", ROOT / "examples/operating-picture/demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


class Marks:
    def __init__(self, now):
        self.value = now

    def now(self, *key):
        return self.value


def picture(strength="WEAK", calls=3, lines="aa"):
    return {"link_suggestions": {"suggestions": [{"id": "l1", "skill": "s", "binding": "b", "component": "c",
                                                  "strength": "STRONG", "signals": [{"signal": "NAME_MATCH"}]}]},
            "dependency_suggestions": {"suggestions": [{"id": "d1", "strength": strength, "signals": [
                {"signal": "OBSERVED_CALLS", "detail": f"{calls} calls, last seen now"}]}]},
            "rule_suggestions": {"suggestions": [{"id": "r1", "citation": {"path": "a.py", "start_line": 1, "end_line": 2},
                                                 "citation_check": {"status": "LINES_FOUND", "lines_sha256": lines}}]},
            "link_watermarks": [{"id": "w1", "now": {"a.py": "11"}}], "rule_decided": []}


class FingerprintTests(unittest.TestCase):
    def test_what_a_person_relies_on_and_nothing_else(self):
        base = sv.views(picture(), Marks({"a.py": "11"}), None)
        self.assertEqual(base, sv.views(picture(), Marks({"a.py": "11"}), None))  # deterministic
        self.assertNotEqual(base["links"]["suggestion"]["l1"],
                            sv.views(picture(), Marks({"a.py": "22"}), None)["links"]["suggestion"]["l1"])  # code changed
        self.assertEqual(base["dependencies"]["suggestion"]["d1"],  # live call counts move; the decision does not
                         sv.views(picture(calls=900), Marks({}), None)["dependencies"]["suggestion"]["d1"])
        self.assertNotEqual(base["dependencies"]["suggestion"]["d1"],
                            sv.views(picture(strength="STRONG"), Marks({}), None)["dependencies"]["suggestion"]["d1"])
        self.assertNotEqual(base["rules"]["suggestion"]["r1"],
                            sv.views(picture(lines="bb"), Marks({}), None)["rules"]["suggestion"]["r1"])
        self.assertIn("w1", base["links"]["decided"])

    def test_check_refuses_the_whole_batch(self):
        current = {"a": "1", "b": "2"}
        sv.check({"a": "1", "b": "2"}, ["a", "b"], current)
        with self.assertRaises(sv.StaleView) as caught:
            sv.check({"a": "1", "b": "old"}, ["a", "b"], current)
        self.assertEqual(caught.exception.changed, ["b"])
        with self.assertRaises(sv.StaleView):
            sv.check({"a": "1"}, ["a", "b"], current)  # an ID the page did not show is not seen
        self.assertFalse(sv.valid_seen(["a"]))
        self.assertFalse(sv.valid_seen({"a": ""}))
        self.assertTrue(sv.valid_seen({"a": "1"}))


class EndToEndTests(unittest.TestCase):
    """Through the page's route: the code changes between looking and clicking, and nothing is written."""

    def test_a_decision_on_a_changed_view_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            built = demo.build(Path(tmp) / "demo", unlinked=True)
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            process = subprocess.Popen([*built["command"], "--port", str(port), "--allow-link-decisions"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.addCleanup(lambda: (process.terminate(), process.wait(timeout=5)))
            base = f"http://127.0.0.1:{port}"
            for _ in range(150):
                try:
                    urllib.request.urlopen(base + "/api/state", timeout=2).close()
                    break
                except OSError:
                    time.sleep(0.1)

            def get(path):
                with urllib.request.urlopen(base + path, timeout=10) as resp:
                    return json.load(resp)
            with urllib.request.urlopen(base + "/business", timeout=5) as resp:
                token = resp.read().decode().split('name="ontologyex-write-token" content="')[1].split('"')[0]

            def decide(ids, decision, seen=None, raw=None):
                body = raw if raw is not None else {"ids": ids, "decision": decision, "by": "Dana", "note": "",
                                                    **({"seen": seen} if seen is not None else {})}
                request = urllib.request.Request(base + "/api/links/decision", method="POST", data=json.dumps(body).encode(),
                                                 headers={"Content-Type": "application/json", "Origin": base,
                                                          "X-OntologyEX-Token": token})
                try:
                    with urllib.request.urlopen(request, timeout=10) as resp:
                        return resp.status, json.load(resp)
                except urllib.error.HTTPError as exc:
                    return exc.code, json.load(exc)

            def confirmed():
                path = Path(built["links"])
                return [r["binding"] for r in bv.read_link_document(path)[0]] if path.exists() else []
            page = get("/api/business")  # the person looks
            suggestions = {s["binding"]: s for s in page["link_suggestions"]["suggestions"]}
            refund, other = suggestions["payments.apply_refund"], next(
                s for b, s in suggestions.items() if b != "payments.apply_refund")
            shown = page["views"]["links"]["suggestion"]
            before = confirmed()

            code = Path(built["repo"]) / "domain/payments.py"  # the code changes while they read
            code.write_text(code.read_text() + "\n# refunds now need a second approver\n")
            time.sleep(0.05)
            status, body = decide([refund["id"]], "confirm", {refund["id"]: shown[refund["id"]]})
            self.assertEqual((status, body["status"], body["changed"]), (409, "STALE_VIEW", [refund["id"]]))
            self.assertEqual(confirmed(), before)  # nothing written
            self.assertFalse(decisions.history_path(Path(built["links"])).exists())

            fresh = get("/api/business")["views"]["links"]["suggestion"]
            self.assertNotEqual(fresh[refund["id"]], shown[refund["id"]])
            status, body = decide([refund["id"], other["id"]], "confirm",
                                  {refund["id"]: fresh[refund["id"]], other["id"]: "f" * 24})
            self.assertEqual((status, body["changed"]), (409, [other["id"]]))  # one changed item refuses the batch
            self.assertEqual(confirmed(), before)

            status, body = decide([refund["id"]], "confirm", {refund["id"]: fresh[refund["id"]]})
            self.assertEqual((status, body["status"]), (200, "CONFIRMED"))  # decided on what is there now
            row = next(r for r in bv.read_link_document(Path(built["links"]))[0] if r["binding"] == "payments.apply_refund")
            self.assertEqual(sv.fingerprint({"strength": refund["strength"],
                                             "signals": sorted({s["signal"] for s in refund["signals"]}),
                                             "evidence": row["evidence"]}), fresh[refund["id"]])  # recorded = seen

            decided = get("/api/views")["links"]["decided"]  # re-confirming is checked the same way
            code.write_text(code.read_text() + "\n# and a third\n")
            time.sleep(0.05)
            decision_id = next(iter(decided))
            self.assertEqual(decide([decision_id], "reconfirm", {decision_id: decided[decision_id]})[0], 409)
            now = get("/api/views")["links"]["decided"]
            self.assertEqual(decide([decision_id], "reconfirm", {decision_id: now[decision_id]})[1]["status"],
                             "RECONFIRMED")

            self.assertEqual(decide([other["id"]], "reject")[0], 200)  # scripts that send no fingerprints still work
            self.assertEqual(decide([refund["id"]], "undo", {"anything": "x"})[0], 200)  # undo needs no view
            self.assertEqual(decide([], "", raw={"ids": [refund["id"]], "decision": "confirm", "by": "Dana",
                                                 "note": "", "seen": ["not", "a", "map"]})[0], 400)
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(base + "/api/views?x=1", timeout=5)
            self.assertEqual(caught.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
