"""Every control-room page's script parses: one syntax error blanks the whole page."""
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "ontology-extraction/scripts"
INLINE = re.compile(r"<script>(.*?)</script>", re.S)


class PageScriptTests(unittest.TestCase):
    def test_every_inline_script_parses(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed; CI runs this check")
        pages = sorted(p for p in SCRIPTS.glob("*.html") if INLINE.search(p.read_text("utf-8")))
        self.assertGreaterEqual(len(pages), 9)
        with tempfile.TemporaryDirectory() as tmp:
            for page in pages:
                for n, body in enumerate(INLINE.findall(page.read_text("utf-8"))):
                    path = Path(tmp) / f"{page.stem}-{n}.js"
                    path.write_text(body)
                    run = subprocess.run([node, "--check", str(path)], capture_output=True, text=True, timeout=30)
                    self.assertEqual(run.returncode, 0, f"{page.name} script {n}: {run.stderr[-600:]}")


if __name__ == "__main__":
    unittest.main()
