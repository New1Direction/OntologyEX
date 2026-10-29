"""A whole-repository overview: the scan, the blueprint draft, and the page built from it.

The scan only reads; every finding names its file. A blueprint can cite the local
checkout, and the build checks each cited line exists and records what it says.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "ontology-extraction/scripts"
sys.path.insert(0, str(SCRIPTS))
import business_xray
import overview


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def make_shop(root: Path) -> Path:
    repo = root / "shop"
    write(repo, "README.md", "# Shop\n\nShop lets small bakeries take preorders online and get paid upfront.\n")
    write(repo, "compose.yml", "services:\n  web:\n    build: ./apps/web\n  api:\n    build: ./services/api\n"
                               "  db:\n    image: postgres:16\n  cache:\n    image: redis:7\n")
    write(repo, "apps/web/package.json", json.dumps({"name": "shop-web", "scripts": {"dev": "next dev"}, "dependencies": {
        "next": "14.2.0", "stripe": "^15.0.0", "next-auth": "^4.24.0", "@prisma/client": "^5.12.0"}}, indent=2))
    write(repo, "apps/web/pages/api/checkout.ts", "import Stripe from 'stripe';\n// Pro plan, $29/month.\n"
                                                  "export default async function handler(req, res) {}\n")
    write(repo, "apps/web/prisma/schema.prisma", "model User {\n  id String @id\n}\n")
    write(repo, "apps/web/src/lib/auth.ts", "export {};\n")
    write(repo, "apps/web/src/components/Border.tsx", "export {};\n")  # not money, despite "order"
    write(repo, "services/api/pyproject.toml", '[project]\nname = "shop-api"\ndependencies = ["django>=5", "openai"]\n')
    write(repo, "services/api/api/urls.py", "urlpatterns = []\n")
    write(repo, "services/api/billing/models.py", "class Invoice:\n    pass\n")
    write(repo, "services/api/manage.py", "")
    write(repo, "services/api/tests/test_orders.py", "")  # tests are not read-first material
    return repo


class Scan(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.repo = make_shop(Path(cls.tmp.name))
        cls.result = overview.scan(cls.repo)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_apps_services_and_datastores(self):
        self.assertEqual([(a["service"], a["language"]) for a in self.result["apps"]], [("web", "node"), ("api", "python")])
        self.assertEqual([s["name"] for s in self.result["services"]], ["api", "web"])
        self.assertEqual([(d["name"], d["image"]) for d in self.result["datastores"]],
                         [("cache", "redis:7"), ("db", "postgres:16")])
        self.assertEqual(self.result["datastores"][1]["evidence"]["path"], "compose.yml")

    def test_integrations_cite_the_manifest_line(self):
        found = {area: [h["library"] for h in hits] for area, hits in self.result["integrations"].items()}
        self.assertEqual(found["payments"], ["stripe"])
        self.assertEqual(found["auth"], ["next-auth"])
        self.assertEqual(found["AI"], ["openai"])
        stripe = self.result["integrations"]["payments"][0]["evidence"]
        self.assertEqual((stripe["path"], stripe["line"]), ("apps/web/package.json", 8))
        self.assertIn('"stripe"', stripe["text"])

    def test_read_first(self):
        read = self.result["read_first"]
        self.assertIn("apps/web/pages/api/checkout.ts", read["routes and APIs"])
        self.assertIn("apps/web/prisma/schema.prisma", read["data model"])
        self.assertEqual(sorted(read["money"]), ["apps/web/pages/api/checkout.ts", "services/api/billing/models.py"])
        self.assertIn("apps/web/src/lib/auth.ts", read["users and access"])
        self.assertIn("services/api/manage.py", read["entry points"])
        self.assertFalse(any("tests/" in p for paths in read.values() for p in paths))
        self.assertEqual(self.result["docs"], ["README.md"])

    def test_the_draft_has_cited_systems_and_placeholders(self):
        draft = overview.draft(self.result)
        self.assertEqual(draft["id"], "local/shop")
        names = {s["name"]: s for s in draft["systems"]}
        self.assertEqual(set(names) >= {"web", "api", "db", "cache", "stripe", "openai"}, True)
        self.assertEqual(names["db"]["kind"], "datastore")
        self.assertTrue(all(e["repo"] == "local" for e in draft["evidence"]))
        self.assertTrue(overview._placeholders(draft))  # purpose, stage, integration uses


class Build(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = make_shop(self.root)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
                       cwd=self.repo, check=True)
        self.head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, capture_output=True,
                                   text=True, check=True).stdout.strip()

    def filled(self) -> dict:
        blueprint = overview.draft(overview.scan(self.repo))
        blueprint["evidence"].append({"id": "readme", "repo": "local", "path": "README.md", "start": 3, "end": 3})
        blueprint["evidence"].append({"id": "checkout", "repo": "local", "path": "apps/web/pages/api/checkout.ts",
                                      "start": 2, "end": 3})
        blueprint["purpose"] = "Lets small bakeries take preorders online and get paid upfront."
        blueprint["stage"] = "Live product per the README"
        blueprint["money_flows"] = [{"id": "pro", "name": "Bakery pays the Pro plan", "description": "$29/month",
                                     "evidence": ["checkout"], "basis": "observed_in_source"}]
        blueprint["journeys"] = [{"id": "upgrade", "name": "Upgrade", "actor": "Bakery", "outcome": "Pro plan",
                                  "steps": [{"id": "pay", "name": "Pay", "evidence": ["checkout"], "state": "described"}]}]
        for system in blueprint["systems"]:
            system["description"] = system["description"].replace(overview.PLACEHOLDER, "not traced")
        return blueprint

    def save(self, blueprint: dict) -> Path:
        path = self.root / "blueprint.json"
        path.write_text(json.dumps(blueprint))
        return path

    def test_build_records_what_each_citation_says(self):
        result = overview.build(self.repo, self.save(self.filled()), self.root / "page.html", self.root / "page.json")
        self.assertEqual((result["status"], result["journeys"]), ("WRITTEN", 1))
        compiled = json.loads((self.root / "page.json").read_text())
        checkout = next(e for e in compiled["projects"][0]["evidence"] if e["id"] == "checkout")
        self.assertEqual((checkout["repo"], checkout["source"], checkout["commit"]), ("shop", "local", self.head))
        self.assertEqual(checkout["quote"], "// Pro plan, $29/month.\nexport default async function handler(req, res) {}")
        self.assertEqual(len(checkout["sha256"]), 64)
        self.assertTrue(checkout["url"].startswith("file://"))
        page = (self.root / "page.html").read_text()
        self.assertIn("Lets small bakeries take preorders", page)
        self.assertNotIn("https://", page.split("<script")[0])

    def test_build_refuses_what_it_cannot_stand_behind(self):
        cases = []
        left = self.filled()
        left["purpose"] = overview.PLACEHOLDER + ": later"
        cases.append((left, "placeholders"))
        past_end = self.filled()
        past_end["evidence"][-1]["end"] = 99
        cases.append((past_end, "has 3 lines"))
        outside = self.filled()
        outside["evidence"][-1]["path"] = "../secret.txt"
        cases.append((outside, "unsafe evidence path"))
        missing = self.filled()
        missing["evidence"][-1]["path"] = "nope.ts"
        cases.append((missing, "not a file"))
        stale = self.filled()
        stale["evidence"][-1]["commit"] = "0" * 40
        cases.append((stale, "not at commit"))
        for blueprint, message in cases:
            with self.assertRaisesRegex((overview.otel_setup.SetupError, business_xray.BlueprintError), message):
                overview.build(self.repo, self.save(blueprint), self.root / "x.html")
        self.assertFalse((self.root / "x.html").exists())

    def test_a_symlink_out_of_the_repository_is_refused(self):
        (self.root / "secret.txt").write_text("token\n")
        os.symlink(self.root / "secret.txt", self.repo / "linked.txt")
        blueprint = self.filled()
        blueprint["evidence"][-1].update(path="linked.txt", start=1, end=1)
        with self.assertRaisesRegex(business_xray.BlueprintError, "symlinked|not a file"):
            overview.build(self.repo, self.save(blueprint), self.root / "x.html")

    def test_cli_scan_keeps_an_edited_blueprint(self):
        out = self.root / "ov"
        command = [sys.executable, str(SCRIPTS / "overview.py"), "scan", "--repo", str(self.repo), "--out", str(out)]
        first = json.loads(subprocess.run(command, capture_output=True, text=True, check=True).stdout)
        self.assertEqual(len(first["written"]), 2)
        (out / "blueprint.json").write_text('{"edited": true}')
        again = json.loads(subprocess.run(command, capture_output=True, text=True, check=True).stdout)
        self.assertEqual((len(again["written"]), again["kept"]), (1, [str(out / "blueprint.json")]))
        self.assertEqual((out / "blueprint.json").read_text(), '{"edited": true}')
        run = subprocess.run([sys.executable, str(SCRIPTS / "overview.py"), "build", "--repo", str(self.repo),
                              "--blueprint", str(out / "blueprint.json"), "--out", str(out / "p.html")],
                             capture_output=True, text=True)
        self.assertEqual((run.returncode, json.loads(run.stdout)["status"]), (1, "ERROR"))


if __name__ == "__main__":
    unittest.main()
