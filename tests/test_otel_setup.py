"""One-command tracing: which apps a repository has, and starting them with OpenTelemetry.

The unit tests need nothing installed. The end-to-end test installs the real
instrumentation from npm and PyPI, so it runs only with ONTOLOGYEX_OTEL_E2E=1 (CI
runs it in its own job): a Node service calls a Flask service, both started with
`otel_setup.py run` and no code change, and the feeds receiver pairs the call.
"""
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "ontology-extraction/scripts"
sys.path.insert(0, str(SCRIPTS))
import feed_setup
import feeds
import otel_setup

E2E = os.environ.get("ONTOLOGYEX_OTEL_E2E") == "1"


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def make_repo(root: Path) -> Path:
    repo = root / "repo"
    write(repo, "compose.yml", "services:\n  storefront:\n    build: ./apps/web\n"
                               "  api:\n    build:\n      context: services/api\n")
    write(repo, "apps/web/package.json", json.dumps({"name": "@shop/web", "scripts": {"start": "node server.js"},
                                                     "dependencies": {"express": "^4.19.0"}}, indent=2))
    write(repo, "services/api/pyproject.toml", '[project]\nname = "shop-api"\ndependencies = ["fastapi>=0.110", "httpx"]\n')
    write(repo, "services/api/main.py", "")
    write(repo, "workers/mailer/requirements.txt", "celery==5.4\nredis\n")
    write(repo, "workers/mailer/app.py", "")
    write(repo, "legacy/package.json", json.dumps({"name": "legacy", "main": "index.js",
                                                   "dependencies": {"@opentelemetry/sdk-node": "^0.300.0"}}))
    write(repo, "services/billing/go.mod", "module example.com/billing\n\ngo 1.22\n")
    write(repo, "services/ledger/Ledger.csproj", "<Project Sdk=\"Microsoft.NET.Sdk\"></Project>\n")
    write(repo, "apps/web/node_modules/dep/package.json", json.dumps({"name": "dep"}))  # never an app
    return repo


