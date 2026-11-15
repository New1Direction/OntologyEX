"""User-supplied incident timelines must not leak information from the future."""
from copy import deepcopy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "ontology-extraction/scripts"
sys.path.insert(0, str(SCRIPTS))
import history_replay as history
import install

FIXTURE = ROOT / "examples/historical-replay/payments-history.json"
BROWSER = shutil.which("chromium") or shutil.which("google-chrome")


class HistoryReplayTests(unittest.TestCase):
    def setUp(self):
        self.dataset = history.read_dataset(FIXTURE)

    def test_constructed_cases_include_miss_false_alarm_conflict_and_unreviewed(self):
        report = history.run(self.dataset)
        self.assertEqual(report["summary"]["two_samples"],
                         {"hit": 1, "miss": 1, "false_alarm": 1, "insufficient_data": 1, "needs_review": 1})
        self.assertEqual(report["summary"]["existing_alerts"], {"hit": 2, "no_alarm": 2, "needs_review": 1})
        cases = {case["id"]: case for case in report["cases"]}
        hit = cases["persistent-before-incident"]
        self.assertEqual(hit["policies"]["two_samples"]["lead_seconds"], 240)
        self.assertEqual(hit["policies"]["existing_alerts"]["lead_seconds"], 120)
        signal = hit["policies"]["two_samples"]["evidence_ref"][0]
        self.assertEqual((signal["previous_refs"], signal["current_refs"]), (["sample-1"], ["sample-2"]))
        conflict = cases["conflicting-readings"]
        self.assertEqual(conflict["policies"]["two_samples"]["outcome"], "insufficient_data")
        self.assertEqual({v["value"] for v in conflict["conflicts"][0]["variants"]}, {280, 720})
        unreviewed = cases["outcome-needs-review"]
        self.assertIsNotNone(unreviewed["policies"]["two_samples"]["first_warning_at"])
        self.assertEqual(unreviewed["policies"]["two_samples"]["outcome"], "needs_review")
        self.assertEqual(unreviewed["outcome"]["incident_at"], "2026-11-05T12:06:00Z")
        self.assertIsNone(unreviewed["policies"]["two_samples"]["lead_seconds"])
        self.assertIn("NOT_INDEPENDENTLY_VERIFIED", report["semantics"])

    def test_late_arrival_cannot_be_used_as_a_historical_warning(self):
        case = next(c for c in self.dataset["cases"] if c["id"] == "late-arriving-reading")
        original = history.run(self.dataset)
        result = next(c for c in original["cases"] if c["id"] == case["id"])
        self.assertEqual(result["late_refs"], ["late-sample"])
        self.assertEqual(result["policies"]["two_samples"]["outcome"], "miss")
        self.assertTrue(all("late-sample" not in d["visible_refs"] for d in result["decisions"]))
        # Same reading available on time would produce a pre-incident signal.
        early = deepcopy(self.dataset)
        sample = next(r for c in early["cases"] if c["id"] == case["id"]
                      for r in c["observations"] if r["ref"] == "late-sample")
        sample["available_at"] = sample["observed_at"]
        changed = next(c for c in history.run(early)["cases"] if c["id"] == case["id"])
        self.assertEqual(changed["policies"]["two_samples"]["outcome"], "hit")
        self.assertEqual(changed["policies"]["two_samples"]["lead_seconds"], 240)

    def test_existing_alert_after_decision_cutoff_is_excluded_even_if_before_incident(self):
        case = next(c for c in self.dataset["cases"] if c["id"] == "persistent-before-incident")
        alert = case["baseline"]["alerts"][0]
        alert["available_at"] = "2026-11-01T12:05:30Z"  # after cutoff, before the 12:06 incident
        result = next(c for c in history.run(self.dataset)["cases"] if c["id"] == case["id"])
        self.assertEqual(result["policies"]["existing_alerts"]["outcome"], "miss")
        self.assertIsNone(result["policies"]["existing_alerts"]["first_warning_at"])
        self.assertEqual(result["outside_alert_refs"], ["fixture:existing-alert"])

    def test_retried_sample_does_not_count_twice(self):
        case = next(c for c in self.dataset["cases"] if c["id"] == "persistent-before-incident")
        sample = deepcopy(case["observations"][1])
        duplicate = {**sample, "ref": "same-sample-retry"}
        case["observations"] = [sample, duplicate]
        result = next(c for c in history.run(self.dataset)["cases"] if c["id"] == case["id"])
        self.assertTrue(all(not d["signals"] for d in result["decisions"]))
        self.assertEqual(result["policies"]["two_samples"]["outcome"], "insufficient_data")
        self.assertEqual(result["conflicts"], [])

    def test_unknown_existing_alert_coverage_is_not_a_clean_no_alarm(self):
        case = next(c for c in self.dataset["cases"] if c["id"] == "signal-without-incident")
        case["baseline"]["coverage"] = "unknown"
        result = next(c for c in history.run(self.dataset)["cases"] if c["id"] == case["id"])
        self.assertEqual(result["policies"]["existing_alerts"]["outcome"], "insufficient_data")

    def test_value_equal_to_threshold_is_not_a_breach(self):
        case = next(c for c in self.dataset["cases"] if c["id"] == "persistent-before-incident")
        for observation in case["observations"]:
            observation["value"] = self.dataset["threshold"]["above"]
        result = next(c for c in history.run(self.dataset)["cases"] if c["id"] == case["id"])
        self.assertEqual(result["policies"]["two_samples"]["outcome"], "miss")
        self.assertTrue(all(not decision["signals"] for decision in result["decisions"]))

    def test_expired_telemetry_is_not_counted_as_coverage(self):
        case = next(c for c in self.dataset["cases"] if c["id"] == "signal-without-incident")
        case["observations"] = case["observations"][:2]
        for observation in case["observations"]:
            observation["value"] = 300
            observation["ttl_seconds"] = 60
        result = next(c for c in history.run(self.dataset)["cases"] if c["id"] == case["id"])
        self.assertEqual(result["policies"]["two_samples"]["outcome"], "insufficient_data")
        self.assertEqual(result["decisions"][-1]["status"], "stale")

    def test_missing_time_and_unjustified_retrospective_threshold_are_refused(self):
        missing = deepcopy(self.dataset)
        del missing["cases"][0]["observations"][0]["available_at"]
        with self.assertRaisesRegex(history.HistoryError, "missing fields"):
            history.run(missing)
        tuned_late = deepcopy(self.dataset)
        tuned_late["threshold"]["selected_at"] = "2026-11-01T12:01:00Z"
        with self.assertRaisesRegex(history.HistoryError, "requires a pre-window training cutoff"):
            history.run(tuned_late)
        backwards = deepcopy(self.dataset)
        backwards["cases"][0]["observations"][1]["available_at"] = "2026-11-01T12:00:00Z"
        with self.assertRaisesRegex(history.HistoryError, "before it happened"):
            history.run(backwards)

    def test_retrospective_holdout_uses_actual_selection_time_and_prior_training_cutoff(self):
        self.dataset["threshold"]["selected_at"] = "2026-11-14T12:00:00Z"
        self.dataset["threshold"]["training_data_available_through"] = "2026-10-30T12:00:00Z"
        report = history.run(self.dataset)
        self.assertTrue(all(c["threshold_mode"] == "retrospective_holdout" for c in report["cases"]))
        self.assertIn("Retrospective holdout", history.render_html(report))
        self.assertIn("not been independently verified", history.render_html(report))
        self.dataset["threshold"]["training_data_available_through"] = "2026-11-01T12:01:00Z"
        with self.assertRaisesRegex(history.HistoryError, "training cutoff must precede"):
            history.run(self.dataset)

    def test_refuses_undeclared_fields_and_bad_outcomes(self):
        extra = deepcopy(self.dataset)
        extra["cases"][0]["observations"][0]["customer_email"] = "private@example.test"
        with self.assertRaisesRegex(history.HistoryError, "unexpected or missing fields"):
            history.run(extra)
        wrong = deepcopy(self.dataset)
        wrong["cases"][0]["outcome"]["incident_at"] = wrong["cases"][0]["window"]["end"]
        with self.assertRaisesRegex(history.HistoryError, "incident must follow decisions"):
            history.run(wrong)

    def test_overlapping_cases_are_called_out_as_possible_double_counts(self):
        repeated = deepcopy(self.dataset["cases"][0])
        repeated["id"] = "duplicate-window"
        self.dataset["cases"].append(repeated)
        report = history.run(self.dataset)
        self.assertIn(["persistent-before-incident", "duplicate-window"], report["overlapping_cases"])
        self.assertIn("same incident or alert may be counted more than once", history.render_html(report))

    def test_html_escapes_content_and_installs_with_the_skill(self):
        report = history.run(self.dataset)
        report["dataset"] = '<img src=x onerror="alert(1)">'
        page = history.render_html(report)
        self.assertIn('&lt;img src=x', page)
        self.assertNotIn('<img src=x', page)
        self.assertIn("default-src 'none'", page)
        self.assertIn("scripts/history_replay.py", install.PACKAGE)
        self.assertIn("references/historical-signal-replay.md", install.PACKAGE)

    def test_installed_skill_runs_the_documented_synthetic_example(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            installed = Path(install.install(project)["path"])
            reference = (installed / "references/historical-signal-replay.md").read_text(encoding="utf-8")
            example = re.search(r"```json\s*\n(.*?)\n```", reference, re.S)
            self.assertIsNotNone(example, "installed reference needs a runnable example")
            dataset = project / "example.json"
            dataset.write_text(json.dumps(json.loads(example.group(1))), encoding="utf-8")
            env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            result = subprocess.run([sys.executable, str(installed / "scripts/history_replay.py"),
                                     "--input", str(dataset)], cwd=tmp, env=env,
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["summary"]["two_samples"], {"hit": 1})
            self.assertEqual(report["summary"]["existing_alerts"], {"insufficient_data": 1})
            self.assertEqual(report["cases"][0]["policies"]["two_samples"]["lead_seconds"], 120)

    def test_cli_does_not_modify_input_or_overwrite_output(self):
        before = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "report.html"
            command = [sys.executable, str(SCRIPTS / "history_replay.py"), "--input", str(FIXTURE),
                       "--format", "html", "--out", str(output)]
            first = subprocess.run(command, capture_output=True, text=True, timeout=20)
            self.assertEqual(first.returncode, 0, first.stderr)
            saved = output.read_bytes()
            second = subprocess.run(command, capture_output=True, text=True, timeout=20)
            self.assertNotEqual(second.returncode, 0)
            self.assertEqual(output.read_bytes(), saved)
        self.assertEqual(hashlib.sha256(FIXTURE.read_bytes()).hexdigest(), before)

    @unittest.skipUnless(BROWSER, "Chromium is optional")
    def test_browser_displays_reviews_without_external_requests(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("Playwright is optional")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.html"
            path.write_text(history.render_html(history.run(self.dataset)), encoding="utf-8")
            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    body = path.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(self, *_):
                    pass

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                with sync_playwright() as p:
                    browser = p.chromium.launch(executable_path=BROWSER, headless=True, args=["--no-sandbox"])
                    try:
                        page = browser.new_page(viewport={"width": 390, "height": 844})
                        requests, errors = [], []
                        page.on("request", lambda request: requests.append(request.url))
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.goto(base + "/report.html", wait_until="load")
                        self.assertIn("not a prediction", page.locator(".notice").inner_text())
                        late = page.locator("section").filter(has_text="late-arriving-reading")
                        self.assertIn("late-sample", late.inner_text())
                        late.get_by_text("Review observation times and sources").click()
                        self.assertIn("2026-11-02T12:07:00Z", late.inner_text())
                        conflict = page.locator("section").filter(has_text="conflicting-readings")
                        self.assertIn("Conflicting readings", conflict.inner_text())
                        self.assertIn("280", conflict.inner_text())
                        self.assertIn("720", conflict.inner_text())
                        self.assertIn("needs review", page.locator("section").filter(has_text="outcome-needs-review").inner_text())
                        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 390)
                        self.assertEqual(errors, [])
                        self.assertTrue(all(url.startswith(base + "/") for url in requests))
                    finally:
                        browser.close()
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
