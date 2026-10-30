"""The code surface read from syntax trees (ontologyex-discover surface, tree-sitter).

Every expectation of test_code_surface holds when Python, JS/TS and Go are read from trees;
then what only trees get right: comments and strings are not code, calls and decorators span
lines, each router keeps its own prefix, chains and route objects are routes, re-exported
handlers count. Without the engine, or with one that predates it, the patterns answer.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ontology-extraction/scripts"))
sys.path.insert(0, str(ROOT / "tests"))
import code_surface
import native_view_bridge
import test_code_surface

BINARY = native_view_bridge.engine_binary("ontologyex-discover")


def has_trees() -> bool:
    try:
        native_view_bridge.native_surface(ROOT, [], BINARY)
        return True
    except native_view_bridge.NativeViewError:
        return False


AVAILABLE = has_trees()
# CI sets ONTOLOGYEX_REQUIRE_TREES where the engine is built, so a broken build fails instead of skipping.
NEEDS = unittest.skipUnless(AVAILABLE or os.environ.get("ONTOLOGYEX_REQUIRE_TREES"),
                            "build the engine (cargo build --manifest-path engine/Cargo.toml --bins)")

TRICKY = {
    # Docstrings and comments are not code; a decorator spans lines; each router keeps its own prefix.
    "py/api.py": '"""Example:\n\n@app.get("/from-a-docstring")\n"""\nusers = APIRouter(prefix="/users")\n'
                 'admin = APIRouter(prefix="/admin")\n# @app.post("/commented")\n@users.get(\n    "/{id}",\n)\n'
                 'def read(id): ...\n@admin.delete("/{id}")\ndef drop(id): ...\n'
                 'note = "__tablename__ = \'not_a_table\'"\n',
    # Flask: a blueprint registered with a prefix in the same file.
    "py/views.py": 'bp = Blueprint("shop", __name__)\n@bp.route("/cart", methods=["GET", "POST"])\ndef cart(): ...\n'
                   'app.register_blueprint(bp, url_prefix="/shop")\n',
    # Express: a router mounted under a prefix, a route chain, a Fastify route object, a JSDoc example.
    "js/server.js": "/**\n * app.get('/from-a-comment', h)\n */\nconst billing = express.Router();\n"
                    "billing.route('/invoices')\n  .get(list)\n  .post(create);\nbilling.get(\n  '/invoices/:id',\n  show,\n);\n"
                    "app.use('/api/billing', billing);\nfastify.route({ method: ['GET', 'HEAD'], url: '/status' });\n",
    # Next.js: a handler re-exported under a method name.
    "web/app/api/report/route.ts": "async function handler(req) {}\nexport { handler as GET, handler as POST };\n",
    # Go: gin groups, chi sub-routers, gorilla path prefixes and methods, raw strings, a commented route.
    "go/main.go": "package main\n\nfunc main() {\n\tv1 := r.Group(\"/v1\")\n\tadmin := v1.Group(\"/admin\")\n"
                  "\tadmin.GET(`/users`, list)\n\tr.Route(\"/articles\", func(r chi.Router) {\n\t\tr.Get(\"/{id}\", show)\n\t})\n"
                  "\tapi := m.PathPrefix(\"/api\").Subrouter()\n\tapi.HandleFunc(\"/orders\", orders).Methods(\"POST\", \"PUT\")\n"
                  "\t// r.Get(\"/commented\", h)\n}\n",
}


def make(root: Path, files: dict) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


@NEEDS
class TreeScan(test_code_surface.Scan):
    """Every expectation of the pattern reader, read from trees instead."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.patterns = cls.result
        cls.result = code_surface.scan(cls.repo, BINARY)

    def test_read_from_trees_with_the_same_answers(self):
        self.assertEqual(self.result["parser"]["name"], "tree-sitter")
        self.assertGreater(self.result["parser"]["files"], 20)
        for kind in ("routes", "operations", "tables", "jobs"):
            key = lambda r: json.dumps(r, sort_keys=True)
            self.assertEqual(sorted(map(key, self.result[kind])), sorted(map(key, self.patterns[kind])), kind)


