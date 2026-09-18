"""Offline CLI regressions. Run: python -m unittest discover -s tests -v"""
import copy
import contextlib
import io
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "ontology-extraction" / "scripts" / "scaffold.py"
LAYERS = {
    "10-upper.yaml": {"anchors": [{"id": "Agent"}]},
    "20-domain.yaml": {
        "classes": [{"id": "Customer", "upper": "Agent", "definition": "A customer.", "source": "fixture"}],
        "relations": [],
    },
    "30-task.yaml": {"tasks": [{"id": "ReadCustomer", "inputs": ["Customer"], "outputs": ["Customer"], "source": "fixture"}]},
    "40-application.yaml": {"concepts": [{"id": "customer_record", "binds": "Customer", "used_by_tasks": ["ReadCustomer"]}]},
}


class ScaffoldTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.data = copy.deepcopy(LAYERS)
        self.write_layers()

    def write_layers(self):
        for name, value in self.data.items():
            (self.workspace / name).write_text(yaml.safe_dump(value), encoding="utf-8")

    def cli(self, *args):
        # Exercise the actual parser/main in-process for fast table-driven tests.
        argv = [str(SCRIPT), *map(str, args)]
        stdout, stderr = io.StringIO(), io.StringIO()
        code = 0
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                runpy.run_path(str(SCRIPT), run_name="__main__")
            except SystemExit as exc:
                code = exc.code or 0
        return subprocess.CompletedProcess(argv, code, stdout.getvalue(), stderr.getvalue())

    def test_real_process_exit_codes(self):
        for target, expected in ((self.workspace, 0), (self.root / "missing", 1)):
            with self.subTest(target=target):
                result = subprocess.run(
                    [sys.executable, str(SCRIPT), "validate", str(target)],
                    capture_output=True, text=True, encoding="utf-8", timeout=10,
                )
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                self.assertNotIn("Traceback", result.stdout + result.stderr)

    def assert_rejected(self, expected, target=None):
        result = self.cli("validate", target or self.workspace)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 1, output)
        self.assertIn(expected, output)
        self.assertNotIn("Traceback", output)
        self.assertNotIn("0 errors, 0 warnings", output)
        return result

    def test_valid_workspace(self):
        result = self.cli("validate", self.workspace)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("0 errors, 0 warnings", result.stdout)

    def test_empty_directory_is_not_valid(self):
        empty = self.root / "empty"
        empty.mkdir()
        result = self.assert_rejected("10-upper.yaml", empty)
        for name in LAYERS:
            self.assertIn(name, result.stdout)
        self.assertIn("4 errors", result.stdout)

    def test_nonexistent_directory_is_not_valid(self):
        self.assert_rejected("existing directory", self.root / "missing")

    def test_regular_file_is_not_a_workspace(self):
        target = self.root / "file"
        target.write_text("not a directory", encoding="utf-8")
        self.assert_rejected("existing directory", target)

    def test_each_layer_is_required(self):
        for name in LAYERS:
            with self.subTest(name=name):
                self.write_layers()
                (self.workspace / name).unlink()
                self.assert_rejected(name)

    def test_directory_instead_of_yaml_is_rejected(self):
        path = self.workspace / "20-domain.yaml"
        path.unlink()
        path.mkdir()
        self.assert_rejected("20-domain.yaml: cannot read")

    def test_empty_and_scalar_yaml_documents_are_rejected(self):
        for text in ("", "# only a comment\n", "null", "false", "42", "[]", "{}", "hello", "[one, two]"):
            with self.subTest(text=text):
                (self.workspace / "20-domain.yaml").write_text(text, encoding="utf-8")
                self.assert_rejected("20-domain.yaml: expected a non-empty YAML mapping")

    def test_malformed_yaml_has_file_line_and_column(self):
        (self.workspace / "20-domain.yaml").write_text("classes: [\n", encoding="utf-8")
        self.assert_rejected("20-domain.yaml:2:1: invalid YAML")

    def test_multiple_yaml_documents_are_rejected(self):
        (self.workspace / "20-domain.yaml").write_text("classes: []\n---\nclasses: []\n", encoding="utf-8")
        self.assert_rejected("invalid YAML")

    def test_invalid_utf8_is_rejected(self):
        (self.workspace / "20-domain.yaml").write_bytes(b"\xff\xfe")
        self.assert_rejected("20-domain.yaml: cannot read")

    def test_unsafe_yaml_tags_are_rejected(self):
        (self.workspace / "20-domain.yaml").write_text("!!python/object:builtins.object {}", encoding="utf-8")
        self.assert_rejected("invalid YAML")

    def test_required_collection_keys(self):
        for name in LAYERS:
            with self.subTest(name=name):
                self.data = copy.deepcopy(LAYERS)
                self.data[name] = {"typo": []}
                self.write_layers()
                self.assert_rejected(f"{name}: missing required")

    def test_collection_types(self):
        for value in (None, False, "Customer", {}, 3):
            with self.subTest(value=value):
                self.data["20-domain.yaml"]["classes"] = value
                self.write_layers()
                self.assert_rejected("'classes' must be a list")

    def test_empty_anchors_and_classes_are_rejected(self):
        for name, key in (("10-upper.yaml", "anchors"), ("20-domain.yaml", "classes")):
            with self.subTest(name=name):
                self.data = copy.deepcopy(LAYERS)
                self.data[name][key] = []
                self.write_layers()
                self.assert_rejected("must contain at least one entry")

    def test_empty_task_and_application_collections_remain_supported(self):
        self.data["30-task.yaml"]["tasks"] = []
        self.data["40-application.yaml"]["concepts"] = []
        self.write_layers()
        result = self.cli("validate", self.workspace)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("orphan domain class", result.stdout)

    def test_non_mapping_entries_are_rejected(self):
        for value in (None, "Customer", ["Customer"], 12):
            with self.subTest(value=value):
                self.data["20-domain.yaml"]["classes"] = [value]
                self.write_layers()
                self.assert_rejected("classes[0] must be a mapping")

    def test_ids_must_be_nonempty_strings(self):
        for value in (None, "", "  ", [], {}, 123, True):
            with self.subTest(value=value):
                self.data["20-domain.yaml"]["classes"] = [{"id": value}]
                self.write_layers()
                self.assert_rejected("classes[0].id must be a non-empty string")

    def test_missing_id_is_rejected(self):
        del self.data["20-domain.yaml"]["classes"][0]["id"]
        self.write_layers()
        self.assert_rejected("classes[0].id")

    def test_reference_fields_are_strings(self):
        for name, key, field in (
            ("20-domain.yaml", "classes", "upper"),
            ("20-domain.yaml", "relations", "domain"),
            ("20-domain.yaml", "relations", "range"),
            ("40-application.yaml", "concepts", "binds"),
        ):
            for value in (None, "", [], {}, 1, True):
                with self.subTest(field=field, value=value):
                    self.data = copy.deepcopy(LAYERS)
                    self.data["20-domain.yaml"]["relations"] = [{"id": "knows", "domain": "Customer", "range": "Customer"}]
                    self.data[name][key][0][field] = value
                    self.write_layers()
                    self.assert_rejected(f".{field} must be a non-empty string")

    def test_reference_list_types(self):
        for name, key, field in (
            ("20-domain.yaml", "classes", "synonyms"),
            ("20-domain.yaml", "classes", "implements"),
            ("20-domain.yaml", "interfaces", "applies_to"),
            ("30-task.yaml", "tasks", "inputs"),
            ("30-task.yaml", "tasks", "outputs"),
            ("30-task.yaml", "tasks", "actor_roles"),
            ("30-task.yaml", "tasks", "decomposes_to"),
            ("40-application.yaml", "concepts", "used_by_tasks"),
        ):
            for value in (None, "Customer", {}, [None], [[]], [{}], [False], [" "]):
                with self.subTest(field=field, value=value):
                    self.data = copy.deepcopy(LAYERS)
                    self.data["20-domain.yaml"]["interfaces"] = [{"id": "Auditable"}]
                    self.data[name][key][0][field] = value
                    self.write_layers()
                    self.assert_rejected(f".{field} must be a list of non-empty strings")

    def test_optional_collections_must_be_lists_when_present(self):
        for key in ("relations", "interfaces"):
            for value in (None, "bad", {}):
                with self.subTest(key=key, value=value):
                    self.data = copy.deepcopy(LAYERS)
                    self.data["20-domain.yaml"][key] = value
                    self.write_layers()
                    self.assert_rejected(f"'{key}' must be a list")

    def test_application_fields_shape(self):
        for value in (None, "email", {}, [42], [None]):
            with self.subTest(value=value):
                self.data["40-application.yaml"]["concepts"][0]["fields"] = value
                self.write_layers()
                self.assert_rejected(".fields must be a list of names or mappings")

    def test_named_and_mapping_application_fields_work(self):
        self.data["40-application.yaml"]["concepts"][0]["fields"] = ["email", {"name": "name"}]
        self.write_layers()
        result = self.cli("validate", self.workspace)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_duplicate_ids_still_fail(self):
        for name, key in (("10-upper.yaml", "anchors"), ("20-domain.yaml", "classes"), ("30-task.yaml", "tasks"), ("40-application.yaml", "concepts")):
            with self.subTest(name=name):
                self.data = copy.deepcopy(LAYERS)
                self.data[name][key].append(copy.deepcopy(self.data[name][key][0]))
                self.write_layers()
                self.assert_rejected("duplicate")

    def test_unknown_anchor_still_fails(self):
        self.data["20-domain.yaml"]["classes"][0]["upper"] = "Missing"
        self.write_layers()
        self.assert_rejected("unknown upper 'Missing'")

    def test_missing_anchor_still_fails(self):
        del self.data["20-domain.yaml"]["classes"][0]["upper"]
        self.write_layers()
        self.assert_rejected("has no 'upper' anchor")

    def test_unknown_domain_reference_still_fails(self):
        self.data["30-task.yaml"]["tasks"][0]["inputs"] = ["Missing"]
        self.write_layers()
        self.assert_rejected("inputs ref 'Missing' not a domain class")

    def test_unknown_task_reference_still_fails(self):
        self.data["40-application.yaml"]["concepts"][0]["used_by_tasks"] = ["Missing"]
        self.write_layers()
        self.assert_rejected("used_by_tasks 'Missing' not a known task")

    def test_unknown_application_binding_still_fails(self):
        self.data["40-application.yaml"]["concepts"][0]["binds"] = "Missing"
        self.write_layers()
        self.assert_rejected("binds unknown domain class 'Missing'")

    def test_missing_sources_remain_warnings(self):
        del self.data["20-domain.yaml"]["classes"][0]["source"]
        self.write_layers()
        result = self.cli("validate", self.workspace)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("has no source evidence", result.stdout)

    def test_independent_layer_errors_are_aggregated(self):
        (self.workspace / "10-upper.yaml").unlink()
        (self.workspace / "20-domain.yaml").write_text("[]", encoding="utf-8")
        result = self.assert_rejected("2 errors")
        self.assertIn("10-upper.yaml", result.stdout)
        self.assertIn("20-domain.yaml", result.stdout)
        self.assertIn("layer checks not run", result.stdout)

    def test_init_validate_and_mapping_smoke_test(self):
        target = self.root / "fresh"
        result = self.cli("init", "--name", "fresh", "--out", target)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = self.cli("validate", target)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = self.cli("mappings", target)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(yaml.safe_load((target / "50-mappings.yaml").read_text()), {"mappings": []})

    def test_mapping_generation_still_works(self):
        result = self.cli("mappings", self.workspace)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        mappings = yaml.safe_load((self.workspace / "50-mappings.yaml").read_text())["mappings"]
        self.assertEqual(mappings[0]["domain"], "Customer")
        self.assertEqual(mappings[0]["upper"], "Agent")

    def test_mapping_missing_input_does_not_overwrite_existing_output(self):
        output = self.workspace / "50-mappings.yaml"
        output.write_text("preserve me", encoding="utf-8")
        (self.workspace / "20-domain.yaml").unlink()
        result = self.cli("mappings", self.workspace)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(output.read_text(), "preserve me")

    def test_init_does_not_overwrite_existing_files(self):
        before = (self.workspace / "20-domain.yaml").read_bytes()
        result = self.cli("init", "--name", "existing", "--out", self.workspace)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.workspace / "20-domain.yaml").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
