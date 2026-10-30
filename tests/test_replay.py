"""Replays and evidence: given only what was known at a moment, the right verdict, cause and citations.

The corpus in tests/replays (synthetic, built by tests/replays/build.py) is replayed through the control
room's own assembly; every finding names its evidence; missing or expired evidence is never healthy.
"""
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "ontology-extraction/scripts"
sys.path.insert(0, str(SCRIPTS))
import command_center
import feeds
import replay

CORPUS = sorted(p for p in (ROOT / "tests/replays").iterdir() if (p / "case.json").is_file())
spec = importlib.util.spec_from_file_location("ontologyex_replay_test_demo", ROOT / "examples/operating-picture/demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)
AT = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)


class CorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = replay.run(CORPUS)
        cls.cases = {c["case"]: c for c in cls.report["cases"]}

    def test_every_case_passes_its_expectations(self):
        self.assertEqual(len(CORPUS), 7)
        failed = {c["case"]: [k for k, ok in c["checks"].items() if not ok] for c in self.report["cases"] if not c["passed"]}
        self.assertEqual(failed, {})
        summary = self.report["summary"]
        self.assertEqual((summary["verdicts_right"], summary["cause_within_rate"], summary["citation_coverage"],
                          summary["healthy_without_evidence"]), (7, 1.0, 1.0, 0))

    def test_missing_evidence_is_inconclusive_and_names_no_cause(self):
        for name in ("no-evidence", "expired", "one-silent"):
            verdict = self.cases[name]["verdict"]
            self.assertEqual((verdict["status"], verdict["causes"]), ("INCONCLUSIVE", []), name)
            self.assertTrue(verdict["evidence"]["missing"], name)
        self.assertEqual(self.cases["no-evidence"]["verdict"]["confidence"], "NONE")
        self.assertEqual(self.cases["one-silent"]["verdict"]["evidence"]["missing"], ["discovery:service:compose.yml:web"])
        healthy = self.cases["all-healthy"]["verdict"]
        self.assertEqual((healthy["status"], healthy["confidence"]), ("HEALTHY", "HIGH"))
        self.assertGreater({"HIGH": 2, "PARTIAL": 1, "NONE": 0}[self.cases["payments-error"]["verdict"]["confidence"]],
                           {"HIGH": 2, "PARTIAL": 1, "NONE": 0}[self.cases["no-evidence"]["verdict"]["confidence"]])

    def test_the_root_cause_comes_first(self):
        self.assertEqual(self.cases["upstream-error"]["verdict"]["causes"][:2],
                         ["discovery:service:compose.yml:payments", "discovery:service:compose.yml:api"])

    def test_a_wrong_expectation_fails_and_the_case_is_never_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = Path(tmp) / "payments-error"
            shutil.copytree(ROOT / "tests/replays/payments-error", case)
            meta = json.loads((case / "case.json").read_text())
            meta["expect"] = {"verdict": "HEALTHY", "cause": [], "within": 3}
            (case / "case.json").write_text(json.dumps(meta))
            run = subprocess.run([sys.executable, str(SCRIPTS / "replay.py"), "run", str(case), "--format", "json"],
                                 capture_output=True, text=True, timeout=300)
            self.assertEqual(run.returncode, 1)
            result = json.loads(run.stdout)["cases"][0]
            self.assertEqual((result["checks"]["verdict"], result["checks"]["read_only"]), (False, True))


class EvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reports = {}
        for name in ("payments-error", "expired"):
            case = replay.load_case(ROOT / "tests/replays" / name)
            cls.reports[name] = command_center.report(replay.bundle_of(ROOT / "tests/replays" / name, case))

    def test_every_finding_names_what_it_rests_on(self):
        for name, report in self.reports.items():
            for item in report["items"]:
                self.assertTrue(item["evidence"], (name, item["kind"]))
                for e in item["evidence"]:
                    self.assertIn(e["state"], command_center.EVIDENCE_STATES)
                    self.assertIn(e["role"], ("basis", "context"))
                    self.assertEqual(set(e), {"kind", "ref", "source", "component", "environment", "collected_at",
                                              "age_seconds", "state", "role", "detail"})

    def test_an_error_rests_on_a_fresh_sample_and_says_what_would_refute_it(self):
        error = next(i for i in self.reports["payments-error"]["items"] if i["kind"] == "SYSTEM_ERROR")
        basis = [e for e in error["evidence"] if e["role"] == "basis"]
        self.assertEqual([(e["kind"], e["state"], e["environment"], e["age_seconds"]) for e in basis],
                         [("observation", "COMPLETE", "production", 120)])
        self.assertEqual(error["contradicted_by"], "a fresh ok sample from payments newer than 2026-10-29T11:58:00Z")
        self.assertEqual(error["standing"], "SOURCE")

    def test_a_conclusion_on_expired_evidence_reads_unknown(self):
        stale = [i for i in self.reports["expired"]["items"] if i["kind"] == "STALE"]
        self.assertTrue(stale)
        for item in stale:
            self.assertEqual((item["standing"], item["unknown_because"]), ("UNKNOWN", "its evidence has expired"))
            self.assertTrue(all(e["state"] == "STALE" for e in item["evidence"] if e["role"] == "basis"))


    def test_agents_do_not_see_evidence_from_withheld_files(self):
        import agent_tools as at
        with tempfile.TemporaryDirectory() as tmp:
            case = replay.load_case(ROOT / "tests/replays/payments-error")
            (Path(tmp) / "bundle.json").write_text(json.dumps(replay.bundle_of(ROOT / "tests/replays/payments-error", case)))
            policy = Path(tmp) / "policy.json"
            policy.write_text(json.dumps({"schema_version": 1, "allow_tools": ["what_needs_attention"],
                                          "deny_paths": ["compose.yml"]}))
            tools = at.Tools(at.Source(None, Path(tmp) / "bundle.json"), at.load_policy(policy),
                             at.Audit(Path(tmp) / "audit.jsonl", "claude", "Dana"), None)
            text = json.dumps(tools.call("what_needs_attention", {}))
            self.assertIn('"evidence"', text)
            self.assertNotIn('"ref": "compose.yml', text)


