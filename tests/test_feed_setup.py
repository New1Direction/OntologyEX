"""Feed setup from a repository's declarations, and the one-command launcher.

`feed_setup.py propose` must back every candidate with the line that declares it and
turn everything else into a gap; `init` must write only what was accepted, refuse
what it cannot stand behind, and produce a config `feeds.py` accepts. `operate.py up`
must bring the feeds and the control room up together and take both down together.
"""
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "ontology-extraction/scripts"
sys.path.insert(0, str(SCRIPTS))
import feed_setup
import feeds

COMPOSE = """\
services:
  api:
    image: shop/api:1.4
    ports: ["{api_port}:8080"]
    environment:
      OTEL_SERVICE_NAME: shop-api
      OTEL_EXPORTER_OTLP_ENDPOINT: http://collector:4318
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8080/healthz"]
      interval: 15s
  payments:
    image: shop/payments:2.0
    ports:
      - target: 9000
        published: "19000"
    environment:
      - OTEL_RESOURCE_ATTRIBUTES=service.name=payments,deployment.environment=prod
    healthcheck:
      test: wget -q --spider http://127.0.0.1:9000/ready || exit 1
  worker:
    image: shop/worker
    environment:
      OTEL_EXPORTER_OTLP_ENDPOINT: http://collector:4318
  db:
    image: postgres:16
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U shop"]
  web:
    image: shop/web
    ports: ["8081:80"]
  admin:
    image: shop/admin
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:7000/health"]
  prometheus:
    image: prom/prometheus:v3.1.0
    ports: ["9090:9090"]
    volumes: ["./ops/prometheus.yml:/etc/prometheus/prometheus.yml:ro"]
"""

PROMETHEUS = """\
scrape_configs:
  - job_name: prometheus
    static_configs:
      - targets: ["localhost:9090"]
  - job_name: shop
    static_configs:
      - targets: ["api:8080", "payments:9000"]
"""

API = "discovery:service:compose.yml:api"
PAYMENTS = "discovery:service:compose.yml:payments"


