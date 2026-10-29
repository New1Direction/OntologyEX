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
    write(repo, "apps/web/src/lib/auth.ts", "import NextAuth from 'next-auth';\nexport const session = NextAuth({});\n")
    write(repo, "apps/web/src/components/Border.tsx", "export {};\n")  # not money, despite "order"
    write(repo, "services/api/pyproject.toml", '[project]\nname = "shop-api"\ndependencies = ["django>=5", "openai"]\n')
    write(repo, "services/api/api/urls.py", "urlpatterns = []\n")
    write(repo, "services/api/billing/models.py", "class Invoice:\n    amount = 0\n    currency = 'usd'\n")
    write(repo, "services/api/lists/subscriptions.py", "def subscriptions(list_id):\n    return subscription_rows\n")
    write(repo, "services/api/Dockerfile", "FROM python:3.12\nCMD [\"uvicorn\", \"api.asgi:application\"]\n")
    write(repo, "services/api/api/asgi.py", "application = None\n")
    write(repo, "apps/web/app/layout.tsx", "export default function Layout() {}\n")
    write(repo, "apps/web/src/components/ui/Card/index.ts", "export {};\n")  # a barrel, not an entry point
    write(repo, "docs/adr/0001-preorders-are-paid-upfront.md", "# Preorders are paid upfront\n")
    write(repo, "AGENTS.md", "# Notes for agents\n")
    write(repo, "LICENSE.md", "MIT\n")
    write(repo, ".env.example", "STRIPE_SECRET_KEY=\n")
    write(repo, "tools/package.json", json.dumps({"name": "release-tools", "devDependencies": {"release-it": "1"}}))
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
        apps = [(a["service"], a["language"], a.get("role", "")[:7]) for a in self.result["apps"]]
        self.assertEqual(apps, [("web", "node", ""), ("api", "python", ""), ("release-tools (tooling)", "node", "tooling")])
        self.assertEqual([f["name"] for f in self.result["apps"][0]["frameworks"]], ["next"])
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
        # Money by what a file says and imports, not its name: list subscriptions are not payments.
        self.assertEqual(read["money"], ["apps/web/pages/api/checkout.ts", "services/api/billing/models.py"])
        self.assertEqual(read["users and access"], ["apps/web/src/lib/auth.ts"])
        # The Dockerfile's command names the real entry point; a UI barrel file is not one.
        self.assertEqual(read["entry points"][:2], ["services/api/api/asgi.py", "services/api/Dockerfile"])
        self.assertIn("apps/web/app/layout.tsx", read["entry points"])
        self.assertNotIn("apps/web/src/components/ui/Card/index.ts", read["entry points"])
        self.assertEqual(read["configuration"], [".env.example", "compose.yml", "services/api/Dockerfile"])
        self.assertFalse(any("tests/" in p for paths in read.values() for p in paths))
        self.assertEqual(self.result["docs"], ["README.md", "AGENTS.md", "docs/adr/0001-preorders-are-paid-upfront.md"])

    def test_datastores_inferred_from_drivers_and_schemas_without_compose(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            write(repo, "pyproject.toml", '[project]\nname = "store"\ndependencies = [\n  "django-celery-beat",\n'
                                          '  "celery[redis]>=5",\n  "psycopg[binary]",\n]\n')
            write(repo, "prisma/schema.prisma", 'datasource db {\n  provider = "mysql"\n}\nmodel User {\n  id Int @id\n}\n')
            result = overview.scan(repo)
            self.assertEqual([(d["name"], d["inferred_from"], d["evidence"]["line"]) for d in result["datastores"]],
                             [("postgres", "the psycopg library", 6)])
            celery = next(h for h in result["integrations"]["data and queues"] if h["library"] == "celery")
            self.assertEqual(celery["evidence"]["line"], 5)  # not django-celery-beat on line 4
            draft = overview.draft(result)
            self.assertEqual([(s["name"], s["basis"]) for s in draft["systems"] if s["kind"] == "datastore"],
                             [("postgres", "derived_from_source")])

    def test_compose_anchors_and_names_repeated_across_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            write(repo, "docker-compose.yml", "x-env: &env\n  POSTGRES_USER: app\nservices:\n  db:\n"
                                              "    image: postgres:17\n    environment:\n      <<: *env\n")
            write(repo, "dev/docker-compose.yml", "services:\n  db:\n    image: postgres:18\n")
            result = overview.scan(repo)
            self.assertEqual([(d["name"], d["compose"]) for d in result["datastores"]],
                             [("db", "docker-compose.yml"), ("db", "dev/docker-compose.yml")])
            names = [s["name"] for s in overview.draft(result)["systems"]]
            self.assertEqual(names, ["db", "db (dev/docker-compose.yml)"])

    def test_round_two_shapes_workspaces_overrides_and_dev_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            # A uv workspace root holds only dev groups; the app is in backend/, built from the root context.
            write(repo, "pyproject.toml", '[dependency-groups]\ndev = ["ruff"]\n[tool.uv.workspace]\nmembers = ["backend"]\n')
            write(repo, "backend/pyproject.toml", '[project]\nname = "app"\ndependencies = ["fastapi", "emails", "pwdlib"]\n')
            write(repo, "backend/app/main.py", "from fastapi import FastAPI\n\napp = FastAPI(title='x')\n")
            write(repo, "compose.yml", "services:\n  backend:\n    build:\n      context: .\n"
                                       "      dockerfile: backend/Dockerfile\n  db:\n    image: postgres:18\n")
            write(repo, "compose.override.yml", "services:\n  backend:\n    ports: ['8000:8000']\n"
                                                "  mailpit:\n    image: axllent/mailpit\n")
            write(repo, "frontend/package.json", json.dumps({"name": "frontend", "dependencies": {"react": "19"},
                                                             "devDependencies": {"@react-email/components": "1"}}))
            write(repo, ".cursor/rules/architecture.mdc", "# How the pieces fit\n")
            result = overview.scan(repo)
            apps = {a["folder"]: (a["service"], a.get("role", "")[:7]) for a in result["apps"]}
            self.assertEqual(apps["."], (f"{repo.name} (tooling)", "tooling"))  # a root app takes the repo name
            self.assertEqual(apps["backend"], ("backend", ""))  # the compose service builds backend/
            self.assertEqual([s["name"] for s in result["services"]], ["backend", "mailpit"])  # one backend, not its override
            self.assertEqual([f["name"] for f in result["apps"][1]["frameworks"]], ["fastapi"])
            libraries = {h["library"] for hits in result["integrations"].values() for h in hits}
            self.assertEqual(libraries & {"emails", "pwdlib", "@react-email/components"}, {"emails", "pwdlib"})
            self.assertEqual(result["read_first"]["entry points"][0], "backend/app/main.py")
            self.assertEqual(result["docs"][0], ".cursor/rules/architecture.mdc")
            self.assertIn("compose.override.yml", result["read_first"]["configuration"])

    def test_a_repository_of_documents(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            write(repo, "README.md", "# Handbook\n\n- [How we work](./how-we-work.md)\n- [Benefits](benefits.md)\n")
            write(repo, "how-we-work.md", "# How we work\n\nSix-week cycles.\n")
            write(repo, "benefits.md", "# Benefits\n\nSalary at the top 10%, a $3,000 stipend, retirement match, "
                                       "insurance premiums paid, profit sharing and severance pay.\n")
            write(repo, "zebra.md", "# Zebra\n\nNotes.\n")
            write(repo, "LICENSE.md", "Permission is hereby granted, free of charge, to any person obtaining a copy...\n")
            (repo / ".git").mkdir()
            (repo / ".git" / "shallow").write_text("0" * 40 + "\n")
            result = overview.scan(repo)
            self.assertEqual(result["docs"], ["README.md", "how-we-work.md", "benefits.md", "zebra.md"])  # README's order
            self.assertEqual(result["read_first"]["money"], ["benefits.md"])
            self.assertIn("shallow", result["history"])
            draft = overview.draft(result)
            self.assertEqual([(e["path"], e["start"]) for e in draft["evidence"]],
                             [("README.md", 1), ("how-we-work.md", 1), ("benefits.md", 1), ("zebra.md", 1)])
            self.assertIn("documents, not code", subprocess.run(
                [sys.executable, str(SCRIPTS / "overview.py"), "scan", "--repo", str(repo), "--out", str(repo / "ov")],
                capture_output=True, text=True, check=True).stdout)

    def test_a_spring_app_from_maven(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            write(repo, "pom.xml", "<project><artifactId>clinic</artifactId><dependencies>\n"
                                   "<dependency>\n<groupId>org.springframework.boot</groupId>\n"
                                   "<artifactId>spring-boot-starter-webmvc</artifactId>\n</dependency>\n"
                                   "<dependency>\n<groupId>org.postgresql</groupId>\n<artifactId>postgresql</artifactId>\n"
                                   "</dependency>\n</dependencies></project>\n")
            write(repo, "src/main/java/clinic/ClinicApplication.java", "@SpringBootApplication\npublic class ClinicApplication {}\n")
            write(repo, "src/main/resources/application.properties", "spring.datasource.url=jdbc:postgresql://db/clinic\n")
            write(repo, "k8s/app.yml", "apiVersion: v1\nkind: Service\nmetadata:\n  name: clinic\n---\n"
                                       "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: clinic\nspec:\n"
                                       "  template:\n    spec:\n      containers:\n      - name: app\n"
                                       "        image: example/clinic\n---\napiVersion: apps/v1\nkind: StatefulSet\n"
                                       "metadata:\n  name: clinic-db\nspec:\n  template:\n    spec:\n      containers:\n"
                                       "      - image: postgres:18\n")
            write(repo, "src/main/java/clinic/OwnerRepository.java", "interface OwnerRepository extends JpaRepository {}\n")
            result = overview.scan(repo)
            self.assertEqual([f["name"] for f in result["apps"][0]["frameworks"]], ["spring-boot-starter-webmvc"])
            self.assertEqual([(s["name"], s["evidence"]["line"]) for s in result["services"]], [("clinic", 4)])
            self.assertEqual([d["name"] for d in result["datastores"]], ["clinic-db"])  # k8s runs postgres; nothing inferred
            self.assertIn("src/main/java/clinic/OwnerRepository.java", result["read_first"]["data model"])
            self.assertIn("k8s/app.yml", result["read_first"]["configuration"])
            self.assertEqual(result["read_first"]["entry points"], ["src/main/java/clinic/ClinicApplication.java"])
            self.assertIn("src/main/resources/application.properties", result["read_first"]["configuration"])

    def test_a_monorepo_past_the_discovery_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            write(repo, "package.json", json.dumps({"name": "mono", "devDependencies": {"turbo": "2"}}))
            write(repo, "apps/web/package.json", json.dumps({"name": "web", "dependencies": {"next": "15", "stripe": "1"},
                                                             "scripts": {"stripe:listen": "stripe listen"}}))
            for n in range(40):
                write(repo, f"packages/app-store/app{n}/package.json", json.dumps({"name": f"app{n}"}))
            write(repo, "docker-compose.yml", "services:\n  db:\n    image: postgres:18\n  db_is_ready:\n"
                                              "    image: postgres:18\n")
            budget = overview.discover_repo.MAX_MANIFESTS
            overview.discover_repo.MAX_MANIFESTS = 10
            try:
                result = overview.scan(repo)
            finally:
                overview.discover_repo.MAX_MANIFESTS = budget
            self.assertIn("compose files and workflows directly", result["discovery_limited"])
            self.assertEqual([d["name"] for d in result["datastores"]], ["db"])  # not the readiness helper
            family = next(a for a in result["apps"] if a.get("members"))
            self.assertEqual((family["folder"], family["service"]), ("packages/app-store/*", "app-store (40 packages)"))
            self.assertEqual(len(result["apps"]), 3)
            stripe = result["integrations"]["payments"][0]["evidence"]
            self.assertIn('"stripe": "1"', stripe["text"])  # the dependency, not the stripe:listen script

    def test_the_default_database_in_env_example(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            write(repo, ".env.example", "APP_NAME=Contacts\nDB_CONNECTION=sqlite\n")
            result = overview.scan(repo)
            self.assertEqual([(d["name"], d["inferred_from"], d["evidence"]["line"]) for d in result["datastores"]],
                             [("sqlite", "the default in .env.example", 2)])

    def test_cli_scan_prints_a_summary_not_the_whole_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_shop(Path(tmp))
            out = Path(tmp) / "ov"
            run = subprocess.run([sys.executable, str(SCRIPTS / "overview.py"), "scan", "--repo", str(repo), "--out",
                                  str(out)], capture_output=True, text=True, check=True)
            printed = json.loads(run.stdout)
            self.assertEqual(sorted(printed), ["found", "kept", "next", "status", "written"])
            self.assertIn("web (node in apps/web: next)", printed["found"]["apps"])
            self.assertIn(".gitignore", printed["next"])
            self.assertIn("read_first", json.loads((out / "scan.json").read_text()))

    def test_an_alias_bomb_in_compose_is_refused_not_expanded(self):
        bomb = "a: &a [x, x, x, x, x, x, x, x, x, x]\n" + "".join(
            f"{chr(98 + i)}: &{chr(98 + i)} [{', '.join(['*' + chr(97 + i)] * 10)}]\n" for i in range(8))
        with self.assertRaisesRegex(overview.otel_setup.SetupError, "expands beyond"):
            overview.otel_setup.load_compose(bomb.encode())
        with self.assertRaisesRegex(overview.otel_setup.SetupError, "tags"):
            overview.otel_setup.load_compose(b"services: !!python/object:os.system {}\n")

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
        self.umask = os.umask(0)
        os.umask(self.umask)
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

    def test_the_fields_the_page_shows_are_checked_or_defaulted(self):
        blueprint = self.filled()
        blueprint["risks"] = [{"id": "r1", "name": "Webhook missed", "description": "No retry", "basis": "proposed"}]
        with self.assertRaisesRegex(business_xray.BlueprintError, "r1.severity must be one of"):
            overview.build(self.repo, self.save(blueprint), self.root / "x.html")
        blueprint["risks"][0]["severity"] = "high"
        blueprint["improvements"] = [{"id": "i1", "name": "Retry webhooks", "basis": "proposed", "priority": "urgent"}]
        with self.assertRaisesRegex(business_xray.BlueprintError, "priority must be P0"):
            overview.build(self.repo, self.save(blueprint), self.root / "x.html")
        del blueprint["improvements"][0]["priority"]
        blueprint["purpose"] += " It has a TODO list for bakers."  # only a value that starts with TODO is a placeholder
        result = overview.build(self.repo, self.save(blueprint), self.root / "x.html", self.root / "x.json")
        project = json.loads((self.root / "x.json").read_text())["projects"][0]
        self.assertEqual((project["money_flows"][0]["state"], project["improvements"][0]["priority"],
                          project["improvements"][0]["addresses"]), ("described", "P2", []))
        self.assertTrue(result["open"].startswith("file://"))
        self.assertIn("checkout apps/web/pages/api/checkout.ts:2-3  // Pro plan, $29/month.  ...  "
                      "export default async function handler(req, res) {}", result["check_citations"])
        page = (self.root / "x.html").read_text()
        self.assertIn("renderSystems", page)
        # The drafted README line and the README citation the test added are cited by nothing.
        self.assertEqual(result["unused_citations"], ["e9", "readme"])
        self.assertEqual(oct((self.root / "x.html").stat().st_mode & 0o777), oct(0o666 & ~self.umask))

    def test_the_citation_check_reads_the_file_not_the_kept_quote(self):
        long_lines = "".join(f"line {n} " + "x" * 300 + "\n" for n in range(1, 11))
        write(self.repo, "policy.md", long_lines)
        blueprint = self.filled()
        blueprint["evidence"].append({"id": "policy", "repo": "local", "path": "policy.md", "start": 2, "end": 9})
        blueprint["unknowns"] = [{"id": "u1", "name": "Policy", "description": "d", "evidence": ["policy"],
                                  "basis": "observed_in_source"}]
        result = overview.build(self.repo, self.save(blueprint), self.root / "y.html", self.root / "y.json")
        check = next(c for c in result["check_citations"] if c.startswith("policy "))
        self.assertTrue(check.startswith("policy policy.md:2-9  line 2 ") and "...  line 9 " in check, check)

    def test_a_shared_page_links_to_github_at_the_commit(self):
        overview.build(self.repo, self.save(self.filled()), self.root / "s.html", self.root / "s.json",
                       github="bakery/shop")
        checkout = next(e for e in json.loads((self.root / "s.json").read_text())["projects"][0]["evidence"]
                        if e["id"] == "checkout")
        self.assertEqual(checkout["url"], f"https://github.com/bakery/shop/blob/{self.head}/"
                                          "apps/web/pages/api/checkout.ts#L2-L3")
        self.assertNotIn("file://", (self.root / "s.html").read_text())
        with self.assertRaisesRegex(overview.otel_setup.SetupError, "OWNER/NAME"):
            overview.build(self.repo, self.save(self.filled()), self.root / "s.html", github="https://x/y")

    def test_every_range_past_its_file_is_named_and_labels_may_quote_todo(self):
        blueprint = self.filled()
        blueprint["evidence"][-1]["label"] = "TODO: team bookings with PayPal"  # quoting a code comment is fine
        blueprint["evidence"].append({"id": "a", "repo": "local", "path": "README.md", "start": 1, "end": 40})
        blueprint["evidence"].append({"id": "b", "repo": "local", "path": "apps/web/pages/api/checkout.ts",
                                      "start": 1, "end": 30})
        with self.assertRaisesRegex(overview.otel_setup.SetupError, "a: README.md has 3 lines, not 40; b: "):
            overview.build(self.repo, self.save(blueprint), self.root / "x.html")

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
