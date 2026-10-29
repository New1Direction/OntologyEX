"""Previewed batches, stale approvals refused, and auditable undo a batch at a time (decisions.py)."""
import json
import shutil
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
import decisions
import dependency_suggest as ds

import importlib.util
spec = importlib.util.spec_from_file_location("ontologyex_batches_demo", ROOT / "examples/operating-picture/demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)
VIEW = ROOT / "engine/target/debug/ontologyex-view"


def svc(name):
    return f"discovery:service:compose.yml:{name}"


def suggestion(source, target, calls=5):
    source, target = svc(source), svc(target)
    return {"status": "PROPOSED", "source": source, "target": target,
            "evidence": {"calls": calls, "errors": 0, "first_seen": "2026-10-01T00:00:00Z",
                         "last_seen": "2026-10-28T00:00:00Z", "caller_services": [source]}}


def key(source, target):
    return decisions.decision_id(*ds.dependency_key({"source": svc(source), "target": svc(target)}))


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "dependencies.json"

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def decide(self, by="Dana", **kwargs):
        return ds.apply_decisions(self.path, by, "", **kwargs)

    def state(self):
        confirmed, rejected = ds.read_dependency_document(self.path) if self.path.exists() else ([], [])
        short = lambda r: (r["source"].rsplit(":", 1)[1], r["target"].rsplit(":", 1)[1])
        return ({short(r) for r in confirmed}, {short(r) for r in rejected})

    def history(self):
        return [json.loads(line) for line in decisions.history_path(self.path).read_text().splitlines()]

    def test_a_preview_writes_nothing_and_the_plan_applies_only_unchanged(self):
        preview = self.decide(confirm=[suggestion("a", "b"), suggestion("c", "d")], preview=True)
        self.assertEqual(preview["status"], "PREVIEW")
        self.assertEqual([c["action"] for c in preview["changes"]], ["confirm", "confirm"])
        self.assertFalse(self.path.exists())
        # Someone else decides a -> b first: the approval was for a world where it was undecided.
        self.decide(by="Lee", reject=[suggestion("a", "b")])
        with self.assertRaises(decisions.StalePlan) as caught:
            self.decide(confirm=[suggestion("a", "b"), suggestion("c", "d")], plan_digests=preview["plan"])
        self.assertEqual(caught.exception.ids, sorted([key("a", "b")]))
        self.assertEqual(self.state(), (set(), {("a", "b")}))  # nothing of the stale batch was written
        # The same batch previewed now applies; what was shown is what is written.
        again = self.decide(confirm=[suggestion("c", "d")], preview=True)
        done = self.decide(confirm=[suggestion("c", "d")], plan_digests=again["plan"])
        self.assertEqual(done["status"], "CONFIRMED")
        self.assertEqual(self.state(), ({("c", "d")}, {("a", "b")}))

    def test_decisions_on_other_items_never_make_a_preview_stale(self):
        preview = self.decide(confirm=[suggestion("a", "b")], preview=True)
        self.decide(by="Lee", confirm=[suggestion("x", "y")])  # another item, in between
        self.assertEqual(self.decide(confirm=[suggestion("a", "b")], plan_digests=preview["plan"])["status"], "CONFIRMED")

    def test_an_evidence_change_makes_a_preview_stale(self):
        preview = self.decide(confirm=[suggestion("a", "b", calls=5)], preview=True)
        with self.assertRaises(decisions.StalePlan):
            self.decide(confirm=[suggestion("a", "b", calls=6)], plan_digests=preview["plan"])

    def test_an_undo_applies_only_to_the_version_the_person_saw(self):
        self.decide(confirm=[suggestion("a", "b")])
        seen = decisions.version({"list": "confirmed", "row": ds.read_dependency_document(self.path)[0][0]})
        self.decide(by="Lee", undo=[key("a", "b")])
        self.decide(by="Lee", reject=[suggestion("a", "b")])  # decided again, differently
        with self.assertRaises(decisions.Refused):
            self.decide(undo=[key("a", "b")], expect={key("a", "b"): seen})
        self.assertEqual(self.state(), (set(), {("a", "b")}))

    def test_undoing_a_batch_puts_back_exactly_its_items_and_keeps_later_decisions(self):
        self.decide(confirm=[suggestion("a", "b")])
        first = self.decide(confirm=[suggestion("c", "d")], reject=[suggestion("e", "f")], undo=[key("a", "b")])
        later = self.decide(by="Lee", confirm=[suggestion("x", "y")])
        self.assertEqual(self.state(), ({("c", "d"), ("x", "y")}, {("e", "f")}))
        preview = self.decide(by="Ana", undo_batch=first["batch"], preview=True)
        self.assertEqual(sorted(c["action"] for c in preview["changes"]), ["restore", "undo", "undo"])
        self.assertEqual(preview["conflicts"], [])
        undone = self.decide(by="Ana", undo_batch=first["batch"], plan_digests=preview["plan"])
        self.assertEqual(undone["status"], "UNDONE_BATCH")
        self.assertEqual(self.state(), ({("a", "b"), ("x", "y")}, set()))  # a -> b back; x -> y kept
        # The audit trail: who undid which batch, and it can itself be undone.
        events = [e for e in self.history() if e["batch"] == undone["batch"]]
        self.assertTrue(all(e["undoes"] == first["batch"] and e["by"] == "Ana" for e in events))
        listed = {b["batch"]: b for b in decisions.batches(self.path)}
        self.assertEqual(listed[first["batch"]]["undone_by"], undone["batch"])
        self.assertIsNone(listed[later["batch"]]["undone_by"])
        with self.assertRaises(ValueError):  # undone once only
            self.decide(undo_batch=first["batch"])
        self.decide(undo_batch=undone["batch"])  # redo
        self.assertEqual(self.state(), ({("c", "d"), ("x", "y")}, {("e", "f")}))

    def test_an_undo_is_refused_when_a_later_decision_changed_its_items(self):
        first = self.decide(confirm=[suggestion("a", "b"), suggestion("c", "d")])
        self.decide(by="Lee", undo=[key("c", "d")])
        preview = self.decide(undo_batch=first["batch"], preview=True)
        self.assertEqual([c["id"] for c in preview["conflicts"]], [key("c", "d")])
        self.assertEqual(preview["conflicts"][0]["by"], "Lee")
        with self.assertRaises(decisions.Refused) as caught:
            self.decide(undo_batch=first["batch"])
        self.assertEqual(caught.exception.ids, [key("c", "d")])
        self.assertEqual(self.state(), ({("a", "b")}, set()))  # Lee's undo of c -> d stands, a -> b untouched

    def test_undoing_a_reconfirm_restores_the_previous_row(self):
        links = self.tmp / "links.json"
        import link_suggest
        sug = {"status": "PROPOSED", "skill": "pay", "binding": "Payment", "component": "svc:api", "evidence": {"f": "1" * 64}}
        link_suggest.apply_decisions(links, "Dana", "", confirm=[sug])
        again = link_suggest.reconfirm(links, "Lee", "", [{**sug, "evidence": {"f": "2" * 64}}])
        link_suggest.apply_decisions(links, "Ana", "", undo_batch=again["batch"])
        import business_view
        row = business_view.read_link_document(links)[0][0]
        self.assertEqual((row["evidence"], row["declared_by"]), ({"f": "1" * 64}, "Dana"))  # not deleted: put back

    def test_kernel(self):
        a = {"list": "confirmed", "row": {"v": 1}}
        out = decisions.commit([{"id": "x", **a}], [{"id": "x", "expect": decisions.version(a), "set": None},
                                                    {"id": "y", "expect": None, "set": a}])
        self.assertEqual([e["id"] for e in out["entries"]], ["y"])
        with self.assertRaises(decisions.Refused) as caught:
            decisions.commit([], [{"id": "x", "expect": None, "set": a}, {"id": "x", "expect": None, "set": None}])
        self.assertEqual(caught.exception.reason, "DUPLICATE_ITEM")


class RouteTests(unittest.TestCase):
    """Through the control room: preview, apply what was previewed, undo a batch, list batches."""

    def test_preview_apply_and_undo_a_batch(self):
        self.flow([], None)

    @unittest.skipUnless(VIEW.is_file(), "the Rust engine is not built here")
    def test_the_same_through_the_rust_kernel(self):
        with tempfile.TemporaryDirectory() as tmp:
            # A wrapper that records each call, then runs the real binary: proof the kernel ran.
            engine, log = Path(tmp) / "engine", Path(tmp) / "calls.log"
            engine.mkdir()
            (engine / "ontologyex-view").write_text(f'#!/bin/sh\necho "$1" >> "{log}"\nexec "{VIEW}" "$@"\n')
            (engine / "ontologyex-view").chmod(0o755)
            (engine / "ontologyex-discover").write_text(f'#!/bin/sh\nexec "{VIEW.with_name("ontologyex-discover")}" "$@"\n')
            (engine / "ontologyex-discover").chmod(0o755)
            self.flow(["--view-engine", "rust"], {"ONTOLOGYEX_ENGINE_DIR": str(engine)})
            self.assertEqual(log.read_text().split().count("decide"), 3)  # the start-up check, the batch, its undo

    @unittest.skipUnless(VIEW.is_file(), "the Rust engine is not built here")
    def test_an_engine_without_the_kernel_leaves_decisions_to_python(self):
        with tempfile.TemporaryDirectory() as tmp:  # an ontologyex-view from before the decision kernel
            engine, log = Path(tmp) / "engine", Path(tmp) / "calls.log"
            engine.mkdir()
            (engine / "ontologyex-view").write_text(
                f'#!/bin/sh\necho "$1" >> "{log}"\n[ "$1" = decide ] && exit 2\nexec "{VIEW}" "$@"\n')
            (engine / "ontologyex-discover").write_text(f'#!/bin/sh\nexec "{VIEW.with_name("ontologyex-discover")}" "$@"\n')
            for f in engine.iterdir():
                f.chmod(0o755)
            self.flow(["--view-engine", "rust"], {"ONTOLOGYEX_ENGINE_DIR": str(engine)})
            self.assertEqual(log.read_text().split().count("decide"), 1)  # asked once at start, then Python decided

    def flow(self, extra, env):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            built = demo.build(Path(tmp) / "demo", unlinked=True)
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            process = subprocess.Popen([*built["command"], "--port", str(port), "--allow-link-decisions", *extra],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       env={**os.environ, **(env or {})})
            self.addCleanup(lambda: (process.terminate(), process.wait(timeout=5)))
            base = f"http://127.0.0.1:{port}"
            for _ in range(150):
                try:
                    urllib.request.urlopen(base + "/api/state", timeout=2).close()
                    break
                except OSError:
                    time.sleep(0.1)
            with urllib.request.urlopen(base + "/business", timeout=10) as resp:
                token = resp.read().decode().split('name="ontologyex-write-token" content="')[1].split('"')[0]

            def get(path):
                with urllib.request.urlopen(base + path, timeout=10) as resp:
                    return json.load(resp)

            def post(body):
                request = urllib.request.Request(base + "/api/links/decision", method="POST", data=json.dumps(body).encode(),
                                                 headers={"Content-Type": "application/json", "Origin": base,
                                                          "X-OntologyEX-Token": token})
                try:
                    with urllib.request.urlopen(request, timeout=10) as resp:
                        return resp.status, json.load(resp)
                except urllib.error.HTTPError as exc:
                    return exc.code, json.load(exc)

            ids = [s["id"] for s in get("/api/business")["link_suggestions"]["suggestions"]][:2]
            ask = {"ids": ids, "decision": "confirm", "by": "Dana", "note": ""}
            status, preview = post({**ask, "preview": True})
            self.assertEqual((status, preview["status"], len(preview["changes"])), (200, "PREVIEW", 2))
            self.assertFalse(Path(built["links"]).with_suffix(".history.jsonl").exists())
            status, done = post({**ask, "plan": {**preview["plan"], ids[0]: "0" * 24}})  # not what was previewed
            self.assertEqual((status, done["status"], done["changed"]), (409, "STALE_VIEW", [ids[0]]))
            status, done = post({**ask, "plan": preview["plan"]})
            self.assertEqual((status, done["status"]), (200, "CONFIRMED"))
            batches = get("/api/links/batches")["batches"]
            self.assertEqual((batches[0]["batch"], batches[0]["changes"], batches[0]["by"]), (done["batch"], 2, "Dana"))
            undo = {"batch": done["batch"], "decision": "undo_batch", "by": "Lee", "note": "wrong component"}
            status, preview = post({**undo, "preview": True})
            self.assertEqual((status, [c["action"] for c in preview["changes"]]), (200, ["undo", "undo"]))
            status, undone = post({**undo, "plan": preview["plan"]})
            self.assertEqual((status, undone["status"], undone["undoes"]), (200, "UNDONE_BATCH", done["batch"]))
            listed = {b["batch"]: b for b in get("/api/links/batches")["batches"]}
            self.assertEqual(listed[done["batch"]]["undone_by"], undone["batch"])
            self.assertEqual(get("/api/links/batches")["batches"][0]["batch"], undone["batch"])  # newest first
            self.assertEqual(post({**undo, "decision": "undo"})[0], 400)  # a batch is only undone by undo_batch
            self.assertEqual(post({**ask, "preview": "yes"})[0], 400)


if __name__ == "__main__":
    unittest.main()
