"""Decision watermarks: a confirmation records its evidence, and reads as stale once it changes."""
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
sys.path.insert(0, str(ROOT / "tests"))
import business_view as bv
import decisions
import entity_context as ec
import link_suggest as ls
import watermark
from policy_check import DomainError
from test_entity_context import API, PAY, bundle

spec = importlib.util.spec_from_file_location("ontologyex_watermark_demo", ROOT / "examples/operating-picture/demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)

A, B = "a" * 64, "b" * 64
REFUND = "payments-domain:action:IssueRefund"
WORKER = "discovery:service:compose.yml:worker"


class CheckTests(unittest.TestCase):
    def test_evidence_check(self):
        self.assertEqual(ec.evidence_check(None, {"f": A}), {"status": "NOT_RECORDED", "changed": []})
        self.assertEqual(ec.evidence_check({"f": A}, {"f": A}), {"status": "CURRENT", "changed": []})
        self.assertEqual(ec.evidence_check({"f": A}, {"f": B})["changed"], ["f"])
        self.assertEqual(ec.evidence_check({"f": A}, {})["changed"], ["f"])  # the file is gone
        self.assertEqual(ec.evidence_check({"f": A}, {"f": A, "g": B})["changed"], ["g"])  # new evidence appeared
        self.assertEqual(ec.watermark_standing({"f": A}, {"f": B}), "INFERRED")
        self.assertEqual(ec.watermark_standing(None, {}), "CONFIRMED")

    def mark(self, recorded, now):
        data = bundle()
        data["business"]["link_watermarks"] = [{"id": "d1", "skill": "s", "binding": "b.pay", "component": API,
                                                "recorded": recorded, "now": now}]
        return data

    def test_changed_evidence_is_no_longer_confirmed(self):
        rel = next(r for r in ec.context(self.mark({"compose.yml": A}, {"compose.yml": B}), PAY)["relationships"]
                   if r["other"]["id"] == API)
        self.assertEqual((rel["standing"], rel["basis"]), ("INFERRED", "CONFIRMED_BEFORE_EVIDENCE_CHANGED"))
        self.assertEqual(rel["evidence_check"], {"status": "CHANGED_SINCE_DECISION", "changed": ["compose.yml"],
                                                 "decision_id": "d1"})
        self.assertEqual(rel["decision"]["by"], "Ana")  # who decided is still shown
        found = ec.context(self.mark({"compose.yml": A}, {"compose.yml": B}), PAY)
        self.assertIn("NO_CONFIRMED_LINK", [u["basis"] for u in found["unknowns"]])
        self.assertEqual(ec.explain(self.mark({"f": A}, {"f": B}), PAY, API)["verdict"], "INFERRED")

    def test_current_and_unrecorded_stay_confirmed(self):
        for recorded, status in (({"f": A}, "CURRENT"), (None, "NOT_RECORDED")):
            rel = next(r for r in ec.context(self.mark(recorded, {"f": A}), PAY)["relationships"]
                       if r["other"]["id"] == API)
            self.assertEqual((rel["standing"], rel["evidence_check"]["status"]), ("CONFIRMED", status))

    def test_stale_decisions_are_listed_for_agents(self):
        import agent_tools as at
        data = self.mark({"compose.yml": A}, {"compose.yml": B})
        found = ec.stale_decisions(data)
        self.assertEqual([(r["decision_id"], r["component"]["id"], r["changed"]) for r in found["stale"]],
                         [("d1", API, ["compose.yml"])])
        self.assertEqual(ec.stale_decisions(self.mark({"f": A}, {"f": A}))["stale"], [])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bundle.json"
            path.write_text(json.dumps(data))
            tools = at.Tools(at.Source(None, path), at.load_policy(None), at.Audit(Path(tmp) / "a.jsonl", "c", "D"), None)
            self.assertEqual(tools.call("list_stale_decisions", {})["total"], 1)
            secret = self.mark({".env": A}, {".env": B})
            path.write_text(json.dumps(secret))
            self.assertNotIn(".env", json.dumps(tools.call("list_stale_decisions", {})))
            self.assertNotIn(".env", json.dumps(tools.call("get_entity_context", {"id": PAY})))
            self.assertTrue(at.annotations("list_stale_decisions")["readOnlyHint"])

    def test_reconfirm_events_name_the_person(self):
        data = self.mark({"f": A}, {"f": A})
        data["link_history"].append({"at": "2026-10-29T10:00:00Z", "by": "Bo", "action": "reconfirm", "note": "still",
                                     "row": {"skill": "s", "binding": "b.pay", "component": API}})
        rel = next(r for r in ec.context(data, PAY)["relationships"] if r["other"]["id"] == API)
        self.assertEqual(rel["decision"]["by"], "Bo")


class DocumentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ontologyex-watermark-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.links = self.tmp / "links.json"

    def write(self, rows):
        self.links.write_text(json.dumps({"schema_version": 1, "kind": bv.LINK_KIND, "links": rows}))

    def test_evidence_is_optional_and_validated(self):
        row = {"skill": "s", "binding": "b", "component": "c", "declared_by": "Ana", "note": ""}
        self.write([row, {**row, "component": "d", "evidence": {"compose.yml": A}}])
        self.assertEqual(len(bv.read_link_document(self.links)[0]), 2)
        for bad in ({"compose.yml": "nothex"}, {"": A}, ["compose.yml"], {f"f{i}": A for i in range(17)}):
            self.write([{**row, "evidence": bad}])
            with self.assertRaises(DomainError):
                bv.read_link_document(self.links)
        self.write([{**row, "extra": 1}])
        with self.assertRaises(DomainError):
            bv.read_link_document(self.links)

    def test_reconfirm_replaces_the_row_and_logs_it(self):
        suggestion = {"skill": "s", "binding": "b", "component": "c", "status": "PROPOSED", "evidence": {"f": A}}
        ls.apply_decisions(self.links, "Ana", "first", confirm=[suggestion])
        self.assertEqual(bv.read_link_document(self.links)[0][0]["evidence"], {"f": A})
        result = ls.reconfirm(self.links, "Bo", "checked again",
                              [{"skill": "s", "binding": "b", "component": "c", "evidence": {"f": B}}])
        self.assertEqual(result["status"], "RECONFIRMED")
        row = bv.read_link_document(self.links)[0][0]
        self.assertEqual((row["evidence"], row["declared_by"], row["note"]), ({"f": B}, "Bo", "checked again"))
        events = [json.loads(line) for line in decisions.history_path(self.links).read_text().splitlines()]
        self.assertEqual([e["action"] for e in events], ["confirm", "reconfirm"])
        with self.assertRaises(DomainError):  # only a confirmed link can be re-confirmed
            ls.reconfirm(self.links, "Bo", "", [{"skill": "s", "binding": "b", "component": "zz", "evidence": {}}])

    def test_rejections_carry_no_watermark(self):
        ls.apply_decisions(self.links, "Ana", "", reject=[{"skill": "s", "binding": "b", "component": "c",
                                                           "status": "PROPOSED", "evidence": {"f": A}}])
        self.assertNotIn("evidence", bv.read_link_document(self.links)[1][0])


class RepoFilesTests(unittest.TestCase):
    def test_paths_resolve_exactly_or_by_unique_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "domain").mkdir()
            (repo / "domain/payments.py").write_text("x = 1\n")
            (repo / "a").mkdir()
            (repo / "b").mkdir()
            (repo / "a/util.py").write_text("")
            (repo / "b/util.py").write_text("")
            (repo / "node_modules").mkdir()
            (repo / "node_modules/payments.py").write_text("")
            files = watermark.RepoFiles(repo, ttl=0)
            self.assertEqual(files.find("payments.py"), "domain/payments.py")
            self.assertEqual(files.find("domain/payments.py"), "domain/payments.py")
            self.assertIsNone(files.find("util.py"))  # ambiguous: never guessed
            self.assertIsNone(files.find("missing.py"))
            self.assertEqual(len(files.sha256("domain/payments.py")), 64)


