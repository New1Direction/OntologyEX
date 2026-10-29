"""Compliance packs: controls, evidence with lines cited, what applies, and what only a person can provide."""
import importlib.util
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ontology-extraction/scripts"))
sys.path.insert(0, str(ROOT / "tests"))
import agent_tools as at
import command_center as cc
import compliance as co
from test_entity_context import bundle

spec = importlib.util.spec_from_file_location("ontologyex_compliance_demo", ROOT / "examples/operating-picture/demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)

FILES = {
    "package.json": json.dumps({"dependencies": {"stripe": "^14", "bcrypt": "^5", "helmet": "^7",
                                                 "@sentry/node": "^8"}}, indent=2),
    "package-lock.json": "{}",
    ".github/workflows/ci.yml": "jobs:\n  test:\n    steps:\n      - run: npm test\n  scan:\n    uses: github/codeql-action/analyze@v3\n",
    ".github/dependabot.yml": "version: 2\n",
    "SECURITY.md": "# Reporting a vulnerability\n",
    ".gitignore": "node_modules\n.env\n",
    ".env": "STRIPE_SECRET=sk_live_never_read\n",
    "prisma/schema.prisma": "model User {\n  id String\n  email String\n  phone String\n  card_number String\n}\n",
    "docs/incident-response.md": "# Incident response\nPage the on-call engineer.\n",
    "src/account.ts": "export function deleteAccount(id) {}\nexport function exportData(id) {}\n",
    "src/app.ts": "import helmet from 'helmet'\napp.use(helmet())\n",
    "src/log.ts": 'console.log("this may take a while, please be patient")\n',
    "src/crypto.ts": 'const KMS_KEY = "kms-never-shown-123"\n',
    "seeds/demo.ts": "deleteAccount(1)\nconst diagnosis = 'x'\n",
}


def shop(root: Path) -> Path:
    for rel, text in FILES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return root


def model() -> dict:
    data = bundle()
    data["suppliers"] = {"suppliers": [{"id": "discovery:supplier:stripe", "name": "Stripe", "category": "payments",
                                        "criticality": "CRITICAL",
                                        "evidence": [{"path": "package.json", "line": 2, "kind": "dependency"}]}],
                         "categories": [{"category": "payments", "criticality": "CRITICAL", "suppliers": ["Stripe"],
                                         "single_supplier": True}]}
    return data


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.repo = shop(Path(tempfile.mkdtemp(prefix="ontologyex-compliance-")))
        self.addCleanup(lambda: shutil.rmtree(self.repo, ignore_errors=True))
        self.scan = co.scan_repo(self.repo)

    def test_evidence_is_cited_by_line(self):
        found = self.scan["found"]
        self.assertEqual((found["ci_tests"][0]["path"], found["ci_tests"][0]["line"]), (".github/workflows/ci.yml", 4))
        self.assertEqual(found["sast"][0]["path"], ".github/workflows/ci.yml")
        self.assertEqual(found["dependency_updates"][0]["path"], ".github/dependabot.yml")
        self.assertEqual(found["card_data"][0], {"path": "prisma/schema.prisma", "line": 5, "text": "card_number String"})
        self.assertEqual(found["data_deletion"][0]["path"], "src/account.ts")
        self.assertEqual(found["env_ignored"][0]["line"], 2)

    def test_secrets_logs_and_seeds_are_never_evidence(self):
        text = json.dumps(self.scan)
        self.assertNotIn("sk_live_never_read", text)  # a real .env is never read
        self.assertNotIn("kms-never-shown-123", text)  # a line setting a key is cited without its text
        self.assertEqual(self.scan["found"]["encryption_at_rest"][0], {"path": "src/crypto.ts", "line": 1, "text": None})
        self.assertFalse(self.scan["found"].get("health_data"))  # "please be patient" is a log line; seeds are not evidence
        self.assertNotIn("seeds/demo.ts", text)


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.repo = shop(Path(tempfile.mkdtemp(prefix="ontologyex-compliance-")))
        self.addCleanup(lambda: shutil.rmtree(self.repo, ignore_errors=True))

    def report(self, data=None):
        return co.report(data or model(), co.scan_repo(self.repo))

    def test_what_applies_and_why(self):
        packs = {p["id"]: p for p in self.report()["packs"]}
        self.assertEqual({k: p["applies"] for k, p in packs.items()},
                         {"soc2": "ASKED_BY_CUSTOMERS", "pci": "APPLIES", "hipaa": "NOT_INDICATED", "gdpr": "APPLIES"})
        self.assertIn("Stripe", packs["pci"]["why"])
        (self.repo / "prisma/clinic.prisma").write_text("model Visit {\n  patient_id String\n  diagnosis String\n}\n")
        self.assertEqual({p["id"]: p["applies"] for p in self.report()["packs"]}["hipaa"], "APPLIES")  # two health terms

    def test_controls_all_of_and_any_of(self):
        controls = {c["id"]: c for p in self.report()["packs"] for c in p["controls"] if p["id"] in ("soc2", "pci")}
        self.assertEqual(controls["8.3"]["status"], "SHOWN")  # bcrypt alone: own hashing or an identity provider
        self.assertEqual(controls["CC8.1"]["status"], "PARTLY_SHOWN")  # tests in CI, but no CODEOWNERS or PR template
        self.assertEqual(controls["CC6.8"]["status"], "SHOWN")
        self.assertEqual(controls["A1.2"]["status"], "NOT_SHOWN_IN_SOURCES")
        self.assertEqual((controls["CC1"]["status"], bool(controls["CC1"]["person"])), ("NEEDS_A_PERSON", True))
        for c in controls.values():
            self.assertEqual(c["status"] == "SHOWN", all(check["found"] for check in c["checks"]) and bool(c["checks"]))

    def test_card_fields_are_a_risk_and_a_ranked_item(self):
        data = model()
        data["compliance"] = self.report(data)
        pci = next(p for p in data["compliance"]["packs"] if p["id"] == "pci")
        self.assertEqual(pci["risks"][0]["risk"], "CARD_FIELDS_IN_SCHEMA")
        items = {i["kind"]: i for i in cc.report(data)["items"]}
        self.assertEqual(items["CARD_FIELDS"]["category"], "Compliance")
        self.assertIn("prisma/schema.prisma:5", items["CARD_FIELDS"]["why"])
        self.assertTrue(items["COMPLIANCE_GAPS"]["title"].startswith(("PCI DSS", "GDPR")))
        self.assertIn("COMPLIANCE_ASKED", items)  # SOC 2: asked by customers, ranked lower

    def test_markdown_for_the_auditor(self):
        text = co.markdown(self.report(), "pci")
        self.assertIn("Not an audit, a certification or legal advice", text)
        self.assertIn("prisma/schema.prisma:5", text)
        self.assertIn("`package.json:2`", text)
        self.assertIn("a person provides:", text)
        self.assertNotIn("## SOC 2", text)

    def test_no_repository_says_so(self):
        self.assertEqual(co.report(model(), None)["status"], "NO_LOCAL_REPOSITORY")


class ServedTests(unittest.TestCase):
    def test_route_page_and_agent_tool(self):
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
                    urllib.request.urlopen(base + "/api/state", timeout=2).close()
                    break
                except OSError:
                    time.sleep(0.1)
            with urllib.request.urlopen(base + "/api/compliance", timeout=30) as resp:
                found = json.load(resp)
            self.assertEqual((found["status"], [p["id"] for p in found["packs"]]), ("OK", ["soc2", "pci", "hipaa", "gdpr"]))
            with urllib.request.urlopen(base + "/api/compliance?pack=pci", timeout=30) as resp:
                self.assertEqual([p["id"] for p in json.load(resp)["packs"]], ["pci"])
            with urllib.request.urlopen(base + "/api/compliance?pack=gdpr&format=markdown", timeout=30) as resp:
                self.assertTrue(resp.headers["Content-Type"].startswith("text/markdown"))
                self.assertIn("## GDPR", resp.read().decode())
            with urllib.request.urlopen(base + "/compliance", timeout=10) as resp:
                self.assertIn(b"Compliance", resp.read())
            for bad in ("pack=iso", "format=pdf", "x=1"):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(base + "/api/compliance?" + bad, timeout=10)
                self.assertEqual(caught.exception.code, 400, bad)
            tools = at.Tools(at.Source(base, None), at.load_policy(None),
                             at.Audit(Path(tmp) / "audit.jsonl", "claude", "Dana"), None)
            self.assertEqual([p["id"] for p in tools.call("compliance_status", {"pack": "soc2"})["packs"]], ["soc2"])
            with self.assertRaises(at.ToolError) as caught:
                tools.call("compliance_status", {"pack": "iso27001"})
            self.assertEqual(caught.exception.status, "INVALID_ARGUMENTS")


if __name__ == "__main__":
    unittest.main()
