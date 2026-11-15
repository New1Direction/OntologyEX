"""Local observation recording, uncertainty, and the feed-to-replay workflow."""
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "ontology-extraction/scripts"
sys.path.insert(0, str(SCRIPTS))
import control_room
import feeds
import history_replay
import install
import observation_history as journal

COMPONENT = "discovery:service:compose.yml:payments"
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def sample(second, value=100, **changes):
    return {"schema_version": 1, "component_id": COMPONENT, "source": "prometheus", "status": "ok",
            "observed_at": journal.iso(T0 + timedelta(seconds=second)), "expires_after_seconds": 60,
            "metrics": {"p95_latency_ms": value}, **changes}


def export(path):
    return journal.export_case(path, name="review", component=COMPONENT, metric="p95_latency_ms",
        start=journal.iso(T0), end=journal.iso(T0 + timedelta(seconds=3)),
        follow_up_end=journal.iso(T0 + timedelta(seconds=60)), step_seconds=1,
        threshold=500, selected_at=journal.iso(T0 - timedelta(seconds=1)))


class RecordingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "history.sqlite3"

    def test_only_new_published_readings_are_recorded_across_eviction_and_restart(self):
        store = feeds.ObservationStore(self.root / "live.jsonl", keep=2)
        store.add([sample(0)])  # Before opt-in; its original availability is unknown.
        self.assertFalse(self.path.exists())
        recorder = journal.Recorder(self.path)
        store.recorder = recorder
        for second in (1, 2, 3):
            store.add([sample(second)])
        recorder.close()
        self.assertEqual(len(control_room.read_observations(store.path)), 2)
        self.assertEqual(journal.status(self.path)["observations"], 3)
        recorder = journal.Recorder(self.path)
        store.recorder = recorder
        store.add([sample(4)])
        recorder.close()
        info = journal.status(self.path)
        self.assertEqual(info["observations"], 4)
        self.assertEqual([s["state"] for s in info["sessions"]], ["closed", "closed"])
        self.assertEqual(info["coverage"], "UNKNOWN")
        if os.name == "posix":
            self.assertEqual(self.path.stat().st_mode & 0o077, 0)

    def test_batch_eviction_is_counted_and_unpublished_rows_not_journaled(self):
        recorder = journal.Recorder(self.path)
        store = feeds.ObservationStore(self.root / "live.jsonl", keep=2, recorder=recorder)
        store.add([sample(1), sample(2), sample(3)])
        recorder.close()
        info = journal.status(self.path)
        self.assertEqual(info["observations"], 2)
        self.assertEqual(info["omitted_observations"], 1)

    def test_duplicate_keeps_first_availability_and_conflicts_refuse_export(self):
        recorder = journal.Recorder(self.path)
        first = T0 + timedelta(seconds=1, microseconds=10)
        recorder.record([sample(1), sample(1)], first)
        recorder.record([sample(1)], first + timedelta(seconds=20))
        recorder.close()
        data = export(self.path)["cases"][0]["observations"]
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["available_at"], journal.iso(first))
        self.assertEqual(journal.status(self.path)["sessions"][0]["duplicates"], 2)
        recorder = journal.Recorder(self.path)
        different_metric = sample(1, metrics={"p95_latency_ms": 100, "error_rate_pct": 3})
        recorder.record([different_metric], T0 + timedelta(seconds=22))
        recorder.close()
        with self.assertRaisesRegex(journal.RecordingError, "conflicting"):
            export(self.path)

    def test_unknown_capture_coverage_keeps_all_results_unscored(self):
        recorder = journal.Recorder(self.path)
        for second in (0, 1, 2):
            recorder.record([sample(second)], T0 + timedelta(seconds=second))
        recorder.close()
        data = export(self.path)
        case = data["cases"][0]
        self.assertEqual(case["capture_coverage"]["state"], "unknown")
        self.assertEqual(case["outcome"]["state"], "unreviewed")
        case["outcome"] = {"state": "no_incident", "incident_at": None, "evidence_ref": "review:quiet"}
        result = history_replay.run(data)["cases"][0]
        self.assertEqual(result["policies"]["two_samples"]["outcome"], "insufficient_data")
        self.assertIn("Recording coverage", history_replay.render_html(history_replay.run(data)))
        case["outcome"] = {"state": "incident", "incident_at": journal.iso(T0 + timedelta(seconds=4)),
                           "evidence_ref": "review:incident"}
        self.assertEqual(history_replay.run(data)["cases"][0]["policies"]["two_samples"]["outcome"],
                         "insufficient_data")
        for row in case["observations"]:
            row["value"] = 700
        result = history_replay.run(data)["cases"][0]["policies"]["two_samples"]
        self.assertIsNotNone(result["first_warning_at"])
        self.assertEqual(result["outcome"], "insufficient_data")
        case["capture_coverage"] = {"state": "complete", "evidence_ref": "review:coverage"}
        self.assertEqual(history_replay.run(data)["cases"][0]["policies"]["two_samples"]["outcome"], "hit")
        for row in case["observations"]:
            row["value"] = 100
        self.assertEqual(history_replay.run(data)["cases"][0]["policies"]["two_samples"]["outcome"], "miss")

    def test_future_availability_and_unrelated_database_are_refused_without_modification(self):
        recorder = journal.Recorder(self.path)
        with self.assertRaisesRegex(journal.RecordingError, "later than local availability"):
            recorder.record([sample(2)], T0 + timedelta(seconds=1))
        recorder.close()
        self.assertEqual(journal.status(self.path)["observations"], 0)
        unrelated = self.root / "other.sqlite3"
        with sqlite3.connect(unrelated) as database:
            database.execute("CREATE TABLE original(value TEXT)")
            database.execute("INSERT INTO original VALUES ('preserve')")
        unrelated.chmod(0o600)
        original = unrelated.read_bytes()
        with self.assertRaisesRegex(journal.RecordingError, "unsupported history database"):
            journal.Recorder(unrelated)
        self.assertEqual(unrelated.read_bytes(), original)

    def test_backwards_clock_does_not_create_early_knowledge(self):
        recorder = journal.Recorder(self.path)
        recorder.record([sample(0)], T0 + timedelta(seconds=1))
        recorder.record([sample(0)], T0 + timedelta(seconds=3))  # Duplicate, but time still advanced.
        with self.assertRaisesRegex(journal.RecordingError, "clock moved backwards"):
            recorder.record([sample(1)], T0 + timedelta(seconds=2))
        recorder.close(clean=False)
        self.assertEqual(journal.status(self.path)["observations"], 1)
        self.assertEqual(journal.status(self.path)["failed_sessions"], 1)

    def test_existing_v1_history_gets_a_clock_without_losing_readings(self):
        recorder = journal.Recorder(self.path)
        recorder.record([sample(0)], T0 + timedelta(seconds=1))
        recorder.close()
        with sqlite3.connect(self.path) as database:
            database.execute("DROP TABLE journal_clock")
        recorder = journal.Recorder(self.path)
        with self.assertRaisesRegex(journal.RecordingError, "clock moved backwards"):
            recorder.record([sample(0)], T0)
        recorder.record([sample(1)], T0 + timedelta(seconds=2))
        recorder.close()
        self.assertEqual(journal.status(self.path)["observations"], 2)

    def test_limit_rolls_back_whole_batch_and_unfinished_sessions_remain_visible(self):
        recorder = journal.Recorder(self.path, max_rows=1)
        recorder.record([sample(0)], T0 + timedelta(seconds=1))
        with self.assertRaisesRegex(journal.RecordingError, "full"):
            recorder.record([sample(1), sample(2)], T0 + timedelta(seconds=3))
        recorder.db.close()  # Simulate an abrupt process exit without a clean close.
        info = journal.status(self.path)
        self.assertEqual(info["observations"], 1)
        self.assertEqual(info["unfinished_sessions"], 1)
        self.assertEqual(info["sessions"][0]["batches"], 1)

    def test_private_directory_and_symlinked_database_are_refused(self):
        if os.name != "posix":
            self.skipTest("POSIX permissions test")
        broad = self.root / "shared"
        broad.mkdir(mode=0o755)
        broad.chmod(0o755)
        with self.assertRaisesRegex(journal.RecordingError, "private"):
            journal.Recorder(broad / "history.sqlite3")
        target = self.root / "target"
        target.write_text("keep")
        self.path.symlink_to(target)
        with self.assertRaisesRegex(journal.RecordingError, "regular file"):
            journal.Recorder(self.path)
        self.assertEqual(target.read_text(), "keep")

    def test_recording_is_included_in_installed_package_and_rejects_rust_or_no_feeds(self):
        self.assertIn("scripts/observation_history.py", install.PACKAGE)
        for options in (["--record-observations"],
                        ["--record-observations", "--feeds", "missing", "--engine", "rust"]):
            result = subprocess.run([sys.executable, str(SCRIPTS / "operate.py"), "up", "--repo", str(self.root),
                                     *options], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("requires", result.stdout)
            self.assertFalse((self.root / ".ontologyex").exists())

    def test_installed_package_can_inspect_history(self):
        installed = Path(install.install(self.root)["path"])
        recorder = journal.Recorder(self.path)
        recorder.record([sample(0)], T0 + timedelta(seconds=1))
        recorder.close()
        result = subprocess.run([sys.executable, str(installed / "scripts/observation_history.py"),
                                 "status", "--db", str(self.path)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["observations"], 1)

    def test_killed_recorder_leaves_an_unfinished_session_after_restart(self):
        code = ("import sys,time; sys.path.insert(0,sys.argv[1]); "
                "import observation_history as j; recorder=j.Recorder(j.Path(sys.argv[2])); time.sleep(60)")
        process = subprocess.Popen([sys.executable, "-c", code, str(SCRIPTS), str(self.path)])
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    if journal.status(self.path)["unfinished_sessions"] == 1:
                        break
                except (OSError, sqlite3.Error, journal.RecordingError):
                    pass
                time.sleep(0.05)
            else:
                self.fail("recorder did not start")
            process.kill()
            process.wait(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        recorder = journal.Recorder(self.path)
        recorder.close()
        info = journal.status(self.path)
        self.assertEqual(info["unfinished_sessions"], 1)
        self.assertEqual(info["sessions"][-1]["state"], "closed")


class RecorderEndToEndTests(unittest.TestCase):
    def test_live_feed_evicts_old_readings_but_journal_survives_restart_and_replays(self):
        class PrometheusFixture(BaseHTTPRequestHandler):
            value = "720"
            def do_GET(self):
                data = json.dumps({"status": "success", "data": {"resultType": "vector",
                                  "result": [{"value": [0, self.value]}]}}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), PrometheusFixture)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            (repo / "compose.yml").write_text("services:\n  payments: {}\n")
            config = root / "feeds.json"
            config.write_text(json.dumps({"schema_version": 1, "kind": "ONTOLOGYEX_FEEDS",
                "output": "observations.jsonl", "allowed_hosts": ["127.0.0.1"], "keep_per_component": 2,
                "prometheus": {"url": f"http://127.0.0.1:{server.server_address[1]}", "interval_seconds": 5,
                               "targets": [{"component": COMPONENT, "p95_latency_ms": "latency"}]}}))
            thresholds = root / "thresholds.json"
            thresholds.write_text(json.dumps({"schema_version": 1, "warning_above": {"p95_latency_ms": 500}}))
            db = repo / ".ontologyex/observation-history/history.sqlite3"
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            command = [sys.executable, str(SCRIPTS / "operate.py"), "up", "--repo", str(repo),
                       "--feeds", str(config), "--thresholds", str(thresholds), "--port", str(port),
                       "--no-history", "--record-observations"]
            def launch():
                return subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            def await_samples(process, count):
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    self.assertIsNone(process.poll(), "launcher unexpectedly exited")
                    try:
                        info = journal.status(db)
                        if info["observations"] >= count:
                            return info
                    except (OSError, sqlite3.Error, journal.RecordingError):
                        pass
                    time.sleep(0.1)
                self.fail(f"did not record {count} readings")
            process = launch()
            try:
                await_samples(process, 3)
                self.assertEqual(len(control_room.read_observations(root / "observations.jsonl")), 2)
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/view?lens=services", timeout=5) as response:
                    view = json.load(response)
                node = next(n for n in view["nodes"] if n["id"] == COMPONENT)
                self.assertEqual(node["status"], "warning")
                self.assertEqual(node["leading_indicators"][0]["current"], 720)
            finally:
                process.terminate()
                process.wait(timeout=15)
            self.assertEqual(journal.status(db)["unfinished_sessions"], 0)
            process = launch()
            try:
                await_samples(process, 4)
            finally:
                process.terminate()
                process.wait(timeout=15)
            info = journal.status(db)
            self.assertEqual(len(info["sessions"]), 2)
            self.assertEqual(info["unfinished_sessions"], 0)
            with sqlite3.connect(db) as database:
                earliest, latest = database.execute("SELECT MIN(observed_at),MAX(available_at) FROM observations").fetchone()
            start = control_room.timestamp(earliest).replace(microsecond=0)
            last = control_room.timestamp(latest)
            end = last.replace(microsecond=0) + timedelta(seconds=1)
            dataset = journal.export_case(db, name="synthetic-live", component=COMPONENT, metric="p95_latency_ms",
                start=journal.iso(start), end=journal.iso(end), follow_up_end=journal.iso(end + timedelta(seconds=60)),
                step_seconds=1, threshold=500, selected_at=journal.iso(start - timedelta(seconds=1)))
            result = history_replay.run(dataset)["cases"][0]
            self.assertGreaterEqual(len(dataset["cases"][0]["observations"]), 4)
            self.assertEqual(result["policies"]["two_samples"]["outcome"], "needs_review")
            self.assertIsNotNone(result["policies"]["two_samples"]["first_warning_at"])
            self.assertEqual(result["policies"]["two_samples"]["coverage"], "partial_or_unknown")
            # A real database write failure must stop the feed and remain visible.
            with sqlite3.connect(db) as database:
                database.execute("CREATE TRIGGER fail_insert BEFORE INSERT ON observations "
                                 "BEGIN SELECT RAISE(ABORT, 'simulated storage failure'); END")
            PrometheusFixture.value = "730"
            result = subprocess.run([sys.executable, str(SCRIPTS / "feeds.py"), "once", "--config", str(config),
                                     "--record-history", str(db)], capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("simulated storage failure", result.stdout)
            self.assertEqual(journal.status(db)["failed_sessions"], 1)
            self.assertEqual(journal.status(db)["sessions"][-1]["state"], "failed")


if __name__ == "__main__":
    unittest.main()
