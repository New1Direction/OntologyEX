"""Benchmark reporting: time, cost and correctness apart, a speed-up only for the same answers, conditions disclosed."""
import copy
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import bench_rust_engines as bench

LATEST = ROOT / "docs" / "benchmarks" / "latest.json"
TINY = {"repo_modules": 1, "runs": 1, "context_runs": 1, "receiver_runs": 1, "seconds": 1.0, "clients": 1}


def engine(wall, cpu, memory):
    return {"status": "MEASURED", "wall_seconds": bench.summary(wall), "cpu_seconds": bench.summary(cpu),
            "peak_rss_mib": memory}


class NumberTests(unittest.TestCase):
    def test_percentiles_are_measured_values(self):
        values = [10, 1, 9, 2, 8, 3, 7, 4, 6, 5]
        s = bench.summary(values)
        self.assertEqual((s["median"], s["p10"], s["p90"], s["min"], s["max"], s["n"]), (5.5, 1, 9, 1, 10, 10))
        self.assertEqual(s["spread_pct"], round(100 * 8 / 5.5, 1))
        self.assertEqual(bench.summary([3.0])["p90"], 3.0)

    def test_a_speed_up_needs_the_same_answers(self):
        engines = {"python": engine([2.0, 2.2], [1.9, 2.1], 40.0), "rust": engine([0.2, 0.22], [0.1, 0.1], 10.0)}
        same = bench.compare(engines, {"status": "SAME"}, "discovery")
        self.assertEqual(same, {"wall_time": 10.0, "cpu_time": 20.0, "memory": 4.0})
        self.assertEqual(bench.compare(engines, {"status": "DIFFERENT"}, "discovery"), {"withheld": "ANSWERS_DIFFER"})
        self.assertEqual(bench.compare(engines, {"status": "NOT_COMPARED"}, "discovery"), {"withheld": "NOT_BOTH_RUN"})

    def test_slower_is_said_as_slower(self):
        self.assertEqual(bench._x(0.02, "faster", "slower"), "50.0× slower")
        self.assertEqual(bench._x(12.345, "faster", "slower"), "12.3× faster")
        self.assertEqual(bench._x(None, "faster", "slower"), "–")

    def test_the_reference_window_for_the_receiver(self):
        otlp = {"services": bench.RECEIVER_SERVICES, "window_seconds": 60, "emit_seconds": 1}
        rows = bench.expected_rows(bench.receiver_body(), 3, otlp)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["metrics"]["error_rate_pct"], 14.3)  # 143 of every 1,000 spans failed
        self.assertEqual(rows[0]["metrics"]["p95_latency_ms"], 283.0)
        self.assertEqual(rows[0]["component_id"], "discovery:service:compose.yml:payments")


class CaveatTests(unittest.TestCase):
    def report(self):
        machine = {"usable_cpus": 4, "load_average_at_start": [0.1, 0.1, 0.1]}
        receiver = {"status": "MEASURED", "spans_per_second": bench.summary([100.0, 101.0]), "cores_used": 2.0,
                    "client_cores_used": 1.6}
        return {"conditions": {"machine": machine, "uncommitted_changes": False,
                               "engines": {"discovery": {"profile": "release"}}},
                "workloads": [{"workload": "otlp_receiver", "title": "OTLP receiver under load",
                               "engines": {"python": receiver, "rust": bench.not_run("no binary")}}]}

    def test_a_saturated_machine_makes_the_figure_a_lower_bound(self):
        codes = [c["code"] for c in bench.caveats(self.report())]
        self.assertEqual(codes, ["MACHINE_SATURATED"])

    def test_debug_noisy_busy_and_uncommitted_are_said(self):
        report = self.report()
        report["conditions"]["engines"]["discovery"]["profile"] = "debug"
        report["conditions"]["machine"]["load_average_at_start"] = [3.5, 1, 1]
        report["conditions"]["uncommitted_changes"] = True
        report["workloads"][0]["engines"]["python"].update(cores_used=0.5, client_cores_used=0.5,
                                                          spans_per_second=bench.summary([50.0, 100.0]))
        codes = [c["code"] for c in bench.caveats(report)]
        self.assertEqual(codes, ["DEBUG_BUILD", "BUSY_MACHINE", "NOISY", "UNCOMMITTED"])


