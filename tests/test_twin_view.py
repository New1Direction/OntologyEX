"""A local digital-twin view over the existing model and observation APIs."""
import hashlib
import importlib.util
import json
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "ontology-extraction/scripts"
sys.path.insert(0, str(SCRIPTS))
import install

spec = importlib.util.spec_from_file_location("ontologyex_twin_demo", ROOT / "examples/operating-picture/demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)
BROWSER = shutil.which("chromium") or shutil.which("google-chrome")
HAS_PLAYWRIGHT = importlib.util.find_spec("playwright") is not None


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def get_json(base, path):
    with urllib.request.urlopen(base + path, timeout=10) as response:
        return json.load(response)


def start_room(command):
    port = free_port()
    process = subprocess.Popen([*command, "--port", str(port)], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    for _ in range(120):
        if process.poll() is not None:
            raise RuntimeError("control room stopped before it was ready")
        try:
            get_json(base, "/api/business")
            return process, base
        except OSError:
            time.sleep(0.1)
    process.terminate()
    process.wait(timeout=5)
    raise RuntimeError("control room did not start")


class TwinViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.built = demo.build(Path(cls.tmp.name) / "demo")
        cls.process, cls.base = start_room(cls.built["command"])

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate()
        cls.process.wait(timeout=5)
        cls.tmp.cleanup()

    def test_page_is_installed_and_served_with_local_only_policy(self):
        self.assertIn("scripts/twin_page.html", install.PACKAGE)
        with urllib.request.urlopen(self.base + "/twin", timeout=10) as response:
            page = response.read().decode()
            self.assertIn("default-src 'none'", response.headers["Content-Security-Policy"])
        self.assertIn("What if a system became unavailable?", page)
        self.assertIn('fetch("/api/context?id="', page)
        self.assertNotIn("innerHTML", page)
        self.assertNotIn("https://", page)

    def test_hypothetical_dependencies_do_not_change_reports_or_files(self):
        picture = get_json(self.base, "/api/business")
        self.assertEqual(picture["status"], "READY")
        self.assertIn("IssueRefund", {a["id"] for a in picture["actions"]})
        service = "discovery:service:compose.yml:api"
        query = "/api/context?" + urllib.parse.urlencode({"id": service})
        files = [Path(self.built[k]) for k in ("observations", "links", "events")]
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
        first = get_json(self.base, query)
        second = get_json(self.base, query)
        self.assertEqual(first["entity"]["status"], "ok")
        self.assertEqual(second["entity"]["status"], "ok")
        self.assertIn("IssueRefund", {x["label"] for x in first["relied_on_by"]})
        self.assertIn("web", {x["label"] for x in first["relied_on_by"]})
        self.assertFalse(first["execution_authorized"])
        self.assertEqual(before, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in files})

    @unittest.skipUnless(BROWSER and HAS_PLAYWRIGHT, "Chromium and Playwright are optional")
    def test_browser_shows_demo_and_scenario_without_changing_current_status(self):
        from playwright.sync_api import sync_playwright
        observations = Path(self.built["observations"])
        original = observations.read_bytes()
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=BROWSER, headless=True, args=["--no-sandbox"])
            try:
                context = browser.new_context(viewport={"width": 1280, "height": 900})
                page = context.new_page()
                errors, methods = [], []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("request", lambda request: methods.append(request.method))
                page.goto(self.base + "/twin", wait_until="domcontentloaded")
                page.get_by_text("IssueRefund", exact=True).wait_for(timeout=15000)
                self.assertIn("synthetic", page.locator("#banner").inner_text().lower())
                page.locator("#system-select").select_option(label="api")
                page.get_by_role("button", name="Explore impact").click()
                page.get_by_text("If api were unavailable").wait_for(timeout=15000)
                result = page.locator("#scenario-result").inner_text()
                self.assertIn("Current reported status: ok", result)
                self.assertIn("IssueRefund", result)
                self.assertIn("web", result)
                self.assertIn("does not prove no impact", result)
                issue_row = page.locator("#scenario-result .impact").filter(has_text="IssueRefund")
                evidence_url = issue_row.locator("a").get_attribute("href")
                detail = page.context.new_page()
                try:
                    detail.goto(self.base + evidence_url, wait_until="domcontentloaded")
                    detail.get_by_text("Quoted from the sources", exact=False).wait_for(timeout=15000)
                    self.assertIn("IssueRefund", detail.title())
                    detail.get_by_role("link", name="Twin", exact=True).click()
                    self.assertIn("Digital Twin", detail.title())
                finally:
                    detail.close()
                self.assertTrue(all(method == "GET" for method in methods))
                self.assertEqual(errors, [])
                self.assertEqual(observations.read_bytes(), original)
                # Change only the synthetic feed: the page should refresh the real
                # report and scenario independently, without a page reload.
                rows = [json.loads(line) for line in original.splitlines()]
                api = next(row for row in rows if row["component_id"] == "discovery:service:compose.yml:api")
                api["status"] = "warning"
                api["observed_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                observations.write_text("".join(json.dumps(row) + "\n" for row in rows))
                page.get_by_text("Current reported status: warning.", exact=False).wait_for(timeout=15000)
                self.assertIn("If api were unavailable", page.locator("#scenario-result").inner_text())
                page.set_viewport_size({"width": 390, "height": 844})
                self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 390)
                # If a refreshed bounded view no longer contains the chosen
                # service, do not leave the old scenario looking current.
                def hide_api(route):
                    response = route.fetch()
                    body = response.json()
                    body["nodes"] = [node for node in body["nodes"] if node.get("label") != "api"]
                    route.fulfill(response=response, json=body)
                page.route(re.compile(r"/api/view\?lens=services$"), hide_api)
                page.get_by_text("The selected service is no longer in this view").wait_for(timeout=15000)
            finally:
                browser.close()
                observations.write_bytes(original)


