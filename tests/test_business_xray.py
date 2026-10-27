"""Offline business X-Ray contract / source-provenance regressions."""
from __future__ import annotations

import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "business/build_blueprints.py"
spec = importlib.util.spec_from_file_location("ontologyex_business_xray", MODULE)
business = importlib.util.module_from_spec(spec)
spec.loader.exec_module(business)
EXAMPLES = ROOT / "business/examples"


def load(name):
    return business.read(EXAMPLES / name)


class BusinessBlueprintTests(unittest.TestCase):
    def test_all_public_business_models_compile(self):
        sources = [load("repoing.json"), load("ohiyo.json"), load("ontologyex.json")]
        result = business.compile_blueprints(sources)
        self.assertEqual(result["totals"]["projects"], 3)
        self.assertGreater(result["totals"]["journeys"], 5)
        self.assertGreater(result["totals"]["steps"], 20)
        self.assertEqual(result["trust"], "REVIEWED_SOURCES_NOT_LIVE_OPERATIONS")
        self.assertFalse(result["execution_authorized"])
        self.assertTrue(all(
            e["url"].startswith("https://github.com/") and "/blob/" in e["url"]
            for p in result["projects"] for e in p["evidence"]))

    def test_repo_business_questions_have_distinct_uncertainty(self):
        p = load("repoing.json")
        business.validate(p)
        self.assertGreaterEqual(len(p["audiences"]), 3)
        self.assertGreaterEqual(len(p["money_flows"]), 2)
        self.assertGreaterEqual(len(p["improvements"]), 1)
        self.assertTrue(all(x["basis"] == "unknown" for x in p["unknowns"]))

    def test_duplicate_evidence_is_refused(self):
        p = load("repoing.json")
        p["evidence"].append(copy.deepcopy(p["evidence"][0]))
        with self.assertRaises(business.BlueprintError):
            business.validate(p)

    def test_missing_link_is_refused(self):
        p = load("repoing.json")
        p["journeys"][0]["steps"][0]["evidence"] = ["E-invented"]
        with self.assertRaises(business.BlueprintError):
            business.validate(p)

    def test_unverified_business_claim_cannot_claim_source_observation(self):
        p = load("repoing.json")
        p["value"][0]["evidence"] = []
        p["value"][0]["basis"] = "observed_in_source"
        with self.assertRaises(business.BlueprintError):
            business.validate(p)

    def test_missing_risk_target_is_refused(self):
        p = load("repoing.json")
        p["improvements"][0]["addresses"].append("invented")
        with self.assertRaises(business.BlueprintError):
            business.validate(p)

    def test_unrelated_duplicate_project_ids_refused(self):
        a = load("repoing.json")
        with self.assertRaises(business.BlueprintError):
            business.compile_blueprints([a, copy.deepcopy(a)])

    def test_source_path_escape_refused(self):
        p = load("repoing.json")
        p["evidence"][0]["path"] = "../.env"
        with self.assertRaises(business.BlueprintError):
            business.validate(p)

    def test_unpinned_commit_refused(self):
        p = load("repoing.json")
        p["evidence"][0]["commit"] = "main"
        with self.assertRaises(business.BlueprintError):
            business.validate(p)

    def test_runtime_health_never_claimed_from_source_documents(self):
        p = load("repoing.json")
        compiled = business.compile_blueprints([p])
        self.assertEqual(compiled["projects"][0]["summary"]["runtime_coverage"], "NOT_MEASURED")
        self.assertEqual(compiled["projects"][0]["summary"]["business_completeness"], "UNKNOWN")

    def test_rendered_html_has_no_live_network_dependency(self):
        compiled = business.compile_blueprints([load("repoing.json")])
        result = business.render_html(compiled, ROOT / "business/business_xray_template.html")
        self.assertIn("Business X-Ray", result)
        self.assertIn('connect-src &#39;none&#39;', result) if False else None
        self.assertIn("connect-src 'none'", result)
        self.assertIn("github.com", result)
        self.assertIn("repo.ing", result)
        self.assertNotIn("__XRAY_DATA__", result)
        self.assertNotIn("New1Direction/FYNCH", result)
        self.assertNotIn("New1Direction/ImOrb", result)
        # HTML can embed safe text, but must never turn authored user content
        # into a script injection via the script JSON payload.
        self.assertNotIn("</script><script>alert(", result)

    def test_json_duplicate_key_parser(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "test.json"
            f.write_text('{"a":1,"a":2}')
            with self.assertRaises(business.BlueprintError):
                business.read(f)

    def test_embedded_public_example_has_no_private_projects(self):
        page = (ROOT / "business/index.html").read_text(encoding="utf-8")
        start = '<script id="blueprint-data" type="application/json">'
        self.assertIn(start, page)
        raw = page.split(start, 1)[1].split("</script>", 1)[0]
        parsed = json.loads(raw)
        self.assertTrue(all(p["id"] in (
            "New1Direction/repo.ing", "New1Direction/ohiyo",
            "New1Direction/OntologyEX") for p in parsed["projects"]))
        self.assertFalse(parsed["execution_authorized"])
        self.assertNotIn("New1Direction/FYNCH", page)
        self.assertNotIn("New1Direction/ImOrb", page)
        self.assertNotIn("New1Direction/colony", page)


if __name__ == "__main__":
    unittest.main()
