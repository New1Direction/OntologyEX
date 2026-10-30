"""Live pulls: the declared read-only API pulls, run on their intervals, and what the page says about them."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "ontology-extraction/scripts"
sys.path.insert(0, str(SCRIPTS))
import command_center
import operate
import operations as ops
import pulls

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
KEY = "rk_test_51PullKeyNeverWritten"


class Stripe(BaseHTTPRequestHandler):
    keys: list = []

    def do_GET(self):
        Stripe.keys.append(self.headers.get("Authorization"))
        path = self.path.partition("?")[0]
        t = int(datetime(2026, 10, 6, tzinfo=timezone.utc).timestamp())
        data = [{"object": "charge", "id": f"ch_{i}abc12", "amount": 1500, "currency": "usd", "created": t,
                 "status": "succeeded", "receipt_email": f"c{i}@x.test"} for i in range(4)] \
            if path == "/v1/charges" else []
        body = json.dumps({"object": "list", "data": data, "has_more": False}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class Folder(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.dir)

    def declare(self, *entries):
        (self.dir / pulls.CONFIG).write_text(json.dumps({"pulls": list(entries)}), "utf-8")


class ConfigTests(Folder):
    def test_defaults_and_names(self):
        self.declare({"provider": "stripe"}, {"provider": "aws", "every_minutes": 360, "profile": "billing-read"},
                     {"provider": "zendesk", "subdomain": "acme"}, {"provider": "hubspot", "key_env": "CRM_TOKEN"})
        found = {p["name"]: p for p in pulls.load(self.dir)}
        self.assertEqual(sorted(found), ["aws", "hubspot", "stripe", "zendesk:acme"])
        self.assertEqual((found["stripe"]["key_env"], found["stripe"]["days"], found["stripe"]["every_minutes"]),
                         ("STRIPE_API_KEY", 180, 60))
        self.assertEqual((found["aws"]["months"], found["aws"]["profile"]), (6, "billing-read"))
        self.assertEqual(found["hubspot"]["key_env"], "CRM_TOKEN")

    def test_refusals(self):
        cases = [
            ({"provider": "paypal"}, "provider is one of"),
            ({"provider": "stripe", "key": KEY}, "unknown field"),  # a key in the file is refused, not stored
            ({"provider": "stripe", "key_env": KEY}, "names an environment variable"),
            ({"provider": "stripe", "every_minutes": 5}, "every_minutes"),
            ({"provider": "stripe", "every_minutes": "60"}, "every_minutes"),
            ({"provider": "stripe", "days": 0}, "days is 1 to"),
            ({"provider": "aws", "months": 99}, "months is 1 to"),
            ({"provider": "aws", "profile": "a b; rm -rf"}, "profile"),
            ({"provider": "zendesk"}, "subdomain"),
            ({"provider": "zendesk", "subdomain": "evil.example.com/"}, "subdomain"),
            ({"provider": "stripe", "api_base": "https://collector.example"}, "this machine"),
            ({"provider": "stripe", "api_base": "http://127.0.0.1.example.com"}, "this machine"),
        ]
        for entry, expected in cases:
            with self.subTest(entry=entry):
                self.declare(entry)
                with self.assertRaises(pulls.PullConfigError) as caught:
                    pulls.load(self.dir)
                self.assertIn(expected, str(caught.exception))
                self.assertNotIn(KEY, str(caught.exception))
        self.declare({"provider": "stripe"}, {"provider": "stripe", "days": 30})
        with self.assertRaisesRegex(pulls.PullConfigError, "declared twice"):
            pulls.load(self.dir)
        (self.dir / pulls.CONFIG).write_text("[]")
        with self.assertRaisesRegex(pulls.PullConfigError, "expected"):
            pulls.load(self.dir)
        (self.dir / pulls.CONFIG).unlink()
        self.assertEqual(pulls.load(self.dir), [])

    def test_the_files_are_not_records(self):
        self.declare({"provider": "stripe"})
        pulls.write_status(self.dir, {})
        self.assertEqual(ops.files_in(self.dir), [])
        before = ops.folder_revision(self.dir)
        pulls.write_status(self.dir, {"stripe": {"ok": True}})  # the status changing does not re-read the ledger
        self.assertEqual(ops.folder_revision(self.dir), before)


class ScheduleTests(Folder):
    def runner(self, fail=()):
        calls = []

        def run(entry, folder, now, entities, environ):
            calls.append((entry["name"], now))
            if entry["name"] in fail:
                raise ops.OperationsError(f"/v1/charges: HTTP 401 for Bearer {environ.get('STRIPE_API_KEY')}")
            return {"file": str(folder / f"{entry['provider']}.ontologyex-aggregate.json"),
                    "objects_read": {"payments": 4}}
        return run, calls

    def test_each_pull_on_its_interval(self):
        self.declare({"provider": "stripe", "every_minutes": 60}, {"provider": "hubspot", "every_minutes": 240})
        run, calls = self.runner()
        env = {"STRIPE_API_KEY": KEY}
        self.assertEqual([d["pull"] for d in pulls.run_due(self.dir, NOW, environ=env, runner=run)], ["stripe", "hubspot"])
        self.assertEqual(pulls.run_due(self.dir, NOW + timedelta(minutes=59), environ=env, runner=run), [])
        later = pulls.run_due(self.dir, NOW + timedelta(minutes=60), environ=env, runner=run)
        self.assertEqual([d["pull"] for d in later], ["stripe"])
        self.assertEqual(len(pulls.run_due(self.dir, NOW + timedelta(minutes=61), environ=env, runner=run, force=True)), 2)
        status = pulls.read_status(self.dir)
        self.assertEqual(status["stripe"]["last_success"], "2026-10-29T13:01:00Z")
        self.assertEqual(status["stripe"]["next_due"], "2026-10-29T14:01:00Z")
        self.assertEqual((status["hubspot"]["objects_read"], status["hubspot"]["file"]),
                         ({"payments": 4}, "hubspot.ontologyex-aggregate.json"))
        self.assertEqual(json.loads((self.dir / pulls.STATUS).read_text())["kind"], "ONTOLOGYEX_PULLS_STATUS")

    def test_a_failure_is_recorded_masked_and_stops_nothing_else(self):
        self.declare({"provider": "stripe"}, {"provider": "hubspot"})
        run, calls = self.runner(fail={"stripe"})
        done = pulls.run_due(self.dir, NOW, environ={"STRIPE_API_KEY": KEY}, runner=run)
        self.assertEqual([(d["pull"], d["ok"]) for d in done], [("stripe", False), ("hubspot", True)])
        status = (self.dir / pulls.STATUS).read_text()
        self.assertNotIn(KEY, status)
        self.assertIn("HTTP 401", pulls.read_status(self.dir)["stripe"]["detail"])
        # A later success keeps the time of the last one, and clears the error.
        run, _ = self.runner()
        pulls.run_due(self.dir, NOW + timedelta(hours=1), environ={"STRIPE_API_KEY": KEY}, runner=run)
        self.assertEqual((pulls.read_status(self.dir)["stripe"]["ok"], pulls.read_status(self.dir)["stripe"]["detail"]),
                         (True, None))

    def test_scrub(self):
        self.assertEqual(pulls.scrub(f"denied for {KEY}", [KEY]), "denied for …")
        self.assertNotIn("abcdefghijklmnop", pulls.scrub("Authorization: Bearer abcdefghijklmnopqrstu", []))
        self.assertNotIn("zzzzzzzzzzzzzzzz", pulls.scrub("token=zzzzzzzzzzzzzzzzzzzz", []))
        self.assertLessEqual(len(pulls.scrub("x" * 1000, [])), 300)


class HealthTests(Folder):
    def test_states_signals_and_the_page_data(self):
        self.assertIsNone(pulls.health(self.dir, NOW))
        self.declare({"provider": "stripe", "every_minutes": 60}, {"provider": "hubspot", "every_minutes": 60},
                     {"provider": "aws", "every_minutes": 60}, {"provider": "zendesk", "subdomain": "acme"})
        pulls.write_status(self.dir, {
            "stripe": {"ok": True, "last_attempt": "2026-10-29T11:30:00Z", "last_success": "2026-10-29T11:30:00Z"},
            "hubspot": {"ok": True, "last_attempt": "2026-10-29T09:00:00Z", "last_success": "2026-10-29T09:00:00Z"},
            "aws": {"ok": False, "last_attempt": "2026-10-29T11:50:00Z", "detail": "OperationsError: AccessDenied"}})
        live = pulls.health(self.dir, NOW)
        self.assertEqual({r["pull"]: r["state"] for r in live["pulls"]},
                         {"stripe": "FRESH", "hubspot": "STALE", "aws": "FAILING", "zendesk:acme": "NOT_RUN_YET"})
        self.assertEqual(sorted((s["signal"], s["level"]) for s in live["signals"]),
                         [("PULL_FAILING", "HIGH"), ("PULL_STALE", "MEDIUM")])
        answer = ops.report(self.dir, now=NOW)  # no records yet: the pulls still show
        self.assertEqual(answer["status"], "NO_RECORDS_READ")
        self.assertEqual(len(answer["live"]["pulls"]), 4)
        self.assertIn("PULL_FAILING", {s["signal"] for s in answer["signals"]})
        self.assertIn("| aws | FAILING |", ops.markdown(answer))
        # Read again on each request: the same cached ledger, three hours later, says stripe is out of date.
        later = ops.with_live(answer, self.dir, NOW + timedelta(hours=3))
        self.assertEqual({r["pull"]: r["state"] for r in later["live"]["pulls"]}["stripe"], "STALE")
        self.assertEqual(sum(s["signal"] == "PULL_FAILING" for s in later["signals"]), 1)
        items = command_center.report({"operations": later, "state": {"nodes": [], "edges": []}})["items"]
        kinds = {i["kind"]: i for i in items}
        self.assertEqual(kinds["PULL_FAILING"]["category"], "Operations")
        self.assertIn("aws", kinds["PULL_FAILING"]["title"])

    def test_a_broken_file_is_a_signal(self):
        (self.dir / pulls.CONFIG).write_text('{"pulls": [{"provider": "stripe", "every_minutes": 1}]}')
        live = pulls.health(self.dir, NOW)
        self.assertEqual(live["signals"][0]["signal"], "PULLS_MISCONFIGURED")
        self.assertIn("PULLS_MISCONFIGURED", {i["kind"] for i in command_center.report({"operations": ops.report(self.dir, now=NOW), "state": {"nodes": [], "edges": []}})["items"]})


class EndToEndTests(Folder):
    def setUp(self):
        super().setUp()
        Stripe.keys = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Stripe)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def test_a_scheduled_stripe_pull_writes_aggregates_the_ledger_reads(self):
        self.declare({"provider": "stripe", "key_env": "TEST_STRIPE_READ_KEY", "api_base": self.base, "days": 90})
        done = pulls.run_due(self.dir, NOW, environ={"TEST_STRIPE_READ_KEY": KEY})
        self.assertEqual(done, [{"pull": "stripe", "ok": True, "detail": None}])
        self.assertEqual(Stripe.keys[0], f"Bearer {KEY}")
        for path in self.dir.iterdir():
            text = path.read_text()
            for needle in (KEY, "@x.test", "ch_0"):
                self.assertNotIn(needle, text, path.name)
        answer = ops.report(self.dir, min_cell=1, now=NOW)
        self.assertEqual(answer["currencies"]["usd"]["totals"]["payments"]["gross"], 60.0)
        self.assertEqual(answer["live"]["pulls"][0]["state"], "FRESH")
        self.assertEqual(answer["live"]["pulls"][0]["objects_read"], {"payments": 4})

    def test_a_missing_or_secret_key_fails_without_echoing_it(self):
        self.declare({"provider": "stripe", "key_env": "TEST_STRIPE_READ_KEY", "api_base": self.base})
        done = pulls.run_due(self.dir, NOW, environ={})
        self.assertIn("TEST_STRIPE_READ_KEY is not set", done[0]["detail"])
        secret = "sk_live_51SecretNeverWritten"
        done = pulls.run_due(self.dir, NOW, environ={"TEST_STRIPE_READ_KEY": secret}, force=True)
        self.assertIn("secret key", done[0]["detail"])
        self.assertNotIn(secret, (self.dir / pulls.STATUS).read_text())
        self.assertEqual(Stripe.keys, [])  # refused before anything was sent

    def test_the_puller_thread(self):
        self.declare({"provider": "stripe", "key_env": "TEST_STRIPE_READ_KEY", "api_base": self.base})
        puller = pulls.Puller(self.dir)
        with mock.patch.dict(os.environ, {"TEST_STRIPE_READ_KEY": KEY}):
            puller.start()
            for _ in range(100):
                if (self.dir / "stripe-api.ontologyex-aggregate.json").exists():
                    break
                time.sleep(0.05)
            puller.stop()
        self.assertFalse(puller.thread.is_alive())
        self.assertTrue(pulls.read_status(self.dir)["stripe"]["ok"])
        self.assertEqual(puller.tick(), [])  # not due again for an hour

    def test_pull_all_from_the_command_line(self):
        self.declare({"provider": "stripe", "key_env": "TEST_STRIPE_READ_KEY", "api_base": self.base})
        env = {**os.environ, "TEST_STRIPE_READ_KEY": KEY}
        run = subprocess.run([sys.executable, str(SCRIPTS / "operations.py"), "pull", "all", "--dir", str(self.dir)],
                             capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)["pulls"][0]["ok"], True)
        self.assertNotIn(KEY, run.stdout + run.stderr)
        (self.dir / pulls.CONFIG).unlink()
        run = subprocess.run([sys.executable, str(SCRIPTS / "operations.py"), "pull", "all", "--dir", str(self.dir)],
                             capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(run.returncode, 2)
        self.assertIn("no pulls.json", run.stderr)

    def test_aws_with_a_profile(self):
        bin_dir = self.dir / "bin"
        bin_dir.mkdir()
        log = self.dir / "args.txt"
        (bin_dir / "aws").write_text(f'#!/bin/sh\necho "$@" > {log}\necho \'{{"ResultsByTime": []}}\'\n')
        (bin_dir / "aws").chmod(0o755)
        ops_dir = self.dir / "ops"
        ops_dir.mkdir()
        (ops_dir / pulls.CONFIG).write_text(json.dumps({"pulls": [{"provider": "aws", "profile": "billing-read"}]}))
        with mock.patch.dict(os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}):
            done = pulls.run_due(ops_dir, NOW)
        self.assertTrue(done[0]["ok"], done)
        self.assertIn("--profile billing-read", log.read_text())


class StartTests(Folder):
    def test_operate_passes_pulls_and_the_control_room_refuses_a_bad_file(self):
        args = operate.main.__globals__["argparse"].Namespace(
            repo=self.dir, port=8790, dependencies=None, feeds=None, skill=[], links=None, events=None,
            allow_link_decisions=False, pulls=True, no_history=True, history=None)
        self.assertIn("--pulls", operate.control_room_command(args, None))
        folder = self.dir / ".ontologyex" / "operations"
        folder.mkdir(parents=True)
        (folder / pulls.CONFIG).write_text('{"pulls": [{"provider": "stripe", "key": "sk_live_x"}]}')
        run = subprocess.run([sys.executable, str(SCRIPTS / "control_room.py"), "--repo", str(self.dir), "--port", "0",
                              "--pulls"], capture_output=True, text=True, timeout=120)
        self.assertEqual(run.returncode, 1)
        self.assertIn("unknown field", run.stdout)
        self.assertNotIn("sk_live_x", run.stdout)


if __name__ == "__main__":
    unittest.main()