class TwinWithoutModelTests(unittest.TestCase):
    @unittest.skipUnless(BROWSER and HAS_PLAYWRIGHT, "Chromium and Playwright are optional")
    def test_browser_marks_unmodeled_and_unobserved_data_as_unknown(self):
        from playwright.sync_api import sync_playwright
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            (repo / "compose.yml").write_text("services:\n  web:\n    depends_on: [db]\n  db: {}\n")
            (repo / "routes.py").write_text('router = APIRouter(prefix="/orders")\n'
                                             '@router.get("/{id}")\ndef read_order(id):\n    pass\n')
            process, base = start_room([sys.executable, str(SCRIPTS / "control_room.py"), "--repo", str(repo)])
            try:
                with sync_playwright() as p:
                    browser = p.chromium.launch(executable_path=BROWSER, headless=True, args=["--no-sandbox"])
                    try:
                        page = browser.new_page()
                        page.goto(base + "/twin", wait_until="domcontentloaded")
                        page.get_by_text("No business workflow model is loaded yet.", exact=False).wait_for(timeout=15000)
                        banner = page.locator("#banner").inner_text()
                        self.assertIn("status is unknown", banner)
                        page.get_by_text("GET /orders/{id}", exact=True).wait_for(timeout=20000)
                        self.assertIn("source candidate", page.locator("#workflows").inner_text())
                        self.assertEqual(page.locator("#workflow-count").inner_text(), "0")
                        page.locator("#system-select").select_option(label="db")
                        page.get_by_role("button", name="Explore impact").click()
                        page.get_by_text("If db were unavailable").wait_for(timeout=15000)
                        result = page.locator("#scenario-result").inner_text()
                        self.assertIn("Current reported status: unknown", result)
                        self.assertIn("web", result)
                        self.assertIn("source declared", result)
                    finally:
                        browser.close()
            finally:
                process.terminate()
                process.wait(timeout=5)