def make_repo(root: Path, api_port: int = 18080) -> Path:
    repo = root / "repo"
    (repo / "ops").mkdir(parents=True)
    (repo / "compose.yml").write_text(COMPOSE.format(api_port=api_port))
    (repo / "ops/prometheus.yml").write_text(PROMETHEUS)
    return repo


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Propose(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.repo = make_repo(Path(cls.tmp.name))
        cls.proposal = feed_setup.propose(cls.repo)
        cls.by_id = {c["id"]: c for c in cls.proposal["candidates"]}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_candidates_and_strengths(self):
        self.assertEqual({i: c["strength"] for i, c in self.by_id.items()}, {
            "probe:compose.yml:api": "STRONG",
            "probe:compose.yml:payments": "STRONG",
            "otlp:compose.yml:api": "STRONG",
            "otlp:compose.yml:payments": "STRONG",
            "otlp:compose.yml:worker": "WEAK",
            "prometheus:compose.yml:api:shop": "STRONG",
            "prometheus:compose.yml:payments:shop": "STRONG",
        })
        self.assertEqual(self.proposal["semantics"], "PROPOSALS_FROM_DECLARATIONS_A_PERSON_CHOOSES_WHAT_TO_MONITOR")

    def test_entries_use_host_ports_on_loopback(self):
        self.assertEqual(self.by_id["probe:compose.yml:api"]["entry"],
                         {"component": API, "url": "http://127.0.0.1:18080/healthz", "interval_seconds": 15})
        self.assertEqual(self.by_id["probe:compose.yml:payments"]["entry"],
                         {"component": PAYMENTS, "url": "http://127.0.0.1:19000/ready", "interval_seconds": 30})
        self.assertEqual(self.by_id["otlp:compose.yml:api"]["entry"], {"service": "shop-api", "component": API})
        self.assertEqual(self.by_id["otlp:compose.yml:payments"]["entry"]["service"], "payments")
        prom = self.by_id["prometheus:compose.yml:payments:shop"]["entry"]
        self.assertEqual(prom["url"], "http://127.0.0.1:9090")
        self.assertEqual(prom["target"], {"component": PAYMENTS,
                                          "up": 'min(up{job="shop",instance="payments:9000"})'})

    def test_evidence_points_at_the_declaring_line(self):
        expected = {
            "probe:compose.yml:api": ("compose.yml", 9, "curl"),
            "probe:compose.yml:payments": ("compose.yml", 19, "wget"),
            "otlp:compose.yml:api": ("compose.yml", 6, "OTEL_SERVICE_NAME"),
            "otlp:compose.yml:payments": ("compose.yml", 17, "OTEL_RESOURCE_ATTRIBUTES"),
            "otlp:compose.yml:worker": ("compose.yml", 23, "OTEL_EXPORTER_OTLP_ENDPOINT"),
            "prometheus:compose.yml:api:shop": ("ops/prometheus.yml", 5, "job_name: shop"),
        }
        for cid, (path, line, text) in expected.items():
            evidence = self.by_id[cid]["evidence"]
            self.assertEqual((evidence["path"], evidence["line"]), (path, line), cid)
            self.assertIn(text, evidence["text"], cid)

    def test_undeclared_things_are_gaps_not_guesses(self):
        gaps = {g["source"]: g["reason"] for g in self.proposal["gaps"]}
        self.assertIn("not published", gaps["compose.yml:admin"])
        self.assertIn("not an HTTP check", gaps["compose.yml:db"])
        self.assertIn("no healthcheck", gaps["compose.yml:web"])
        self.assertNotIn("compose.yml:worker", gaps)  # a WEAK candidate, not a gap
        self.assertFalse(any(c["component"].endswith(":web") for c in self.proposal["candidates"]))

    def test_propose_writes_nothing(self):
        before = sorted(p.relative_to(self.repo) for p in self.repo.rglob("*"))
        feed_setup.propose(self.repo)
        self.assertEqual(sorted(p.relative_to(self.repo) for p in self.repo.rglob("*")), before)

    def test_otlp_port_avoids_a_published_4318(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "compose.yml").write_text(
                "services:\n  collector:\n    image: otel/collector\n    ports: ['4318:4318']\n")
            self.assertEqual(feed_setup.propose(repo)["otlp_port"], 14318)
        self.assertEqual(self.proposal["otlp_port"], 4318)


class Fields(unittest.TestCase):
    def test_published_ports(self):
        self.assertEqual(feed_setup.published_ports([
            "18080:8080", "127.0.0.1:9001:9000", "[::1]:7001:7000", "0.0.0.0:6001:6000/tcp",
            "10.0.0.5:5001:5000", "4001:4000/udp", "3000-3001:3000-3001", "2000", 2500,
            {"target": 1000, "published": "11000"}, {"target": 1100, "published": 11100, "host_ip": "10.1.1.1"},
            {"target": 1200, "published": "11200", "protocol": "udp"},
        ]), {8080: 18080, 9000: 9001, 7000: 7001, 6000: 6001, 1000: 11000})
        self.assertEqual(feed_setup.published_ports(None), {})

    def test_durations(self):
        self.assertEqual(feed_setup.seconds("15s"), 15)
        self.assertEqual(feed_setup.seconds("1m30s"), 90)
        self.assertEqual(feed_setup.seconds("1h"), 3600)
        self.assertEqual(feed_setup.seconds("2h"), 3600)
        self.assertEqual(feed_setup.seconds("500ms"), 30)  # no whole seconds: default
        self.assertEqual(feed_setup.seconds("1s"), 5)
        self.assertEqual(feed_setup.seconds("soon"), 30)
        self.assertEqual(feed_setup.seconds(None), 30)

    def test_environment_forms(self):
        self.assertEqual(feed_setup.environment({"A": "1", "B": 2}), {"A": "1"})
        self.assertEqual(feed_setup.environment(["A=1", "B"]), {"A": "1"})

    def test_deploy_time_service_name_is_a_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "compose.yml").write_text(
                "services:\n  api:\n    image: x\n    environment:\n      OTEL_SERVICE_NAME: ${NAME}\n")
            proposal = feed_setup.propose(repo)
        self.assertEqual(proposal["candidates"], [])
        self.assertIn("deploy time", proposal["gaps"][0]["reason"])


