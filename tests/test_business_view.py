"""Business operating picture: verified skills joined to control-room state, read-only."""
import copy
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
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ontology-extraction/scripts"))
import business_view as bv
import control_room as room
import discover_repo
from policy_check import DomainError

spec = importlib.util.spec_from_file_location(
    "ontologyex_operating_picture_demo", ROOT / "examples/operating-picture/demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)


class BusinessViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.temp.name) / "demo"
        cls.built = demo.build(cls.out, NOW)
        cls.skill = Path(cls.built["skill"])
        cls.links = bv.read_links(Path(cls.built["links"]))
        cls.events = Path(cls.built["events"])

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def state(self, rows=None):
        rows = room.read_observations(Path(self.built["observations"])) if rows is None else rows
        return room.build_state(discover_repo.discover(Path(self.built["repo"])), rows, {}, NOW)

    def picture(self, rows=None, links=None, events="default"):
        return bv.project([self.skill], self.state(rows), self.links if links is None else links,
                          self.events if events == "default" else events)

    def by_id(self, items):
        return {item["id"]: item for item in items}

    def test_action_health_rolls_up_linked_systems(self):
        result = self.picture()
        actions = self.by_id(result["actions"])
        self.assertEqual(actions["IssueRefund"]["health"], "AT_RISK")
        self.assertIn("payments reports error", actions["IssueRefund"]["health_reason"])
        # api reports ok but declares a dependency on the failing payments service.
        self.assertEqual(actions["ApproveException"]["health"], "POSSIBLY_IMPACTED")
        self.assertIn("payments", actions["ApproveException"]["health_reason"])
        self.assertEqual(result["summary"]["action_health"]["AT_RISK"], 1)
        self.assertFalse(result["execution_authorized"])

    def test_no_observations_is_unknown_not_ok(self):
        result = self.picture(rows=[])
        for action in result["actions"]:
            self.assertEqual(action["health"], "UNKNOWN")
        self.assertEqual(result["summary"]["action_health"]["REPORTED_OK"], 0)

    def test_all_ok_is_only_reported_ok(self):
        rows = [dict(r, status="ok", metrics={})
                for r in room.read_observations(Path(self.built["observations"]))]
        actions = self.by_id(self.picture(rows=rows)["actions"])
        self.assertEqual(actions["IssueRefund"]["health"], "REPORTED_OK")
        self.assertIn("not independently attested", actions["IssueRefund"]["health_reason"])

    def test_without_links_everything_is_unlinked(self):
        result = self.picture(links=[])
        self.assertEqual({a["health"] for a in result["actions"]}, {"UNLINKED"})
        self.assertEqual(result["systems"], [])

    def test_objects_follow_their_bindings(self):
        objects = self.by_id(self.picture()["objects"])
        self.assertEqual(objects["Refund"]["health"], "AT_RISK")
        self.assertEqual(objects["MonetaryAmount"]["health"], "UNLINKED")
        self.assertIn({"action": "payments-domain:action:IssueRefund", "label": "IssueRefund",
                       "role": "produces"}, objects["Refund"]["actions"])

    def test_review_blockers_and_grades_are_reported(self):
        result = self.picture()
        actions = self.by_id(result["actions"])
        approve = actions["ApproveException"]
        self.assertEqual(approve["readiness"], "REVIEW_BLOCKED")
        self.assertEqual(approve["blockers"][0]["type"], "unknown")
        refund = actions["IssueRefund"]
        self.assertEqual(refund["readiness"], "MODELED_NOT_APPROVED")
        grades = {r["id"]: r["grade"] for r in refund["rules"]}
        self.assertEqual(grades["remaining-balance"], "G3")
        self.assertEqual(grades["positive-amount"], "G1")
        self.assertEqual(result["summary"]["rules_reviewed_or_better"], 2)
        self.assertEqual(self.by_id(self.picture(events=None)["actions"])["IssueRefund"]["grade_distribution"]["G0"], 7)

    def test_attention_is_ordered_by_urgency(self):
        severities = [a["severity"] for a in self.picture()["attention"]]
        order = ["AT_RISK", "POSSIBLY_IMPACTED", "DEGRADED", "STALE", "REVIEW_BLOCKED", "UNLINKED"]
        self.assertEqual(severities, sorted(severities, key=order.index))
        self.assertEqual(severities[0], "AT_RISK")
        self.assertIn("REVIEW_BLOCKED", severities)

    def test_bad_links_become_gaps_not_edges(self):
        links = copy.deepcopy(self.links)
        links.append({**links[0], "component": "discovery:service:compose.yml:absent"})
        links.append({**links[0], "binding": "payments.absent"})
        links.append({**links[0], "skill": "other-skill"})
        result = self.picture(links=links)
        self.assertEqual(sorted(g["reason"] for g in result["link_gaps"]), [
            "LINK_BINDING_NOT_IN_SKILL", "LINK_COMPONENT_NOT_DISCOVERED", "LINK_SKILL_NOT_LOADED"])
        targets = {e["target"] for e in result["graph"]["edges"]}
        self.assertNotIn("discovery:service:compose.yml:absent", targets)

    def test_evidence_quotes_are_exact_and_bounded(self):
        refund = self.by_id(self.picture()["actions"])["IssueRefund"]
        rule = next(r for r in refund["rules"] if r["id"] == "remaining-balance")
        self.assertEqual({e["kind"] for e in rule["evidence"]}, {"requirement", "implementation"})
        for item in rule["evidence"]:
            self.assertLessEqual(len(item["quote"]), bv.MAX_QUOTE)

    def test_links_file_validation(self):
        path = Path(self.temp.name) / "links-bad.json"
        good = json.loads(Path(self.built["links"]).read_text())
        for mutate in (lambda d: d["links"][0].pop("declared_by"),
                       lambda d: d["links"][0].update(extra=1),
                       lambda d: d.update(kind="OTHER"),
                       lambda d: d["links"].append(dict(d["links"][0]))):
            data = copy.deepcopy(good)
            mutate(data)
            path.write_text(json.dumps(data))
            with self.assertRaises(DomainError):
                bv.read_links(path)

    def test_tampered_skill_is_refused(self):
        import shutil
        forged = Path(self.temp.name) / "forged"
        shutil.copytree(self.skill, forged)
        with (forged / "model.json").open("a") as handle:
            handle.write("\n")
        with self.assertRaises(DomainError):
            bv.project([forged], self.state(), [], None)

    def test_duplicate_skill_names_are_refused(self):
        with self.assertRaises(DomainError):
            bv.project([self.skill, self.skill], self.state(), [], None)

    def test_no_skills_is_an_explicit_status(self):
        result = bv.project([], self.state(), [], None)
        self.assertEqual(result["status"], "NO_DOMAIN_SKILLS")
        self.assertEqual(result["actions"], [])


