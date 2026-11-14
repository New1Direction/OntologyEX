"""The first look remains useful and honest before enrichment has finished."""
import copy
import hashlib
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "ontology-extraction/scripts"
sys.path.insert(0, str(SCRIPTS))
import code_surface
import control_room
import discover_repo
import first_look
import suppliers


class FirstLookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "shop"
        shutil.copytree(ROOT / "tests/golden/repos/shop", self.repo)
        self.manifests = discover_repo.discover(self.repo)
        surface = code_surface.scan(self.repo, engine=None)
        topology = code_surface.attach(self.manifests, self.repo, surface=surface)
        topology = suppliers.with_suppliers(topology, suppliers.scan(self.repo, topology["nodes"]), self.repo)
        self.state = control_room.build_state(topology, [], {})

    def test_manifest_preview_is_useful_while_other_scans_are_pending(self):
        state = control_room.build_state(self.manifests, [], {})
        view = first_look.project(state, {"code": {"status": "PENDING"}, "suppliers": {"status": "PENDING"}})
        groups = {g["id"]: g for g in view["groups"]}
        self.assertTrue(any(x["label"] == "api" for x in groups["systems"]["items"]))
        self.assertEqual(groups["routes"]["scan_status"], "PENDING")
        self.assertEqual(groups["providers"]["scan_status"], "PENDING")
        self.assertEqual(view["observed_nodes"], 0)
        self.assertFalse(view["execution_authorized"])

    def test_enrichment_is_bounded_and_points_to_its_sources(self):
        view = first_look.project(self.state, {"code": {"status": "READY"}, "suppliers": {"status": "READY"}})
        groups = {g["id"]: g for g in view["groups"]}
        for name, item in (("routes", "POST /api/checkout"), ("data", "Order"), ("providers", "Stripe")):
            row = next(row for row in groups[name]["items"] if row["label"] == item)
            self.assertIsNotNone(row["source"]["path"])
            self.assertGreater(row["source"]["line"], 0)
            self.assertEqual(len(row["source"]["sha256"]), 64)
        # A preview never silently treats an incomplete or failed scan as no findings.
        view = first_look.project(self.state, {"code": {"status": "ERROR", "error": "source unreadable"},
                                               "suppliers": {"status": "STALE"}})
        groups = {g["id"]: g for g in view["groups"]}
        self.assertEqual((groups["routes"]["scan_status"], groups["routes"]["scan_error"]),
                         ("ERROR", "source unreadable"))
        self.assertEqual(groups["providers"]["scan_status"], "STALE")

    def test_large_groups_report_how_many_items_are_omitted(self):
        state = copy.deepcopy(self.state)
        sample = next(n for n in state["nodes"] if n["kind"] == "api_route")
        for i in range(20):
            state["nodes"].append({**sample, "id": f"example-route-{i}", "label": f"GET /extra-{i:02}"})
        view = first_look.project(state, {"code": {"status": "READY"}, "suppliers": {"status": "READY"}})
        routes = next(g for g in view["groups"] if g["id"] == "routes")
        self.assertEqual(len(routes["items"]), first_look.MAX_PER_GROUP)
        self.assertEqual(routes["omitted"], routes["total"] - first_look.MAX_PER_GROUP)
        self.assertGreater(routes["omitted"], 0)

    def test_source_is_bound_to_the_shown_revision(self):
        node = next(n for n in self.state["nodes"] if n["kind"] == "api_route" and "checkout" in n["label"])
        ev = next(e for e in self.state["source_evidence"] if e["id"] == node["evidence_id"])
        result = first_look.source(self.repo, self.state, node["id"], ev["sha256"])
        self.assertEqual(result["status"], "SOURCE_LINE")
        self.assertIn("checkout", result["text"])
        path = self.repo / ev["path"]
        path.write_text(path.read_text() + "\n// changed\n")
        self.assertEqual(first_look.source(self.repo, self.state, node["id"], ev["sha256"])["status"],
                         "SOURCE_CHANGED")
        changed = copy.deepcopy(self.state)
        record = next(e for e in changed["source_evidence"] if e["id"] == node["evidence_id"])
        record["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(first_look.source(self.repo, changed, node["id"], ev["sha256"])["status"],
                         "SOURCE_CHANGED")

    def test_sensitive_and_unavailable_source_is_not_exposed(self):
        node = next(n for n in self.state["nodes"] if n["kind"] == "api_route")
        state = copy.deepcopy(self.state)
        selected = next(n for n in state["nodes"] if n["id"] == node["id"])
        ev = next(e for e in state["source_evidence"] if e["id"] == node["evidence_id"])
        path = self.repo / "snippet.py"
        path.write_text('app.get("/demo", token="not-for-display")\n')
        ev.update(path="snippet.py", sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        selected["source_line"] = 1
        result = first_look.source(self.repo, state, node["id"], ev["sha256"])
        self.assertEqual(result["status"], "WITHHELD_SENSITIVE_LINE")
        self.assertNotIn("not-for-display", json.dumps(result))
        ev["path"] = ".env.example"
        self.assertEqual(first_look.source(self.repo, state, node["id"], ev["sha256"])["status"],
                         "WITHHELD_SECRET_FILE")
        ev["path"] = "../outside.txt"
        self.assertEqual(first_look.source(self.repo, state, node["id"], ev["sha256"])["status"],
                         "SOURCE_UNAVAILABLE")
        with self.assertRaises(first_look.FirstLookError):
            first_look.source(self.repo, state, "unknown", ev["sha256"])

    def test_symlink_replacement_and_unrecorded_lines(self):
        node = next(n for n in self.state["nodes"] if n["kind"] == "api_route")
        ev = next(e for e in self.state["source_evidence"] if e["id"] == node["evidence_id"])
        path = self.repo / ev["path"]
        original = path.read_bytes()
        outside = Path(self.temp.name) / "outside.py"
        outside.write_bytes(original)
        path.unlink()
        path.symlink_to(outside)
        self.assertEqual(first_look.source(self.repo, self.state, node["id"], ev["sha256"])["status"],
                         "SOURCE_UNAVAILABLE")
        manifest = next(n for n in self.state["nodes"] if n["kind"] == "source_manifest")
        source = next(e for e in self.state["source_evidence"] if e["id"] == manifest["evidence_id"])
        self.assertEqual(first_look.source(self.repo, self.state, manifest["id"], source["sha256"])["status"],
                         "LINE_NOT_RECORDED")


class FirstLookHttpTests(unittest.TestCase):
    def test_preview_is_available_before_slow_enrichment(self):
        # Block the background code scan; the first look should still serve manifests.
        with tempfile.TemporaryDirectory() as tmp, socket.socket() as sock:
            repo = Path(tmp) / "shop"
            shutil.copytree(ROOT / "tests/golden/repos/shop", repo)
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            sock.close()
            script = ("import sys,time; from pathlib import Path; "
                      f"sys.path.insert(0,{str(SCRIPTS)!r}); "
                      "import code_surface,control_room; original=code_surface.scan; "
                      "code_surface.scan=lambda *a,**k:(time.sleep(4),original(*a,**k))[1]; "
                      "control_room.serve(Path(sys.argv[1]),None,None,int(sys.argv[2]))")
            process = subprocess.Popen([sys.executable, "-c", script, str(repo), str(port)],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.addCleanup(lambda: process.poll() is None and process.kill())
            base = f"http://127.0.0.1:{port}"
            try:
                deadline = time.monotonic() + 8
                while True:
                    try:
                        with urllib.request.urlopen(base + "/api/first-look", timeout=1) as response:
                            view = json.load(response)
                        break
                    except OSError:
                        if time.monotonic() > deadline:
                            self.fail("first look did not start")
                        time.sleep(0.05)
                groups = {g["id"]: g for g in view["groups"]}
                self.assertEqual(groups["routes"]["scan_status"], "PENDING")
                self.assertTrue(any(x["label"] == "api" for x in groups["systems"]["items"]))
                self.assertFalse(view["execution_authorized"])
                with self.assertRaises(urllib.error.HTTPError) as invalid:
                    urllib.request.urlopen(base + "/api/first-look/source?path=../secret", timeout=2)
                self.assertEqual(invalid.exception.code, 400)
                deadline = time.monotonic() + 15
                while groups["routes"]["scan_status"] != "READY" and time.monotonic() < deadline:
                    time.sleep(0.1)
                    with urllib.request.urlopen(base + "/api/first-look", timeout=2) as response:
                        view = json.load(response)
                    groups = {g["id"]: g for g in view["groups"]}
                self.assertEqual(groups["routes"]["scan_status"], "READY")
                row = next(x for x in groups["routes"]["items"] if "checkout" in x["label"])
                params = urllib.parse.urlencode({"id": row["id"], "sha256": row["source"]["sha256"]})
                with urllib.request.urlopen(base + "/api/first-look/source?" + params, timeout=2) as response:
                    source = json.load(response)
                self.assertEqual(source["status"], "SOURCE_LINE")
                self.assertIn("checkout", source["text"])
            finally:
                process.terminate()
                process.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