class Init(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = make_repo(Path(self.tmp.name))
        self.out = Path(self.tmp.name) / "feeds.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_accept_strong_writes_a_valid_config(self):
        result = feed_setup.init(self.repo, self.out, [], True, False)
        self.assertEqual(result["status"], "WRITTEN")
        self.assertIn("otlp:compose.yml:worker", result["not_accepted"])
        config = json.loads(self.out.read_text())
        feeds.load_config(self.out)
        self.assertEqual(config["allowed_hosts"], ["127.0.0.1"])
        self.assertEqual(len(config["probes"]), 2)
        self.assertEqual(config["otlp"], {"bind": "127.0.0.1", "port": 4318,
                                          "services": {"shop-api": API, "payments": PAYMENTS}})
        self.assertEqual(len(config["prometheus"]["targets"]), 2)

    def test_accept_by_id_writes_only_those(self):
        feed_setup.init(self.repo, self.out, ["probe:compose.yml:api", "otlp:compose.yml:worker"], False, False)
        config = json.loads(self.out.read_text())
        self.assertEqual([p["component"] for p in config["probes"]], [API])
        self.assertEqual(config["otlp"]["services"], {"worker": "discovery:service:compose.yml:worker"})
        self.assertNotIn("prometheus", config)

    def test_refusals_leave_nothing_behind(self):
        for accept, strong, message in ((["probe:compose.yml:nope"], False, "no such candidate"),
                                        ([], False, "nothing accepted")):
            with self.assertRaisesRegex(feed_setup.SetupError, message):
                feed_setup.init(self.repo, self.out, accept, strong, False)
        self.assertEqual(list(Path(self.tmp.name).glob("*.json")), [])
        self.out.write_text("{}")
        with self.assertRaisesRegex(feed_setup.SetupError, "--force"):
            feed_setup.init(self.repo, self.out, [], True, False)
        self.assertEqual(self.out.read_text(), "{}")
        feed_setup.init(self.repo, self.out, [], True, True)
        feeds.load_config(self.out)
        self.assertEqual([p.name for p in Path(self.tmp.name).iterdir() if p.name.startswith(".feeds-")], [])

    def test_cli(self):
        run = subprocess.run([sys.executable, str(SCRIPTS / "feed_setup.py"), "propose", "--repo", str(self.repo)],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)["kind"], "FEED_SETUP_PROPOSAL")
        run = subprocess.run([sys.executable, str(SCRIPTS / "feed_setup.py"), "init", "--repo", str(self.repo),
                              "--out", str(self.out)], capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 1)
        self.assertEqual(json.loads(run.stdout)["status"], "ERROR")


class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == "/healthz" else 404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_):
        pass


def accepts(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


class OperateUp(unittest.TestCase):
    def test_up_shows_live_health_and_stops_both(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Health)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp), api_port=server.server_address[1])
            config = Path(tmp) / "feeds.json"
            feed_setup.init(repo, config, ["probe:compose.yml:api", "otlp:compose.yml:api"], False, False)
            data = json.loads(config.read_text())
            data["otlp"]["port"] = otlp = free_port()
            config.write_text(json.dumps(data))
            port = free_port()
            process = subprocess.Popen(
                [sys.executable, str(SCRIPTS / "operate.py"), "up", "--repo", str(repo), "--feeds", str(config),
                 "--port", str(port), "--engine", "python"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, cwd=tmp)
            lines: list[str] = []
            reader = threading.Thread(target=lambda: lines.extend(process.stdout), daemon=True)
            reader.start()
            try:
                status, deadline = None, time.monotonic() + 30
                while status is None and time.monotonic() < deadline:
                    text = list(lines)
                    if "{\n" in text and "}\n" in text[text.index("{\n"):]:
                        start = text.index("{\n")
                        status = json.loads("".join(text[start:text.index("}\n", start) + 1]))
                    time.sleep(0.1)
                self.assertIsNotNone(status, lines)
                self.assertEqual((status["status"], status["feeds_engine"]), ("UP", "python"))
                self.assertEqual(status["observations"], str(Path(tmp) / "observations.jsonl"))
                counts, deadline = {}, time.monotonic() + 60
                while counts.get("ok") != 1 and time.monotonic() < deadline:
                    try:
                        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=5) as r:
                            counts = json.load(r)["coverage"]["node_status_counts"]
                    except OSError:
                        pass
                    time.sleep(0.5)
                self.assertEqual(counts.get("ok"), 1, counts)
                self.assertTrue(accepts(otlp))
                process.send_signal(signal.SIGTERM)
                self.assertEqual(process.wait(timeout=30), 0)
            finally:
                if process.poll() is None:
                    process.kill()
                reader.join(timeout=10)
                process.stdout.close()
            self.assertFalse(accepts(port), "control room still listening")
            self.assertFalse(accepts(otlp), "feeds still listening")

    def test_bad_config_starts_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "feeds.json"
            config.write_text('{"schema_version": 1}')
            run = subprocess.run([sys.executable, str(SCRIPTS / "operate.py"), "up", "--repo", tmp,
                                  "--feeds", str(config), "--port", str(free_port())],
                                 capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 1)
        self.assertEqual(json.loads(run.stdout)["status"], "ERROR")


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    unittest.main()