class Apps(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.repo = make_repo(Path(cls.tmp.name))
        cls.apps = {a["folder"]: a for a in otel_setup.find_apps(cls.repo)}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_every_app_is_found_once(self):
        self.assertEqual(sorted(self.apps), ["apps/web", "legacy", "services/api", "services/billing",
                                             "services/ledger", "workers/mailer"])
        self.assertEqual({f: a["language"] for f, a in self.apps.items()}, {
            "apps/web": "node", "legacy": "node", "services/api": "python", "services/billing": "go",
            "services/ledger": "dotnet", "workers/mailer": "python"})

    def test_names_and_components_come_from_compose_then_the_manifest(self):
        web, api, mailer = self.apps["apps/web"], self.apps["services/api"], self.apps["workers/mailer"]
        self.assertEqual((web["service"], web["component"], web["component_source"]),
                         ("storefront", "discovery:service:compose.yml:storefront", "compose service"))
        self.assertEqual((api["service"], api["component"]), ("api", "discovery:service:compose.yml:api"))
        # Discovery reads pyproject.toml but not requirements.txt: no map node, so no name to map.
        self.assertEqual((mailer["service"], mailer["component"]), ("mailer", None))

    def test_status_frameworks_and_commands(self):
        web = self.apps["apps/web"]
        self.assertEqual((web["status"], web["start"]), ("READY", "npm start"))
        self.assertTrue(web["command"].endswith("run --service storefront -- npm start"))
        self.assertEqual(web["frameworks"][0]["name"], "express")
        self.assertEqual(web["frameworks"][0]["evidence"]["path"], "apps/web/package.json")
        self.assertIn('"express"', web["frameworks"][0]["evidence"]["text"])
        self.assertEqual(self.apps["services/api"]["start"], "python main.py")
        self.assertEqual([f["name"] for f in self.apps["workers/mailer"]["frameworks"]], ["celery", "redis"])
        self.assertEqual(self.apps["legacy"]["status"], "ALREADY_INSTRUMENTED")
        for folder in ("services/billing", "services/ledger"):
            self.assertEqual(self.apps[folder]["status"], "NOT_AUTOMATED")
            self.assertTrue(self.apps[folder]["needs"])
            self.assertNotIn("command", self.apps[folder])

    def test_propose_maps_names_and_feed_setup_offers_them(self):
        proposal = otel_setup.propose(self.repo)
        self.assertEqual(proposal["otlp_services"]["storefront"], "discovery:service:compose.yml:storefront")
        self.assertNotIn("billing", proposal["otlp_services"])
        offered = {c["entry"]["service"]: c for c in feed_setup.propose(self.repo)["candidates"] if c["kind"] == "otlp"}
        self.assertEqual(set(offered), {"storefront", "api"})
        self.assertEqual(offered["api"]["strength"], "STRONG")

    def test_duplicate_names_get_a_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            write(repo, "a/package.json", json.dumps({"name": "web"}))
            write(repo, "b/package.json", json.dumps({"name": "web"}))
            names = sorted(a["service"] for a in otel_setup.find_apps(repo))
        self.assertEqual(names, ["web", "web-2"])


class Running(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def script(self, name: str, first_line: str) -> str:
        path = self.root / "bin" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(first_line + "\nprint('hi')\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        return str(path)

    def test_language_from_the_command(self):
        for command, language in ((["npm", "start"], "node"), (["node", "server.js"], "node"),
                                  (["python3", "-m", "flask", "run"], "python"), (["uvicorn", "app:app"], "python"),
                                  (["python3.12", "app.py"], "python")):
            self.assertEqual(otel_setup.detect_language(command), language, command)
        self.assertEqual(otel_setup.detect_language([self.script("serve", "#!/usr/bin/env python3")]), "python")
        self.assertEqual(otel_setup.detect_language([self.script("web", "#!/usr/bin/env node")]), "node")
        with self.assertRaisesRegex(otel_setup.SetupError, "--lang"):
            otel_setup.detect_language(["./run.sh"])

    def test_the_interpreter_comes_from_the_command_or_its_first_line(self):
        python = shutil.which("python3")
        self.assertEqual(otel_setup.python_for(["python3", "app.py"], None), python)
        self.assertEqual(otel_setup.python_for([self.script("serve", f"#!{python}")], None), python)
        self.assertEqual(otel_setup.python_for([self.script("gun", "#!/usr/bin/env python3")], None), python)
        self.assertEqual(otel_setup.python_for(["uv", "run", "app.py"], "python3"), python)
        with self.assertRaisesRegex(otel_setup.SetupError, "--python"):
            otel_setup.python_for(["uv", "run", "app.py"], None)

    def test_endpoint_and_token_from_the_feeds_config(self):
        self.assertEqual(otel_setup.endpoint_from(None, None), ("http://127.0.0.1:4318", ""))
        config = self.root / "feeds.json"
        config.write_text(json.dumps({"schema_version": 1, "kind": feeds.FEED_KIND, "output": "o.jsonl",
                                      "otlp": {"bind": "0.0.0.0", "port": 14318, "token_env": "SHOP_OTLP_TOKEN",
                                               "services": {"api": "discovery:service:compose.yml:api"}}}))
        with mock.patch.dict(os.environ, {"SHOP_OTLP_TOKEN": "s3cret"}):
            self.assertEqual(otel_setup.endpoint_from(config, None),
                             ("http://127.0.0.1:14318", "Authorization=Bearer%20s3cret"))
        with mock.patch.dict(os.environ, {"SHOP_OTLP_TOKEN": ""}):
            with self.assertRaisesRegex(otel_setup.SetupError, "SHOP_OTLP_TOKEN"):
                otel_setup.endpoint_from(config, None)
        with self.assertRaises(otel_setup.SetupError):
            otel_setup.endpoint_from(None, "ftp://x")

    def test_environment_keeps_what_the_app_already_sets(self):
        args = mock.Mock(service="web", feeds=None, endpoint=None, lang="node", python=None)
        with mock.patch.object(otel_setup, "ensure_node", return_value=Path("/c/register.cjs")), \
                mock.patch.dict(os.environ, {"NODE_OPTIONS": "--max-old-space-size=512"}):
            env, language = otel_setup.prepare(args, ["npm", "start"])
        self.assertEqual(language, "node")
        self.assertEqual(env["NODE_OPTIONS"], f'--max-old-space-size=512 --require "/c/register.cjs" '
                                              f'--require "{otel_setup.NODE_EXIT}"')
        self.assertEqual((env["OTEL_SERVICE_NAME"], env["OTEL_EXPORTER_OTLP_PROTOCOL"]), ("web", "http/protobuf"))
        args = mock.Mock(service="api", feeds=None, endpoint="http://127.0.0.1:9", lang=None, python=None)
        with mock.patch.object(otel_setup, "ensure_python", return_value=Path("/c/hook")), \
                mock.patch.dict(os.environ, {"PYTHONPATH": "/app/src"}):
            env, language = otel_setup.prepare(args, ["python3", "app.py"])
        self.assertEqual((language, env["PYTHONPATH"]), ("python", os.pathsep.join(["/c/hook", "/app/src"])))
        with self.assertRaises(otel_setup.SetupError):
            otel_setup.otel_env("bad name", "http://x", "")

    def test_cli_errors_are_json(self):
        run = subprocess.run([sys.executable, str(SCRIPTS / "otel_setup.py"), "run", "--service", "web"],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 1)
        self.assertIn("--", json.loads(run.stderr)["error"])


@unittest.skipUnless(E2E, "installs from npm and PyPI; set ONTOLOGYEX_OTEL_E2E=1")
class EndToEnd(unittest.TestCase):
    def test_node_calls_python_with_no_code_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            venv = root / "venv"
            subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
            python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            subprocess.run([str(python), "-m", "pip", "install", "--quiet", "flask==3.1.1"], check=True, timeout=600)
            write(root, "api/app.py", "import os\nfrom flask import Flask\napp = Flask(__name__)\n"
                                      "@app.get('/orders')\ndef orders():\n    return '[]'\n"
                                      "app.run(port=int(os.environ['PORT']))\n")
            write(root, "web/server.js", "const http = require('http');\nhttp.createServer((req, res) => {\n"
                                         "  http.get(`http://127.0.0.1:${process.env.API_PORT}/orders`, r => { r.resume(); "
                                         "r.on('end', () => { res.writeHead(200); res.end('ok'); }); })\n"
                                         "    .on('error', () => { res.writeHead(502); res.end(); });\n"
                                         "}).listen(+process.env.PORT);\n")
            window = feeds.SpanWindow({"services": {"web": "discovery:service:c.yml:web",
                                                    "api": "discovery:service:c.yml:api"}})
            receiver = ThreadingHTTPServer(("127.0.0.1", 0), feeds.otlp_handler(window, ""))
            threading.Thread(target=receiver.serve_forever, daemon=True).start()
            self.addCleanup(receiver.server_close)
            self.addCleanup(receiver.shutdown)

            def free():
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", 0))
                    return sock.getsockname()[1]

            api, web = free(), free()
            env = {**os.environ, "ONTOLOGYEX_OTEL_CACHE": str(root / "cache"), "API_PORT": str(api),
                   "OTEL_BSP_SCHEDULE_DELAY": "200"}
            endpoint = f"http://127.0.0.1:{receiver.server_address[1]}"
            command = [sys.executable, str(SCRIPTS / "otel_setup.py"), "run", "--endpoint", endpoint, "--service"]
            processes = [
                subprocess.Popen([*command, "api", "--", str(python), "app.py"], cwd=root / "api",
                                 env={**env, "PORT": str(api)}, stderr=subprocess.DEVNULL),
                subprocess.Popen([*command, "web", "--", "node", "server.js"], cwd=root / "web",
                                 env={**env, "PORT": str(web)}, stderr=subprocess.DEVNULL)]
            try:
                deadline = time.monotonic() + 600  # first runs install the instrumentation
                while time.monotonic() < deadline:
                    try:
                        urllib.request.urlopen(f"http://127.0.0.1:{api}/orders", timeout=2).read()
                        urllib.request.urlopen(f"http://127.0.0.1:{web}/", timeout=2).read()
                        break
                    except OSError:
                        time.sleep(0.5)
                for _ in range(10):
                    urllib.request.urlopen(f"http://127.0.0.1:{web}/", timeout=5).read()
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline and not window.call_counts():
                    time.sleep(0.5)
                calls = window.call_counts()
                self.assertEqual([(a, b) for a, b, *_ in calls], [("web", "api")])
                self.assertGreaterEqual(calls[0][2], 10)
            finally:
                for process in processes:
                    process.send_signal(signal.SIGTERM)
                codes = [p.wait(timeout=20) for p in processes]
            self.assertEqual(codes, [143, 143])  # SIGTERM still stops both, after the last spans are sent


if __name__ == "__main__":
    unittest.main()