class CaptureTests(unittest.TestCase):
    def test_capture_keeps_what_was_known_then_and_no_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            built = demo.build(Path(tmp) / "demo", now=AT)
            observations = Path(tmp) / "health.jsonl"
            rows = []
            for name, age in (("payments", timedelta(minutes=5)), ("api", timedelta(hours=30)),
                              ("db", timedelta(minutes=-10))):  # within the window, before it, after the moment
                rows.append({"schema_version": 1, "component_id": demo.SERVICE + name,
                             "observed_at": (AT - age).strftime("%Y-%m-%dT%H:%M:%SZ"), "status": "error",
                             "source": "synthetic", "expires_after_seconds": 3600, "metrics": {}})
            observations.write_text("".join(json.dumps(r) + "\n" for r in rows))
            run = subprocess.run([sys.executable, str(SCRIPTS / "replay.py"), "capture", "--repo", built["repo"],
                                  "--name", "outage", "--at", "2026-10-29T12:00:00Z", "--observations", str(observations),
                                  "--skill", built["skill"], "--links", built["links"], "--environment", "production"],
                                 capture_output=True, text=True, timeout=300)
            self.assertEqual(run.returncode, 0, run.stderr)
            answer = json.loads(run.stdout)
            self.assertEqual((answer["observations"], answer["concluded"]["status"]), (1, "FAULT"))
            case = Path(built["repo"]) / ".ontologyex/replays/outage"
            self.assertEqual(sorted(p.name for p in case.iterdir()),
                             ["case.json", "links.json", "observations.jsonl", "operations.json", "skills", "topology.json"])
            ledger = (case / "operations.json").read_text()
            self.assertIn("OPERATIONS_LEDGER", ledger)
            for needle in ("@example", "cus_", "ch_"):  # aggregates only: no customer, charge or email
                self.assertNotIn(needle, ledger)
            meta = json.loads((case / "case.json").read_text())
            self.assertEqual((meta["expect"]["verdict"], meta["environment"]), (None, "production"))
            result = replay.evaluate(case)
            self.assertFalse(result["scored"])
            self.assertTrue(result["passed"])  # unscored: only the standing checks (citations, read-only)
            again = subprocess.run([sys.executable, str(SCRIPTS / "replay.py"), "capture", "--repo", built["repo"],
                                    "--name", "outage"], capture_output=True, text=True, timeout=300)
            self.assertEqual(again.returncode, 2)

    def test_the_frozen_inputs_are_only_for_bundles(self):
        run = subprocess.run([sys.executable, str(SCRIPTS / "control_room.py"), "--topology",
                              str(ROOT / "tests/replays/payments-error/topology.json"), "--port", "0"],
                             capture_output=True, text=True, timeout=60)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("bundle-only", run.stdout + run.stderr)


class FreshnessProbeTests(unittest.TestCase):
    def test_how_long_since_a_job_last_worked(self):
        config = {"schema_version": 1, "kind": "ONTOLOGYEX_FEEDS", "output": "health.jsonl",
                  "prometheus": {"url": "http://prom.internal:9090", "targets": [
            {"component": "discovery:service:compose.yml:indexer", "job_lag_seconds": "time() - indexer_last_block_ts",
             "warning": {"job_lag_seconds": 120}, "error": {"job_lag_seconds": 600}}]},
                  "allowed_hosts": ["prom.internal"]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "feeds.json"
            path.write_text(json.dumps(config))
            feeds.load_config(path)  # a config with a freshness query and thresholds on it is valid
            config["prometheus"]["targets"][0]["error"] = {"job_lag": 600}
            path.write_text(json.dumps(config))
            with self.assertRaises(feeds.FeedError):
                feeds.load_config(path)
        prom = config["prometheus"]
        prom["targets"][0]["error"] = {"job_lag_seconds": 600}
        for lag, status in ((30, "ok"), (300, "warning"), (900, "error")):
            body = json.dumps({"status": "success", "data": {"resultType": "vector",
                                                             "result": [{"value": [0, str(lag)]}]}}).encode()
            row = feeds.run_prometheus(prom, prom["targets"][0], AT, getter=lambda *a, body=body, **k: (200, body, {}, None))
            self.assertEqual((row["status"], row["metrics"]), (status, {"job_lag_seconds": float(lag)}))


if __name__ == "__main__":
    unittest.main()