class EndToEndTests(unittest.TestCase):
    """Confirm through the page's route, change the cited code, see it reopen, re-confirm."""

    def test_confirm_change_reopen_reconfirm(self):
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

            def decide(ids, decision):
                request = urllib.request.Request(base + "/api/links/decision", method="POST", data=json.dumps(
                    {"ids": ids, "decision": decision, "by": "Dana", "note": ""}).encode(),
                    headers={"Content-Type": "application/json", "Origin": base, "X-OntologyEX-Token": token})
                try:
                    with urllib.request.urlopen(request, timeout=10) as resp:
                        return resp.status, json.load(resp)
                except urllib.error.HTTPError as exc:
                    return exc.code, json.load(exc)

            refund = next(s for s in get("/api/business")["link_suggestions"]["suggestions"]
                          if s["binding"] == "payments.apply_refund")
            self.assertEqual(decide([refund["id"]], "confirm")[0], 200)
            row = next(r for r in bv.read_link_document(Path(built["links"]))[0] if r["binding"] == "payments.apply_refund")
            self.assertEqual(sorted(row["evidence"]), ["compose.yml", "domain/payments.py",
                                                       "skill:payments-domain/payments.py"])

            def link():
                found = get("/api/context?id=" + REFUND)
                return next(r for r in found["relationships"]
                            if r.get("via") == "payments-domain:binding:payments.apply_refund"
                            and r["other"]["id"] == refund["component"])
            self.assertEqual((link()["standing"], link()["evidence_check"]["status"]), ("CONFIRMED", "CURRENT"))

            code = Path(built["repo"]) / "domain/payments.py"
            code.write_text(code.read_text() + "\n# refunds now go through the worker\n")
            time.sleep(0.05)
            stale = link()
            self.assertEqual((stale["standing"], stale["basis"]), ("INFERRED", "CONFIRMED_BEFORE_EVIDENCE_CHANGED"))
            self.assertEqual(stale["evidence_check"]["changed"], ["domain/payments.py"])
            decided = next(r for r in get("/api/business")["link_decided"] if r["id"] == refund["id"])
            self.assertEqual(decided["evidence_check"], "CHANGED_SINCE_DECISION")

            self.assertEqual(decide(["not-a-link"], "reconfirm")[0], 400)
            status, body = decide([stale["evidence_check"]["decision_id"]], "reconfirm")
            self.assertEqual((status, body["status"]), (200, "RECONFIRMED"))
            again = link()
            self.assertEqual((again["standing"], again["evidence_check"]["status"], again["decision"]["by"]),
                             ("CONFIRMED", "CURRENT", "Dana"))
            dependency = urllib.request.Request(base + "/api/dependencies/decision", method="POST", data=json.dumps(
                {"ids": ["x"], "decision": "reconfirm", "by": "Dana", "note": ""}).encode(),
                headers={"Content-Type": "application/json", "Origin": base, "X-OntologyEX-Token": token})
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(dependency, timeout=5)


if __name__ == "__main__":
    unittest.main()
