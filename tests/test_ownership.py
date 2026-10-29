"""Ownership: CODEOWNERS (declared, line cited) and git history (inferred), per entity, and where it shows."""
from datetime import datetime, timezone
import importlib.util
import json
import os
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
import ownership as ow
from test_entity_context import API, DB, PAY, REFUND, WEB, bundle

spec = importlib.util.spec_from_file_location("ontologyex_ownership_demo", ROOT / "examples/operating-picture/demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)

CODEOWNERS = """# who answers for what
*                  @acme/everyone
/payments/         @acme/payments @alice   # money
*.md               docs@acme.example
[Optional section]
/payments/legacy.py
"""


def git(repo: Path, *args, who=None):
    name, email = who or ("Setup", "setup@example.invalid")
    env = {**os.environ, "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_NAME": name,
           "GIT_COMMITTER_EMAIL": email, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


def commit(repo: Path, rel: str, text: str, who: tuple[str, str]):
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(text)
    git(repo, "add", rel, who=who)
    git(repo, "commit", "-q", "-m", f"change {rel}", who=who)


def owned_repo(root: Path, codeowners: bool = True) -> Path:
    """Alice and Bob change payments, Carol alone changes web, a bot touches everything."""
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    alice, bob, carol = ("Alice Ng", "alice@example.invalid"), ("Bob Ruiz", "bob@example.invalid"), \
        ("Carol Li", "carol@example.invalid")
    if codeowners:
        commit(repo, ".github/CODEOWNERS", CODEOWNERS, alice)
    commit(repo, "compose.yml", "services:\n  api:\n    build: ./payments\n", alice)
    commit(repo, "payments/app.py", "def pay():\n    return 1\n", alice)
    commit(repo, "payments/app.py", "def pay():\n    return 2\n", alice)
    commit(repo, "payments/app.py", "def pay():\n    return 3\n", bob)
    commit(repo, "payments/legacy.py", "OLD = 1\n", bob)
    commit(repo, "web/index.js", "1\n", carol)
    commit(repo, "web/index.js", "2\n", carol)
    commit(repo, "req.md", "Customers pay by card.\n", carol)
    commit(repo, "payments/app.py", "def pay():\n    return 4\n", ("dependabot[bot]", "bot@example.invalid"))
    return repo


def model() -> dict:
    """The small bundle with Pay's binding in payments/app.py, web declared in web/index.js, Refund running on api."""
    data = bundle()
    data["business"]["actions"][0]["bindings"] = [{"id": "b.pay", "components": [API],
                                                   "evidence": [{"path": "payments/app.py", "start_line": 1}]}]
    data["business"]["objects"][0]["systems"] = [API]
    data["state"]["source_evidence"].append({"id": "e3", "path": "web/index.js", "sha256": "cc",
                                             "source_kind": "MANIFEST", "authenticity": "UNVERIFIED"})
    for node in data["state"]["nodes"]:
        if node["id"] == WEB:
            node["evidence_id"] = "e3"
    return data


class PatternTests(unittest.TestCase):
    def test_codeowners_patterns_follow_gitignore_rules(self):
        cases = [("*", "a/b/c.py", True), ("*.py", "a/b.py", True), ("*.py", "b.py", True), ("*.py", "b.js", False),
                 ("/docs/*", "docs/x.md", True), ("/docs/*", "docs/a/x.md", False), ("/docs/", "docs/a/x.md", True),
                 ("apps/", "x/apps/y.js", True), ("/apps/", "x/apps/y.js", False), ("**/logs", "a/logs/x", True),
                 ("docs/**/*.md", "docs/a/b/c.md", True), ("/payments/", "payments/", True),
                 ("/payments/", "paymentsx/a", False), ("src/app", "src/app/main.go", True)]
        for pattern, path, expected in cases:
            self.assertEqual(bool(ow._pattern(pattern).match(path)), expected, (pattern, path))

    def test_last_match_wins_and_an_empty_rule_unowns(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".github").mkdir()
            (Path(tmp) / ".github/CODEOWNERS").write_text(CODEOWNERS)
            rules = ow.read_codeowners(Path(tmp))
            self.assertEqual(rules["path"], ".github/CODEOWNERS")
            self.assertEqual(len(rules["rules"]), 4)  # comment and section header skipped
            found = ow.declared_owner(rules, "payments/app.py")
            self.assertEqual((found["owners"], found["line"]), (["@acme/payments", "@alice"], 3))  # trailing comment cut
            self.assertEqual(ow.declared_owner(rules, "README.md")["owners"], ["docs@acme.example"])
            self.assertEqual(ow.declared_owner(rules, "payments/legacy.py")["owners"], [])  # explicitly unowned
            self.assertEqual(ow.declared_owner(rules, "web/a.js")["owners"], ["@acme/everyone"])


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ontologyex-owners-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def scan(self, codeowners=True):
        found = ow.scan_repo(owned_repo(self.tmp, codeowners), datetime.now(timezone.utc))
        found["folders"] = {API: "payments"}  # the small bundle's IDs are not compose IDs
        return found

    def test_history_counts_people_not_bots_or_addresses(self):
        found = self.scan()
        self.assertEqual(found["git"], "OK")
        self.assertEqual({n: c for n, (c, _) in found["activity"]["payments/app.py"].items()},
                         {"Alice Ng": 2, "Bob Ruiz": 1})
        self.assertNotIn("example.invalid", json.dumps(found["activity"]))

    def test_declared_owners_cite_the_line_and_history_ranks_people(self):
        found = ow.report(model(), self.scan())
        rows = {r["id"]: r for r in found["entities"]}
        pay = rows[PAY]
        self.assertEqual((pay["status"], pay["owner"], pay["standing"], pay["basis"]),
                         ("DECLARED", "@acme/payments", "SOURCE", "CODEOWNERS"))
        self.assertEqual((pay["declared"][0]["path"], pay["declared"][0]["line"], pay["files"][0]["basis"]),
                         (".github/CODEOWNERS", 3, "BINDING_SOURCE"))
        self.assertEqual([a["name"] for a in pay["active"]], ["Alice Ng", "Bob Ruiz"])
        api = rows[API]  # the folder its compose file builds
        self.assertEqual((api["files"][0], api["owner"]), ({"path": "payments/", "basis": "SERVICE_FOLDER"}, "@acme/payments"))
        self.assertEqual(rows[REFUND]["through"], [API])  # no file of its own: owned through what it runs on
        self.assertEqual(rows[REFUND]["owner"], "@acme/payments")
        self.assertEqual(rows[DB]["owner"], "@acme/everyone")  # declared in compose.yml, which only * covers
        self.assertEqual(found["contributors"], 3)
        people = {p["owner"]: [o["id"] for o in p["owns"]] for p in found["people"]}
        self.assertIn(PAY, people["@acme/payments"])
        self.assertEqual(found["coverage"][0]["percent"], 100)

    def test_without_codeowners_owners_are_inferred_and_gaps_show(self):
        data = model()
        data["business"]["actions"].append({"key": "s:action:Ship", "id": "Ship", "skill": "s", "health": "UNLINKED",
                                            "bindings": [], "evidence": [{"path": "nowhere.md", "quote": "x"}]})
        found = ow.report(data, self.scan(codeowners=False))
        rows = {r["id"]: r for r in found["entities"]}
        self.assertEqual((rows[PAY]["status"], rows[PAY]["owner"], rows[PAY]["standing"]),
                         ("ACTIVE_ONLY", "Alice Ng", "INFERRED"))
        self.assertEqual(rows[WEB]["risks"], ["ONE_PERSON"])  # only Carol, twice
        self.assertEqual((rows["s:action:Ship"]["status"], rows["s:action:Ship"]["risks"]), ("NO_OWNER", []))
        self.assertIsNone(found["codeowners"])

    def test_the_command_center_names_owners_and_flags_gaps(self):
        data = model()
        data["business"]["actions"].append({"key": "s:action:Ship", "id": "Ship", "skill": "s", "health": "UNLINKED",
                                            "bindings": [], "evidence": []})
        data["ownership"] = ow.report(data, self.scan(codeowners=False))
        found = cc.report(data)
        kinds = {i["kind"]: i for i in found["items"]}
        self.assertEqual(kinds["NO_OWNER"]["title"], "Nobody owns Ship")
        self.assertIn("NO_CODEOWNERS", kinds)
        self.assertNotIn("ONE_PERSON_REPOSITORY", kinds)
        self.assertEqual(kinds["AT_RISK"]["owner"]["owner"], "Alice Ng")  # Pay is at risk; Alice changed it most
        rows = {r["label"]: r for r in found["coverage"]["rows"]}
        self.assertEqual((rows["Business actions with any owner"]["part"],
                          rows["Business actions with any owner"]["whole"]), (1, 2))

    def test_one_person_repository_is_one_item_not_many(self):
        data = model()
        scan = self.scan()
        scan["activity"] = {p: {"Alice Ng": [5, "2026-10-01T00:00:00+00:00"]} for p in scan["activity"]}
        data["ownership"] = ow.report(data, scan)
        kinds = [i["kind"] for i in cc.report(data)["items"]]
        self.assertEqual(kinds.count("ONE_PERSON_REPOSITORY"), 1)
        self.assertNotIn("ONE_PERSON", kinds)


class ServedTests(unittest.TestCase):
    def test_owners_route_page_entity_and_agent_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            built = demo.build(Path(tmp) / "demo")
            repo = Path(built["repo"])
            (repo / ".github").mkdir(exist_ok=True)
            (repo / ".github/CODEOWNERS").write_text("*  @acme/platform\n")
            git(repo, "init", "-q", "-b", "main")
            git(repo, "add", "-A")
            git(repo, "commit", "-q", "-m", "demo", who=("Dana Fox", "dana@example.invalid"))
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
            with urllib.request.urlopen(base + "/api/owners", timeout=30) as resp:
                found = json.load(resp)
            self.assertEqual((found["status"], found["codeowners"], found["git"], found["contributors"]),
                             ("OK", ".github/CODEOWNERS", "OK", 1))
            payments = next(r for r in found["entities"] if r["label"] == "payments")
            self.assertEqual((payments["owner"], payments["declared"][0]["line"]), ("@acme/platform", 1))
            with urllib.request.urlopen(base + "/api/owners?id=" + urllib.request.quote(payments["id"]), timeout=30) as r:
                self.assertEqual([e["id"] for e in json.load(r)["entities"]], [payments["id"]])
            with urllib.request.urlopen(base + "/owners", timeout=10) as resp:
                self.assertIn(b"Owners", resp.read())
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(base + "/api/owners?who=x", timeout=10)
            self.assertEqual(caught.exception.code, 400)
            with urllib.request.urlopen(base + "/api/command", timeout=30) as resp:
                command = json.load(resp)
            self.assertEqual(command["items"][0]["owner"]["owner"], "@acme/platform")  # payments is down: who to call
            self.assertIn("ONE_PERSON_REPOSITORY", [i["kind"] for i in command["items"]])
            tools = at.Tools(at.Source(base, None), at.load_policy(None),
                             at.Audit(Path(tmp) / "audit.jsonl", "claude", "Dana"), None)
            one = tools.call("who_owns", {"id": payments["id"]})
            self.assertEqual([e["owner"] for e in one["entities"]], ["@acme/platform"])
            with self.assertRaises(at.ToolError) as caught:
                tools.call("who_owns", {"id": "no:such:entity"})
            self.assertEqual(caught.exception.status, "NOT_FOUND")
            policy = Path(tmp) / "policy.json"
            policy.write_text(json.dumps({"schema_version": 1, "allow_tools": ["who_owns"],
                                          "deny_entities": [payments["id"]]}))
            hidden = at.Tools(at.Source(base, None), at.load_policy(policy),
                              at.Audit(Path(tmp) / "audit.jsonl", "claude", "Dana"), None).call("who_owns", {})
            self.assertNotIn(payments["id"], json.dumps(hidden))
            self.assertTrue(hidden["entities"])


if __name__ == "__main__":
    unittest.main()