class TwinEarlySignalTests(unittest.TestCase):
    def test_launcher_forwards_thresholds_from_a_local_prometheus_feed(self):
        class PrometheusFixture(BaseHTTPRequestHandler):
            def do_GET(self):
                payload = json.dumps({"status": "success", "data": {"resultType": "vector",
                                      "result": [{"value": [0, "720"]}]}}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), PrometheusFixture)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                repo = root / "repo"
                repo.mkdir()
                (repo / "compose.yml").write_text("services:\n  payments: {}\n")
                component = "discovery:service:compose.yml:payments"
                config = root / "feeds.json"
                config.write_text(json.dumps({"schema_version": 1, "kind": "ONTOLOGYEX_FEEDS",
                    "output": "observations.jsonl", "allowed_hosts": ["127.0.0.1"],
                    "prometheus": {"url": f"http://127.0.0.1:{server.server_address[1]}",
                                   "interval_seconds": 5, "targets": [{"component": component,
                                                                          "p95_latency_ms": "latency"}]}}))
                thresholds = root / "thresholds.json"
                thresholds.write_text(json.dumps({"schema_version": 1, "warning_above": {"p95_latency_ms": 500}}))
                port = free_port()
                process = subprocess.Popen([sys.executable, str(SCRIPTS / "operate.py"), "up", "--repo", str(repo),
                                            "--feeds", str(config), "--thresholds", str(thresholds),
                                            "--port", str(port), "--engine", "python", "--no-history"],
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try:
                    base = f"http://127.0.0.1:{port}"
                    deadline, indicator = time.monotonic() + 20, None
                    while time.monotonic() < deadline:
                        self.assertIsNone(process.poll(), "one-command launcher stopped")
                        try:
                            view = get_json(base, "/api/view?lens=services")
                            node = next(n for n in view["nodes"] if n["id"] == component)
                            if node["leading_indicators"]:
                                indicator = node["leading_indicators"][0]
                                break
                        except (OSError, StopIteration):
                            pass
                        time.sleep(0.1)
                    self.assertIsNotNone(indicator, "no signal arrived from the local Prometheus fixture")
                    self.assertEqual(node["source_status"], "ok")
                    self.assertEqual(node["status"], "warning")
                    self.assertEqual(indicator["source"], "prometheus")
                    self.assertEqual((indicator["previous"], indicator["current"]), (720, 720))
                    self.assertNotEqual(indicator["previous_observed_at"], indicator["current_observed_at"])
                finally:
                    process.terminate()
                    process.wait(timeout=10)
        finally:
            server.shutdown()
            server.server_close()

    @unittest.skipUnless(BROWSER and HAS_PLAYWRIGHT, "Chromium and Playwright are optional")
    def test_synthetic_signal_links_to_business_paths_and_clears_with_only_one_sample(self):
        from playwright.sync_api import sync_playwright
        with tempfile.TemporaryDirectory() as tmp:
            built = demo.build(Path(tmp) / "demo", early_warning=True)
            observations = Path(built["observations"])
            original = observations.read_bytes()
            process, base = start_room(built["command"])
            try:
                view = get_json(base, "/api/view?lens=services")
                payments = next(n for n in view["nodes"] if n["label"] == "payments")
                self.assertEqual((payments["status"], payments["source_status"]), ("warning", "ok"))
                self.assertEqual(payments["leading_indicators"][0]["previous"], 650)
                self.assertEqual(payments["leading_indicators"][0]["current"], 720)
                with sync_playwright() as p:
                    browser = p.chromium.launch(executable_path=BROWSER, headless=True, args=["--no-sandbox"])
                    try:
                        page = browser.new_page(viewport={"width": 1280, "height": 900})
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.goto(base + "/twin", wait_until="domcontentloaded")
                        signal = page.locator("#signals .row").filter(has_text="payments")
                        signal.get_by_text("650 ms → 720 ms", exact=False).wait_for(timeout=15000)
                        self.assertIn("above 500 ms", signal.inner_text())
                        self.assertIn("synthetic", signal.inner_text())
                        self.assertIn("source reported ok", signal.inner_text())
                        self.assertIn("not a prediction", page.locator("#signals-title").locator("..").inner_text())
                        payment_row = page.locator("#systems .row").filter(has_text="payments")
                        self.assertIn("threshold signal active", payment_row.inner_text())
                        self.assertIn("ok", payment_row.inner_text())
                        signal.get_by_role("button", name="Explore connected effects").click()
                        page.get_by_text("If payments were unavailable").wait_for(timeout=15000)
                        result = page.locator("#scenario-result").inner_text()
                        self.assertIn("Current reported status: ok. A threshold signal is active.", result)
                        self.assertIn("IssueRefund", result)
                        self.assertIn("web", result)
                        self.assertIn("2 connections", result)
                        self.assertIn("revenue_reporting", result)
                        self.assertIn("3 connections", result)
                        self.assertIn("sales_dashboard", result)
                        self.assertIn("4 connections", result)
                        self.assertIn("source declared", result)
                        self.assertIn("recorded as confirmed", result)
                        self.assertEqual(observations.read_bytes(), original)
                        # Once there is only one sample, the active signal clears
                        # without changing the source's reported status.
                        rows = [json.loads(line) for line in original.splitlines()]
                        payment_rows = [r for r in rows if r["component_id"] == payments["id"]]
                        rows.remove(min(payment_rows, key=lambda r: r["observed_at"]))
                        observations.write_text("".join(json.dumps(row) + "\n" for row in rows))
                        page.get_by_text("No active threshold signals", exact=False).wait_for(timeout=15000)
                        self.assertIn("ok", page.locator("#systems .row").filter(has_text="payments").inner_text())
                        self.assertEqual(errors, [])
                    finally:
                        browser.close()
            finally:
                process.terminate()
                process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
