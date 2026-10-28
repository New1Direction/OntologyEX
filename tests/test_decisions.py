"""Decisions in batches, and undo: all of a batch lands or none of it, and every change is logged."""
import importlib.util
import json
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "ontology-extraction/scripts"
sys.path.insert(0, str(SCRIPTS))
import business_view as bv
import control_room as room
import decisions
import dependency_suggest as ds
import discover_repo
import feeds
import link_suggest as ls
from policy_check import DomainError

spec = importlib.util.spec_from_file_location("ontologyex_decisions_demo", ROOT / "examples/operating-picture/demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
SERVICES = {name: f"discovery:service:compose.yml:{name}" for name in ("checkout", "payments", "ledger", "web")}
COMPOSE = "services:\n" + "".join(f"  {name}: {{image: shop/{name}}}\n" for name in SERVICES)
CALLS = {("checkout", "payments"): 40, ("payments", "ledger"): 25, ("web", "checkout"): 3}


def history(path: Path) -> list[tuple[str, str]]:
    lines = decisions.history_path(path).read_text().splitlines()
    return [(e["action"], e["by"]) for e in map(json.loads, lines)]


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        (self.repo / "compose.yml").write_text(COMPOSE)
        self.calls = root / "obs.calls.json"
        totals = {pair: {"calls": n, "errors": 0, "first_seen": "2026-10-28T08:00:00Z",
                         "last_seen": "2026-10-28T09:00:00Z"} for pair, n in CALLS.items()}
        self.calls.write_text(json.dumps(feeds.calls_document(totals, SERVICES, NOW)))
        self.deps = root / "deps.json"

    def suggestions(self):
        confirmed, rejected = ds.read_dependency_document(self.deps) if self.deps.exists() else ([], [])
        state = room.build_state(discover_repo.discover(self.repo), [], {}, NOW)
        return ds.suggest(state, feeds.read_calls(self.calls), confirmed, rejected)["suggestions"]


class Batches(Fixture):
    def test_a_batch_is_one_write_and_undo_brings_suggestions_back(self):
        found = self.suggestions()
        self.assertEqual(len(found), 3)
        result = ds.apply_decisions(self.deps, "ana", confirm=found[:2], reject=found[2:])
        self.assertEqual(result["status"], "APPLIED")
        self.assertEqual([a["action"] for a in result["applied"]], ["confirm", "confirm", "reject"])
        self.assertEqual(self.suggestions(), [])
        confirmed, rejected = ds.read_dependency_document(self.deps)
        self.assertEqual((len(confirmed), len(rejected)), (2, 1))
        # The IDs a suggestion carries are the IDs that undo its decision.
        undone = ds.apply_decisions(self.deps, "bo", undo=[s["id"] for s in found[:2]])
        self.assertEqual((undone["status"], len(undone["applied"])), ("UNDONE", 2))
        self.assertEqual([s["id"] for s in self.suggestions()], [s["id"] for s in found[:2]])
        self.assertEqual(history(self.deps), [("confirm", "ana"), ("confirm", "ana"), ("reject", "ana"),
                                              ("undo", "bo"), ("undo", "bo")])

    def test_uniform_batches_report_like_single_decisions(self):
        found = self.suggestions()
        self.assertEqual(ds.apply_decisions(self.deps, "ana", confirm=found)["status"], "CONFIRMED")
        again = ds.apply_decisions(self.deps, "ana", confirm=found)
        self.assertEqual((again["status"], len(again["unchanged"])), ("UNCHANGED", 3))
        self.assertEqual(len(history(self.deps)), 3)  # nothing changed, nothing logged

    def test_a_bad_batch_changes_nothing(self):
        found = self.suggestions()
        ds.apply_decisions(self.deps, "ana", confirm=found[:1])
        before = self.deps.read_bytes()
        for kwargs in ({"undo": [found[0]["id"], "not-decided"]}, {"undo": []},
                       {"confirm": found * 34}, {"confirm": [{**found[1], "status": "CONFIRMED"}]}):
            with self.assertRaises(DomainError, msg=kwargs):
                ds.apply_decisions(self.deps, "ana", **kwargs)
        with self.assertRaises(DomainError):
            ds.apply_decisions(self.deps, " ", confirm=found[1:])
        self.assertEqual(self.deps.read_bytes(), before)
        self.assertEqual(len(history(self.deps)), 1)

    def test_the_row_limit_is_checked_before_writing(self):
        found = self.suggestions()
        with unittest.mock.patch.object(ds, "MAX_DEPENDENCIES", 2):
            with self.assertRaisesRegex(DomainError, "undo some first"):
                ds.apply_decisions(self.deps, "ana", confirm=found)
        self.assertFalse(self.deps.exists())

    def test_a_file_changed_during_the_batch_is_refused(self):
        found = self.suggestions()
        ds.apply_decisions(self.deps, "ana", confirm=found[:1])

        def read_then_change(path):
            rows = ds.read_dependency_document(path)
            path.write_text(path.read_text() + " ")
            return rows

        with self.assertRaisesRegex(DomainError, "changed during write"):
            decisions.apply(self.deps, read=read_then_change, kind=ds.DEPENDENCY_KIND, confirmed_field="confirmed",
                            key=ds.dependency_key, name="dependencies", max_rows=10, by="bo",
                            confirm=[{"source": found[1]["source"], "target": found[1]["target"],
                                      "evidence": found[1]["evidence"]}])

    def test_decided_lists_newest_first_with_undo_ids(self):
        found = self.suggestions()
        ds.apply_decisions(self.deps, "ana", confirm=found[:1])
        ds.apply_decisions(self.deps, "ana", confirm=found[1:2])
        ds.apply_decisions(self.deps, "ana", reject=found[2:])
        rows = decisions.decided(*ds.read_dependency_document(self.deps), ds.dependency_key)
        self.assertEqual([(r["id"], r["decision"]) for r in rows],
                         [(found[1]["id"], "confirm"), (found[0]["id"], "confirm"), (found[2]["id"], "reject")])

    def test_links_batch_and_undo(self):
        with tempfile.TemporaryDirectory() as tmp:
            built = demo.build(Path(tmp) / "demo", NOW, unlinked=True)
            skill = Path(built["skill"])
            state = room.build_state(discover_repo.discover(Path(built["repo"])), [], {}, NOW)
            path = Path(tmp) / "links.json"
            found = ls.suggest([skill], state, [], [])["suggestions"]
            self.assertEqual(ls.apply_decisions(path, "ana", confirm=found)["status"], "CONFIRMED")
            links, rejected = bv.read_link_document(path)
            self.assertEqual(ls.suggest([skill], state, links, rejected)["suggestions"], [])
            ls.apply_decisions(path, "ana", undo=[found[0]["id"]])
            links, rejected = bv.read_link_document(path)
            self.assertEqual([s["id"] for s in ls.suggest([skill], state, links, rejected)["suggestions"]],
                             [found[0]["id"]])


class CommandLine(Fixture):
    def run_cli(self, *args):
        run = subprocess.run([sys.executable, str(SCRIPTS / "dependency_suggest.py"), *args, "--repo", str(self.repo),
                              "--calls", str(self.calls), "--dependencies", str(self.deps)],
                             capture_output=True, text=True, timeout=60)
        return run.returncode, json.loads(run.stdout)

    def test_all_strong_repeatable_ids_and_undo(self):
        code, result = self.run_cli("confirm", "--all-strong", "--by", "ana")
        self.assertEqual((code, result["status"], len(result["applied"])), (0, "CONFIRMED", 2))
        code, listed = self.run_cli("suggest")
        self.assertEqual([s["strength"] for s in listed["suggestions"]], ["WEAK"])
        self.assertEqual([r["decision"] for r in listed["decided"]], ["confirm", "confirm"])
        code, result = self.run_cli("undo", "--by", "ana", *[x for r in listed["decided"] for x in ("--id", r["id"])])
        self.assertEqual((code, result["status"]), (0, "UNDONE"))
        code, listed = self.run_cli("suggest")
        ids = [s["id"] for s in listed["suggestions"]]
        code, result = self.run_cli("reject", "--by", "ana", "--id", ids[0], "--id", ids[1])
        self.assertEqual((result["status"], len(result["applied"])), ("REJECTED", 2))
        for args in (("confirm", "--by", "ana"), ("undo", "--by", "ana"), ("undo", "--by", "ana", "--id", "nope"),
                     ("confirm", "--by", "ana", "--id", "nope")):
            code, result = self.run_cli(*args)
            self.assertEqual((code, result["status"]), (1, "ERROR"), args)


class Http(Fixture):
    def serve(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        observations = Path(self.tmp.name) / "obs.jsonl"
        observations.touch()
        process = subprocess.Popen([sys.executable, str(SCRIPTS / "control_room.py"), "--repo", str(self.repo),
                                    "--observations", str(observations), "--dependencies", str(self.deps),
                                    "--allow-link-decisions", "--port", str(port)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (process.terminate(), process.wait(timeout=5)))
        base = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                with urllib.request.urlopen(base + "/business", timeout=2) as resp:
                    page = resp.read().decode()
                return base, page.split('name="ontologyex-write-token" content="')[1].split('"')[0]
            except OSError:
                time.sleep(0.1)
        self.fail("control room did not start")

    def post(self, base, token, body):
        request = urllib.request.Request(base + "/api/dependencies/decision", method="POST", data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json", "Origin": base,
                                                  "X-OntologyEX-Token": token})
        try:
            with urllib.request.urlopen(request, timeout=5) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def business(self, base):
        with urllib.request.urlopen(base + "/api/business", timeout=5) as resp:
            return json.load(resp)

    def test_batch_then_undo_over_http(self):
        base, token = self.serve()
        ids = [s["id"] for s in self.business(base)["dependency_suggestions"]["suggestions"]]
        status, result = self.post(base, token, {"ids": ids, "decision": "confirm", "by": "ana", "note": ""})
        self.assertEqual((status, result["status"], len(result["applied"])), (200, "CONFIRMED", 3))
        data = self.business(base)
        self.assertEqual(data["dependency_suggestions"]["suggestions"], [])
        self.assertEqual({r["id"] for r in data["dependency_decided"]}, set(ids))
        self.assertNotIn("evidence", data["dependency_decided"][0])
        status, result = self.post(base, token, {"ids": ids[:1], "decision": "undo", "by": "ana", "note": ""})
        self.assertEqual((status, result["status"]), (200, "UNDONE"))
        self.assertEqual([s["id"] for s in self.business(base)["dependency_suggestions"]["suggestions"]], ids[:1])
        for body in ({"ids": ids[:1] + ["nope"], "decision": "confirm", "by": "ana", "note": ""},
                     {"ids": ["x"] * 101, "decision": "undo", "by": "ana", "note": ""},
                     {"ids": [], "decision": "confirm", "by": "ana", "note": ""},
                     {"ids": ids[:1], "decision": "approve", "by": "ana", "note": ""},
                     {"ids": ids[:1], "id": ids[0], "decision": "confirm", "by": "ana", "note": ""}):
            self.assertEqual(self.post(base, token, body)[0], 400, body)
        self.assertEqual(len(self.business(base)["dependency_suggestions"]["suggestions"]), 1)


if __name__ == "__main__":
    unittest.main()
