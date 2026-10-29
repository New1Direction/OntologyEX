"""The weekly digest: the ranking now against the start of the window, decisions made, and code shipped."""
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
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
import agent_tools as at
import digest as dg
from test_entity_context import API, DB, PAY, bundle

spec = importlib.util.spec_from_file_location("ontologyex_digest_demo", ROOT / "examples/operating-picture/demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)


def item(item_id, score, title=None, level=None):
    return {"id": item_id, "kind": "AT_RISK", "category": "Business at risk", "score": score,
            "level": level or dg.command_center._level(score), "title": title or item_id,
            "subject": {"id": f"s:{item_id}", "label": item_id}, "links": [f"s:{item_id}"], "next_step": "look"}


def report(items, ok=0, risk=0, linked=(1, 2)):
    return {"items": items,
            "kpis": [{"label": "Reported OK", "value": ok, "tone": "ok"},
                     {"label": "At risk or impacted", "value": risk, "tone": "risk"},
                     {"label": "Suppliers", "value": 3, "tone": "neutral"}],
            "coverage": {"rows": [{"label": "Business actions linked to a system", "part": linked[0],
                                   "whole": linked[1]}]},
            "field": {"field": None}}


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ontologyex-digest-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.history = self.tmp / "history.jsonl"


class RecordTests(Harness):
    def test_appends_on_change_or_once_a_day(self):
        first = report([item("a", 80)])
        self.assertTrue(dg.record(self.history, first, NOW))
        self.assertFalse(dg.record(self.history, first, NOW + timedelta(hours=3)))  # nothing moved
        self.assertTrue(dg.record(self.history, report([item("a", 81)]), NOW + timedelta(hours=4)))  # moved
        self.assertTrue(dg.record(self.history, report([item("a", 81)]), NOW + timedelta(hours=29)))  # a day passed
        self.assertEqual(len(dg.read_history(self.history)), 3)

    def test_torn_and_foreign_lines_are_skipped(self):
        dg.record(self.history, report([item("a", 80)]), NOW)
        with self.history.open("a") as handle:
            handle.write('{"kind":"SOMETHING_ELSE","at":"2026-10-29T12:00:00Z","items":[]}\n{"kind":"ONTOLOGYEX_COMM')
        self.assertEqual(len(dg.read_history(self.history)), 1)

    def test_a_symlinked_history_is_refused(self):
        (self.tmp / "real.jsonl").write_text("")
        self.history.symlink_to(self.tmp / "real.jsonl")
        with self.assertRaises(dg.DomainError):
            dg.record(self.history, report([]), NOW)


class BuildTests(Harness):
    def built(self, days=7, logs=None):
        week_ago = NOW - timedelta(days=8)
        dg.record(self.history, report([item("fixed", 72), item("worse", 60), item("better", 75), item("same", 40)],
                                       ok=1, risk=3, linked=(1, 2)), week_ago)
        now = report([item("new", 90), item("worse", 70), item("better", 55), item("same", 40)], ok=2, risk=2,
                     linked=(2, 2))
        return dg.build(dg.read_history(self.history), now, NOW, days, logs)

    def test_every_item_is_in_exactly_one_group(self):
        d = self.built()
        ids = {k: {r["id"] for r in d[k]} for k in ("new", "worse", "better", "still_open", "fixed")}
        self.assertEqual(ids, {"new": {"new"}, "worse": {"worse"}, "better": {"better"}, "still_open": {"same"},
                               "fixed": {"fixed"}})
        before, now = {"fixed", "worse", "better", "same"}, {"new", "worse", "better", "same"}
        self.assertEqual(ids["fixed"] | ids["worse"] | ids["better"] | ids["still_open"], before)
        self.assertEqual(ids["new"] | ids["worse"] | ids["better"] | ids["still_open"], now)
        self.assertEqual(sum(map(len, ids.values())), len(before | now))  # disjoint
        self.assertEqual((d["worse"][0]["score_before"], d["better"][0]["level_before"]), (60, "CRITICAL"))
        self.assertEqual(d["status"], "OK")
        self.assertFalse(d["execution_authorized"])

    def test_counts_and_coverage_move_with_a_direction(self):
        d = self.built()
        kpis = {k["label"]: k for k in d["kpis"]}
        self.assertEqual((kpis["Reported OK"]["delta"], kpis["Reported OK"]["better_when"]), (1, "up"))
        self.assertEqual((kpis["At risk or impacted"]["delta"], kpis["At risk or impacted"]["better_when"]), (-1, "down"))
        self.assertIsNone(kpis["Suppliers"]["better_when"])
        self.assertEqual(d["coverage"][0]["delta"], 50)

    def test_open_since_restarts_when_an_item_went_away(self):
        dg.record(self.history, report([item("a", 80)]), NOW - timedelta(days=9))
        dg.record(self.history, report([]), NOW - timedelta(days=5))
        dg.record(self.history, report([item("a", 80)]), NOW - timedelta(days=2))
        d = dg.build(dg.read_history(self.history), report([item("a", 80)]), NOW, 7)
        self.assertEqual([r["id"] for r in d["still_open"]], ["a"])  # there when the window began, and now
        self.assertEqual((d["still_open"][0]["open_days"], d["still_open"][0]["reopened"]), (2, True))
        self.assertIn("1 fixed and back again", d["headline"])
        self.assertIn("## Fixed, then back", dg.markdown(d))

    def test_long_open_names_the_oldest_critical(self):
        dg.record(self.history, report([item("old", 90, "payments reports an error")]), NOW - timedelta(days=20))
        d = dg.build(dg.read_history(self.history), report([item("old", 90, "payments reports an error")]), NOW, 7)
        self.assertEqual(d["long_open"][0]["open_days"], 20)
        self.assertIn("Longest open: payments reports an error (critical, 20 days)", d["headline"])

    def test_decisions_in_the_window_only_with_names(self):
        logs = {"link": [{"at": "2026-10-28T09:00:00Z", "by": "Ana", "action": "confirm", "note": "handler",
                          "row": {"binding": "b.pay", "component": API}},
                         {"at": "2026-10-01T09:00:00Z", "by": "Ana", "action": "reject", "row": {}}],
                "dependency": [{"at": "2026-10-27T09:00:00Z", "by": "Bo", "action": "undo",
                                "row": {"source": API, "target": DB}}]}
        d = dg.build([], report([]), NOW, 7, logs, names={API: "api", DB: "db"})
        self.assertEqual([(x["by"], x["action"], x["what"]) for x in d["decisions"]],
                         [("Ana", "confirm", "b.pay runs on api"), ("Bo", "undo", "api depends on db")])
        self.assertEqual(d["decided_by"], {"Ana": 1, "Bo": 1})
        self.assertEqual(d["decisions"][1]["about"], [API, DB])
        text = dg.markdown(d)
        self.assertIn("Ana confirmed a link: b.pay runs on api", text)
        self.assertIn("Bo undid a dependency: api depends on db", text)

    def test_no_history_and_history_starting_in_the_window(self):
        d = dg.build([], report([item("a", 80)]), NOW, 7)
        self.assertEqual((d["status"], [r["id"] for r in d["still_open"]], d["counts"]["new"]),
                         ("NO_HISTORY", ["a"], 0))
        self.assertIn("No history yet", d["headline"])
        dg.record(self.history, report([item("a", 80)]), NOW - timedelta(days=2))
        d = dg.build(dg.read_history(self.history), report([item("a", 80), item("b", 30)]), NOW, 7)
        self.assertEqual((d["status"], d["baseline_at"][:10]), ("HISTORY_STARTS_IN_WINDOW", "2026-10-27"))
        self.assertEqual([r["id"] for r in d["new"]], ["b"])

    def test_same_inputs_same_digest(self):
        self.assertEqual(json.dumps(self.built(), sort_keys=True), json.dumps(self.built(), sort_keys=True))


class CodeTests(Harness):
    def git(self, *args, when=None):
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
               "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        if when:
            env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when
        subprocess.run(["git", "-C", str(self.tmp), *args], check=True, capture_output=True, env=env)

    def test_commits_in_the_window_and_the_business_they_touched(self):
        self.git("init", "-q", "-b", "main")
        (self.tmp / "req.md").write_text("Customers pay by card.\n")
        self.git("add", "req.md")
        self.git("commit", "-q", "-m", "requirements", when=(datetime.now(timezone.utc) - timedelta(days=10)).isoformat())
        (self.tmp / "req.md").write_text("Customers pay by card or bank transfer.\n")
        self.git("commit", "-q", "-am", "accept bank transfers")
        code = dg._code(self.tmp, bundle(), datetime.now(timezone.utc) - timedelta(days=7))
        self.assertEqual((code["status"], code["commits_total"], code["commits"][0]["subject"]),
                         ("OK", 1, "accept bank transfers"))
        self.assertIn(PAY, [t["id"] for t in code["touched_business"]])  # Pay's skill cites req.md
        self.assertEqual(dg._code(self.tmp, bundle(), datetime.now(timezone.utc) - timedelta(days=30))["status"],
                         "HISTORY_STARTS_IN_WINDOW")


class ServedTests(unittest.TestCase):
    def test_recorded_by_the_control_room_and_served(self):
        with tempfile.TemporaryDirectory() as tmp:
            built = demo.build(Path(tmp) / "demo")
            history = Path(tmp) / "history.jsonl"
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            process = subprocess.Popen([*built["command"], "--port", str(port), "--history", str(history),
                                        "--history-seconds", "10"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.addCleanup(lambda: (process.terminate(), process.wait(timeout=5)))
            base = f"http://127.0.0.1:{port}"
            for _ in range(300):
                if history.is_file() and history.read_text().strip():
                    break
                time.sleep(0.1)
            self.assertEqual(len(dg.read_history(history)), 1)  # recorded at start, once
            with urllib.request.urlopen(base + "/api/digest?days=7", timeout=60) as resp:
                found = json.load(resp)
            self.assertEqual((found["kind"], found["status"], found["recording"]),
                             ("WEEKLY_DIGEST", "HISTORY_STARTS_IN_WINDOW", "history.jsonl"))
            self.assertIn("payments reports an error", [r["title"] for r in found["still_open"]])
            with urllib.request.urlopen(base + "/api/digest?days=30&format=markdown", timeout=60) as resp:
                self.assertTrue(resp.headers["Content-Type"].startswith("text/markdown"))
                self.assertTrue(resp.read().decode().startswith("# Business digest"))
            with urllib.request.urlopen(base + "/digest", timeout=10) as resp:
                self.assertIn(b"Weekly digest", resp.read())
            for bad in ("days=0", "days=91", "days=x", "format=pdf", "other=1"):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(base + "/api/digest?" + bad, timeout=10)
                self.assertEqual(caught.exception.code, 400, bad)
            tools = at.Tools(at.Source(base, None), at.load_policy(None),
                             at.Audit(Path(tmp) / "audit.jsonl", "claude", "Dana"), None)
            self.assertEqual(tools.call("weekly_digest", {"days": "7"})["kind"], "WEEKLY_DIGEST")
            with self.assertRaises(at.ToolError) as caught:
                tools.call("weekly_digest", {"days": "365"})
            self.assertEqual(caught.exception.status, "INVALID_ARGUMENTS")
            policy = Path(tmp) / "policy.json"
            payments = next(r["subject"]["id"] for r in found["still_open"] if r["title"] == "payments reports an error")
            policy.write_text(json.dumps({"schema_version": 1, "allow_tools": ["weekly_digest"],
                                          "deny_entities": [payments]}))
            hidden = at.Tools(at.Source(base, None), at.load_policy(policy),
                              at.Audit(Path(tmp) / "audit.jsonl", "claude", "Dana"), None).call("weekly_digest", {})
            self.assertNotIn("payments reports an error", json.dumps(hidden))
            self.assertEqual(hidden["counts"]["still_open"], len(hidden["still_open"]))
            offline = at.Tools(at.Source(None, Path(tmp) / "nothing.json"), at.load_policy(None),
                               at.Audit(Path(tmp) / "audit.jsonl", "claude", "Dana"), None)
            with self.assertRaises(at.ToolError) as caught:
                offline.call("weekly_digest", {})
            self.assertEqual(caught.exception.status, "NOT_ALLOWED")


if __name__ == "__main__":
    unittest.main()
