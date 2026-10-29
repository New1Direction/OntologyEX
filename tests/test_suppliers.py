"""The supplier map: who the business runs on, from what the repository declares, and what relies on each."""
import importlib.util
import json
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ontology-extraction/scripts"))
import agent_tools as at
import suppliers as sp

spec = importlib.util.spec_from_file_location("ontologyex_supplier_demo", ROOT / "examples/operating-picture/demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


def write(repo: Path, rel: str, text: str) -> None:
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(text)


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp(prefix="ontologyex-suppliers-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.repo, ignore_errors=True))
        write(self.repo, "package.json", json.dumps({
            "name": "shop", "dependencies": {"stripe": "^14", "@sendgrid/mail": "^8", "@aws-sdk/client-s3": "^3",
                                             "react": "^18", "prisma": "^5"},
            "devDependencies": {"@sentry/cli": "^2"}}, indent=2))
        write(self.repo, "requirements.txt", "boto3==1.34\nopenai==1.0\n")
        write(self.repo, ".env.example", "STRIPE_SECRET_KEY=\nNEXT_PUBLIC_POSTHOG_KEY=\nDATABASE_URL=\n")
        write(self.repo, ".env", "TWILIO_AUTH_TOKEN=a-real-secret\n")
        write(self.repo, "scripts/requirements.txt", "anthropic\n")
        write(self.repo, "compose.yml", "services:\n  app:\n    build: .\n    environment:\n      PLAID_CLIENT_ID: x\n")

    def test_vendors_from_manifests_and_setting_names(self):
        report = sp.scan(self.repo)
        names = {s["name"]: s for s in report["suppliers"]}
        self.assertEqual(set(names), {"Stripe", "SendGrid", "Amazon Web Services", "OpenAI", "PostHog", "Plaid"})
        self.assertEqual(names["Stripe"]["criticality"], "CRITICAL")
        self.assertEqual({e["kind"] for e in names["Stripe"]["evidence"]}, {"dependency", "setting name"})
        dependency = next(e for e in names["Stripe"]["evidence"] if e["kind"] == "dependency")
        self.assertEqual((dependency["path"], dependency["line"]), ("package.json", 4))
        self.assertEqual(names["PostHog"]["settings"], ["NEXT_PUBLIC_POSTHOG_KEY"])  # framework prefix understood
        self.assertEqual(names["Plaid"]["evidence"][0]["path"], "compose.yml")
        text = json.dumps(report)
        self.assertNotIn("a-real-secret", text)  # a real .env is never read
        self.assertNotIn("Twilio", text)
        self.assertNotIn("Anthropic", text)  # helper scripts are not the product's suppliers
        self.assertNotIn("Sentry", text)  # development tools are not suppliers

    def test_categories_and_single_suppliers(self):
        cats = {c["category"]: c for c in sp.scan(self.repo)["categories"]}
        self.assertTrue(cats["payments"]["single_supplier"])
        self.assertFalse(cats["analytics and monitoring"]["single_supplier"])  # LOW criticality is never flagged
        self.assertEqual(cats["payments"]["criticality"], "CRITICAL")

    def test_own_supplier_records_and_nothing_declared(self):
        nodes = [{"id": "t1", "label": "purchase_orders", "kind": "declared_db_table"},
                 {"id": "t2", "label": "users", "kind": "declared_db_table"},
                 {"id": "r1", "label": "GET /api/vendors/:id", "kind": "api_route"}]
        self.assertEqual([r["id"] for r in sp.scan(self.repo, nodes)["own_supplier_records"]], ["t1", "r1"])
        empty = Path(tempfile.mkdtemp(prefix="ontologyex-no-suppliers-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(empty, ignore_errors=True))
        write(empty, "package.json", '{"dependencies": {"react": "^18"}}')
        self.assertEqual(sp.scan(empty)["suppliers"], [])


class ServedTests(unittest.TestCase):
    """In the control room, suppliers are entities: Stripe reaches the business through the payments service."""

    def test_suppliers_reach_the_business_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            built = demo.build(Path(tmp) / "demo")
            repo = Path(built["repo"])
            compose = (repo / "compose.yml").read_text().replace("  payments:\n", "  payments:\n    build: ./payments\n")
            (repo / "compose.yml").write_text(compose)
            write(repo, "payments/requirements.txt", "stripe==7.0\nflask==3.0\n")
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            process = subprocess.Popen([*built["command"], "--port", str(port)],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.addCleanup(lambda: (process.terminate(), process.wait(timeout=5)))
            base = f"http://127.0.0.1:{port}"
            for _ in range(150):
                try:
                    urllib.request.urlopen(base + "/api/state", timeout=2).close()
                    break
                except OSError:
                    time.sleep(0.1)
            with urllib.request.urlopen(base + "/api/suppliers", timeout=30) as resp:
                report = json.load(resp)
            stripe = next(s for s in report["suppliers"] if s["name"] == "Stripe")
            relied = {r["label"]: r["standing"] for r in stripe["relied_on_by"]}
            self.assertEqual(relied["payments"], "SOURCE")  # payments/requirements.txt declares it
            self.assertIn("IssueRefund", relied)  # through the link a person confirmed
            with urllib.request.urlopen(base + "/suppliers", timeout=10) as resp:
                self.assertIn(b"Suppliers", resp.read())
            tools = at.Tools(at.Source(base, None), at.load_policy(None),
                             at.Audit(Path(tmp) / "audit.jsonl", "claude", "Dana"), None)
            listed = tools.call("list_suppliers", {})
            self.assertEqual([s["name"] for s in listed["suppliers"]], ["Stripe", "SendGrid", "Sentry"])  # by criticality
            context = tools.call("get_entity_context", {"id": stripe["id"]})
            self.assertEqual(context["entity"]["kind"], "external_supplier")
            impact = tools.call("analyze_change", {"files": "payments/requirements.txt"})
            self.assertIn(stripe["id"], [t["id"] for t in impact["touched"]])  # change impact sees suppliers too


if __name__ == "__main__":
    unittest.main()