class BusinessHttpTests(unittest.TestCase):
    def test_business_page_and_api_are_served_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            built = demo.build(Path(tmp) / "demo")
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            process = subprocess.Popen([*built["command"], "--port", str(port)],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                base = f"http://127.0.0.1:{port}"
                for _ in range(100):
                    try:
                        with urllib.request.urlopen(base + "/api/business", timeout=2) as resp:
                            data = json.load(resp)
                        break
                    except OSError:
                        time.sleep(0.1)
                else:
                    self.fail("control room did not start")
                self.assertEqual(data["kind"], "BUSINESS_OPERATING_PICTURE")
                self.assertEqual(data["status"], "READY")
                self.assertEqual(data["discovery_freshness"], "AS_OF_LATEST_SUCCESSFUL_SCAN")
                with urllib.request.urlopen(base + "/business", timeout=2) as resp:
                    page = resp.read().decode()
                    self.assertIn("default-src 'none'", resp.headers["Content-Security-Policy"])
                self.assertIn("Operating Picture", page)
                self.assertNotIn("innerHTML", page)
                self.assertNotIn("https://", page.split("<script>")[1])
                with self.assertRaises(urllib.error.HTTPError) as bad:
                    urllib.request.urlopen(base + "/api/business?x=1", timeout=2)
                self.assertEqual(bad.exception.code, 400)
                # No Origin: refused. Same origin: the read route has no write handler.
                for headers, expected in (({}, 403), ({"Origin": base}, 404)):
                    request = urllib.request.Request(base + "/api/business", method="POST",
                                                     data=b"{}", headers=headers)
                    with self.assertRaises(urllib.error.HTTPError) as post:
                        urllib.request.urlopen(request, timeout=2)
                    self.assertEqual(post.exception.code, expected)
            finally:
                process.terminate()
                process.wait(timeout=5)

    def test_systems_page_links_to_business_view(self):
        self.assertIn('href="/business"', room.PAGE)


if __name__ == "__main__":
    unittest.main()
