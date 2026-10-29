"""Golden fixtures: the answers OntologyEX gives on fixed inputs, and the record of why each last changed."""
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import golden


class GoldenTests(unittest.TestCase):
    def test_every_output_matches_its_expected_file(self):
        problems = golden.check(None, False)  # Rust too, when the engine is built here
        self.assertEqual(problems, [], "\n".join(problems))

    def test_manifest_names_every_file_with_a_reason(self):
        manifest = json.loads(golden.MANIFEST.read_text("utf-8"))["outputs"]
        files = {p.relative_to(golden.EXPECTED).as_posix(): p for p in golden.EXPECTED.rglob("*.json")}
        self.assertEqual(set(manifest), set(files))
        for rel, entry in manifest.items():
            self.assertEqual(entry["sha256"], hashlib.sha256(files[rel].read_bytes()).hexdigest(), rel)
            self.assertGreaterEqual(len(entry["reason"]), 10, rel)

    def test_a_hand_edited_expected_file_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            expected, manifest = Path(tmp) / "expected", Path(tmp) / "MANIFEST.json"
            shutil.copytree(golden.EXPECTED, expected)
            shutil.copy(golden.MANIFEST, manifest)
            target = expected / "shop/command_center.json"
            data = json.loads(target.read_text())
            data["items"][0]["score"] = 99
            target.write_bytes(golden.encode(data))
            saved = golden.EXPECTED, golden.MANIFEST
            golden.EXPECTED, golden.MANIFEST = expected, manifest
            self.addCleanup(lambda: setattr(golden, "EXPECTED", saved[0]) or setattr(golden, "MANIFEST", saved[1]))
            problems = golden.check(None, False)
        self.assertEqual(len(problems), 2, problems)
        self.assertIn("does not match MANIFEST.json", problems[0])
        self.assertIn("~ $.items[0].score: 99 -> 50", problems[1])  # the path, the old value and the new

    def test_normalizing_keeps_behaviour(self):
        value = {"at": "2026-10-29T12:00:01.5Z", "path": "/tmp/x/shop/a.py", "score": 18, "ids": ["b", "a"],
                 "text": "seen 2026-10-01T00:00:00+00:00 in /tmp/x"}
        self.assertEqual(golden.normalize(value, "/tmp/x"),
                         {"at": "<time>", "path": "<root>/shop/a.py", "score": 18, "ids": ["b", "a"],
                          "text": "seen <time> in <root>"})

    def test_differences_name_paths(self):
        self.assertEqual(golden.differences({"a": [1, 2], "b": 1}, {"a": [1, 3, 4], "c": 2}),
                         ["~ $.a: 2 items -> 3", "~ $.a[1]: 2 -> 3", "- $.b (gone)", "+ $.c = 2"])

    def test_update_needs_a_reason(self):
        run = subprocess.run([sys.executable, str(ROOT / "tests/golden.py"), "update", "--reason", "x"],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 2)
        self.assertIn("--reason must say why", run.stderr)


if __name__ == "__main__":
    unittest.main()