@NEEDS
class WhatOnlyTreesSee(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        repo = make(Path(cls.tmp.name), TRICKY)
        cls.trees = code_surface.scan(repo, BINARY)
        cls.patterns = code_surface.scan(repo)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def routes(self, result, prefix):
        return sorted((r["method"], r["path"], r["line"]) for r in result["routes"] if r["file"].startswith(prefix))

    def test_python(self):
        self.assertEqual(self.routes(self.trees, "py/"), [
            ("DELETE", "/admin/{id}", 12, ), ("GET", "/shop/cart", 2), ("GET", "/users/{id}", 8), ("POST", "/shop/cart", 2)])
        self.assertEqual(self.trees["tables"], [])  # the patterns take a table name out of a string
        self.assertIn(("GET", "/from-a-docstring", 3), self.routes(self.patterns, "py/"))

    def test_javascript(self):
        self.assertEqual(self.routes(self.trees, "js/"), [
            ("GET", "/api/billing/invoices", 6), ("GET", "/api/billing/invoices/:id", 8), ("GET", "/status", 13),
            ("HEAD", "/status", 13), ("POST", "/api/billing/invoices", 7)])
        self.assertEqual(self.routes(self.trees, "web/"), [("GET", "/api/report", 2), ("POST", "/api/report", 2)])
        self.assertEqual(self.routes(self.patterns, "web/"), [("ANY", "/api/report", 1)])

    def test_go(self):
        self.assertEqual(self.routes(self.trees, "go/"), [
            ("GET", "/articles/{id}", 8), ("GET", "/v1/admin/users", 6), ("POST", "/api/orders", 11),
            ("PUT", "/api/orders", 11)])
        self.assertIn(("GET", "/commented", 12), self.routes(self.patterns, "go/"))


class Fallback(unittest.TestCase):
    def test_without_the_engine_the_patterns_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make(Path(tmp) / "repo", test_code_surface.FILES)
            old = Path(tmp) / "ontologyex-discover"  # an engine from before the surface command
            old.write_text("#!/bin/sh\necho 'usage: ontologyex-discover --repo REPO' >&2\nexit 2\n")
            old.chmod(0o755)
            found = code_surface.scan(repo, old)
            self.assertEqual(found["parser"]["name"], "patterns")
            self.assertIn("cannot read code surfaces", found["parser"]["note"])
            plain = code_surface.scan(repo)
            self.assertEqual(plain["parser"], {"name": "patterns"})
            self.assertEqual(found["routes"], plain["routes"])
            missing = code_surface.scan(repo, Path(tmp) / "nowhere")
            self.assertEqual(missing["parser"]["name"], "patterns")

    @NEEDS
    def test_the_engine_reads_only_files_inside_the_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            outside = Path(tmp) / "secret.py"
            outside.write_text("@app.get('/outside')\ndef f(): ...\n")
            os.symlink(outside, repo / "link.py")
            (repo / "ok.py").write_text("@app.get('/inside')\ndef f(): ...\n")
            found = native_view_bridge.native_surface(repo, ["link.py", "ok.py"], BINARY)
            self.assertEqual(found["not_parsed"], ["link.py"])
            self.assertEqual([r["path"] for r in found["files"]["ok.py"]["routes"]], ["/inside"])
            for bad in (["../secret.py"], ["/etc/passwd"], ["a/../../secret.py"]):
                with self.assertRaises(native_view_bridge.NativeViewError):
                    native_view_bridge.native_surface(repo, bad, BINARY)
            run = subprocess.run([str(BINARY), "surface"], input=b"not json", capture_output=True, timeout=30)
            self.assertEqual(run.returncode, 1)


if __name__ == "__main__":
    unittest.main()
