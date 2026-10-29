"""Background scans: slow reads off the request path, and a result kept only if the repository held still."""
import importlib.util
import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ontology-extraction/scripts"))
import background_scans as bs

spec = importlib.util.spec_from_file_location("ontologyex_scans_demo", ROOT / "examples/operating-picture/demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Revisions:
    """A fingerprint the test scripts: each call returns the next revision, then the last one forever."""

    def __init__(self, *values):
        self.values = list(values)

    def __call__(self, repo):
        return self.values.pop(0) if len(self.values) > 1 else self.values[0]


class ScannerTests(unittest.TestCase):
    def scanner(self, revisions, run=lambda: "result", interval=60):
        clock = Clock()
        scanner = bs.Scanner(Path("."), [("job", interval, run)], fingerprint=revisions, clock=clock)
        return scanner, clock

    def test_a_result_is_kept_only_if_the_repository_held_still(self):
        scanner, _ = self.scanner(Revisions("r1", "r1", "r1"))
        self.assertEqual(scanner.run_pending(), [("job", "COMMITTED")])
        self.assertEqual(scanner.result("job"), ("result", None))
        self.assertEqual(scanner.status()["scans"][0]["revision"], "r1")

    def test_a_change_during_the_scan_discards_it_and_keeps_the_last_good_one(self):
        runs = iter(["old", "mixed", "new"])
        scanner, clock = self.scanner(Revisions("r1", "r1", "r1",  # pass 1: tick, before, after -> commit "old"
                                                "r2", "r2", "r3",  # pass 2: changed since; scan sees r2 then r3
                                                "r3", "r3", "r3"), run=lambda: next(runs))
        scanner.run_pending()
        clock.now += 10
        self.assertEqual(scanner.run_pending(), [("job", "DISCARDED_REPOSITORY_CHANGED")])
        self.assertEqual(scanner.result("job")[0], "old")  # the half-read result never replaced the good one
        self.assertEqual(scanner.status()["scans"][0]["discarded_repository_changed"], 1)
        clock.now += 1
        self.assertEqual(scanner.run_pending(), [("job", "COMMITTED")])  # retried at once, now consistent
        self.assertEqual(scanner.result("job")[0], "new")

    def test_a_repository_that_never_holds_still_is_retried_then_left_to_its_interval(self):
        counter = iter(range(1000))
        scanner, clock = self.scanner(lambda repo: f"r{next(counter)}")  # every look sees a new revision
        for _ in range(bs.RETRY_AFTER_CHANGE):
            self.assertEqual(scanner.run_pending(), [("job", "DISCARDED_REPOSITORY_CHANGED")])
        self.assertEqual(scanner.run_pending(), [])  # backs off instead of spinning
        clock.now += 61
        self.assertEqual(len(scanner.run_pending()), 1)

    def test_interval_and_early_rerun_when_the_repository_changed(self):
        scanner, clock = self.scanner(Revisions("r1", "r1", "r1", "r1"))
        scanner.run_pending()
        clock.now += 30
        self.assertEqual(scanner.run_pending(), [])  # unchanged and not yet due
        scanner.fingerprint = Revisions("r2")
        self.assertEqual(scanner.run_pending(), [("job", "COMMITTED")])  # the repository changed: read it again
        self.assertEqual(scanner.status()["scans"][0]["revision"], "r2")

    def test_an_error_is_a_result_too(self):
        def broken():
            raise OSError("unreadable")
        scanner, _ = self.scanner(Revisions("r1"), run=broken)
        self.assertEqual(scanner.run_pending(), [("job", "COMMITTED_ERROR")])
        self.assertEqual(scanner.result("job"), (None, "OSError: unreadable"))

    def test_the_first_request_waits_for_the_first_result_and_later_ones_do_not(self):
        release = threading.Event()
        scanner, _ = self.scanner(Revisions("r1"), run=lambda: release.wait(5) and "done")
        worker = threading.Thread(target=scanner.run_pending)
        worker.start()
        threading.Timer(0.2, release.set).start()
        started = time.monotonic()
        self.assertEqual(scanner.result("job")[0], "done")
        self.assertGreaterEqual(time.monotonic() - started, 0.15)
        worker.join()
        started = time.monotonic()
        scanner.result("job")
        self.assertLess(time.monotonic() - started, 0.05)

    def test_revision_follows_files_and_git_but_not_dependencies(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "app.py").write_text("x = 1\n")
            (repo / "node_modules/pkg").mkdir(parents=True)
            first = bs.revision(repo)
            (repo / "node_modules/pkg/index.js").write_text("dependency")
            self.assertEqual(bs.revision(repo), first)
            (repo / "app.py").write_text("x = 22\n")
            second = bs.revision(repo)
            self.assertNotEqual(second, first)
            (repo / ".git").mkdir()
            (repo / ".git/HEAD").write_text("ref: refs/heads/main\n")
            self.assertNotEqual(bs.revision(repo), second)


class ServedTests(unittest.TestCase):
    def test_scans_are_reported_and_pages_get_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            built = demo.build(Path(tmp) / "demo")
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            process = subprocess.Popen([*built["command"], "--port", str(port)],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.addCleanup(lambda: (process.terminate(), process.wait(timeout=5)))
            base = f"http://127.0.0.1:{port}"
            for _ in range(150):
                try:
                    urllib.request.urlopen(base + "/api/state", timeout=10).close()
                    break
                except OSError:
                    time.sleep(0.1)
            with urllib.request.urlopen(base + "/api/compliance", timeout=60) as resp:
                self.assertEqual(json.load(resp)["status"], "OK")  # the first request had its result
            with urllib.request.urlopen(base + "/api/scans", timeout=10) as resp:
                scans = {s["scan"]: s for s in json.load(resp)["scans"]}
            self.assertEqual(set(scans), {"code", "suppliers", "ownership", "compliance"})
            for name, row in scans.items():
                self.assertTrue(row["revision"] and row["committed_at"], name)
                self.assertIsNone(row["error"], name)
                self.assertTrue(row["describes_current_repository"], name)


if __name__ == "__main__":
    unittest.main()