class RunTests(unittest.TestCase):
    def test_python_alone_is_measured_and_not_compared(self):
        report = bench.run({w: None for w in bench.WORKLOADS}, dict(TINY))
        self.assertEqual([w["workload"] for w in report["workloads"]], list(bench.WORKLOADS))
        for w in report["workloads"]:
            self.assertEqual(w["engines"]["python"]["status"], "MEASURED", w["workload"])
            self.assertEqual(w["engines"]["rust"]["status"], "NOT_RUN", w["workload"])
            self.assertEqual(w["correctness"]["status"], "NOT_COMPARED", w["workload"])
            self.assertEqual(w["comparison"], {"withheld": "NOT_BOTH_RUN"})
        receiver = report["workloads"][2]["engines"]["python"]
        self.assertGreater(receiver["spans_per_second"]["median"], 0)
        self.assertEqual(receiver["failed_requests"], 0)
        self.assertEqual(report["answers_differ"], [])
        conditions = report["conditions"]
        for key in ("measured_at", "machine", "os", "python", "commit", "settings"):
            self.assertIn(key, conditions)
        self.assertIn("No comparison: not both run.", bench.markdown(report))

    def test_a_different_answer_is_reported_and_fails_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "ontologyex-discover"
            fake.write_text("#!/bin/sh\necho '{\"services\": []}'\n")
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
            out = Path(tmp) / "report.json"
            argv = ["--discover", str(fake), "--workloads", "discovery", "--json", str(out),
                    *[f"--{k.replace('_', '-')}={v}" for k, v in TINY.items()]]
            code = bench.main(argv)
            report = json.loads(out.read_text())
        self.assertEqual(code, 1)
        discovery = report["workloads"][0]
        self.assertEqual(discovery["correctness"]["status"], "DIFFERENT")
        self.assertEqual(discovery["comparison"], {"withheld": "ANSWERS_DIFFER"})
        self.assertEqual(report["answers_differ"], ["discovery"])
        self.assertEqual(report["conditions"]["engines"]["discovery"]["profile"], "unknown")
        self.assertIn("UNKNOWN_BUILD", [c["code"] for c in report["caveats"]])
        self.assertIn("No comparison: answers differ.", bench.markdown(report))


class DocsTests(unittest.TestCase):
    def test_the_docs_quote_the_saved_report(self):
        self.assertEqual(bench.write_docs(LATEST, check_only=True), [])

    def test_the_saved_report_is_a_release_run_with_the_same_answers(self):
        report = json.loads(LATEST.read_text("utf-8"))
        self.assertEqual(report["kind"], "ONTOLOGYEX_BENCHMARK")
        self.assertEqual({e["profile"] for e in report["conditions"]["engines"].values()}, {"release"})
        self.assertFalse(report["conditions"]["settings"]["quick"])
        self.assertFalse(report["conditions"]["uncommitted_changes"])
        self.assertEqual({w["correctness"]["status"] for w in report["workloads"]}, {"SAME"})
        self.assertEqual((ROOT / "docs/benchmarks/latest.md").read_text("utf-8"), bench.markdown(report))

    def test_a_changed_number_makes_the_docs_stale(self):
        report = json.loads(LATEST.read_text("utf-8"))
        changed = copy.deepcopy(report)
        changed["workloads"][0]["engines"]["rust"]["wall_seconds"]["median"] *= 2
        changed["workloads"][0]["comparison"]["wall_time"] /= 2
        self.assertNotEqual(bench.docs_block(report, "full", "x.md"), bench.docs_block(changed, "full", "x.md"))


if __name__ == "__main__":
    unittest.main()
