"""fetch_engine: target mapping, checksum, and archive safety, all offline."""
import hashlib
import io
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ontology-extraction" / "scripts"))
import fetch_engine as fe  # noqa: E402

FOLDER = "ontologyex-engine-x86_64-unknown-linux-musl"


def archive(members: dict, folder: str = FOLDER, symlink: str | None = None) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(f"{folder}/{name}" if not name.startswith("/") and ".." not in name else name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        if symlink:
            info = tarfile.TarInfo(f"{folder}/{symlink}")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tar.addfile(info)
    return buffer.getvalue()


GOOD = {b: f"#!{b}".encode() for b in fe.BINARIES} | {"LICENSE": b"MIT"}


class FetchEngineTests(unittest.TestCase):
    def test_targets(self):
        self.assertEqual(fe.target("linux", "x86_64"), "x86_64-unknown-linux-musl")
        self.assertEqual(fe.target("linux", "aarch64"), "aarch64-unknown-linux-musl")
        self.assertEqual(fe.target("darwin", "arm64"), "aarch64-apple-darwin")
        self.assertEqual(fe.target("darwin", "x86_64"), "x86_64-apple-darwin")
        for system, machine in [("win32", "amd64"), ("linux", "riscv64")]:
            with self.assertRaises(fe.FetchError):
                fe.target(system, machine)

    def test_only_github_https_is_fetched(self):
        for url in ["http://github.com/x", "https://example.com/x", "https://github.com.evil.io/x", "file:///etc/passwd"]:
            with self.subTest(url=url), self.assertRaises(fe.FetchError):
                fe.get(url, 10)

    def test_checksum_lines(self):
        digest = "a" * 64
        sums = f"{digest}  other.tar.gz\n{digest}  {FOLDER}.tar.gz\n".encode()
        self.assertEqual(fe.expected_digest(sums, f"{FOLDER}.tar.gz"), digest)
        with self.assertRaises(fe.FetchError):
            fe.expected_digest(b"zz  x\n", f"{FOLDER}.tar.gz")

    def test_extract_accepts_exactly_the_engine(self):
        files = fe.extract(archive(GOOD), FOLDER)
        self.assertEqual(set(files), set(fe.BINARIES) | {"LICENSE"})

    def test_extract_refuses_anything_else(self):
        for bad in [archive({**GOOD, "extra.sh": b"x"}), archive({**GOOD, "../escape": b"x"}),
                    archive(GOOD, symlink="ontologyex-view2"), archive(GOOD, folder="other"),
                    archive({"LICENSE": b"MIT"})]:
            with self.assertRaises(fe.FetchError):
                fe.extract(bad, FOLDER)

    def test_fetch_verifies_the_checksum_before_installing(self):
        data = archive(GOOD)
        name = f"{FOLDER}.tar.gz"
        good_sums = f"{hashlib.sha256(data).hexdigest()}  {name}\n".encode()
        bad_sums = f"{'0' * 64}  {name}\n".encode()
        with tempfile.TemporaryDirectory() as tmp, patch.object(fe, "target", return_value=FOLDER.removeprefix("ontologyex-engine-")):
            dest = Path(tmp) / "bin"
            with patch.object(fe, "get", side_effect=lambda url, limit, accept=None: bad_sums if url.endswith("SUMS") else data):
                with self.assertRaises(fe.FetchError):
                    fe.fetch("engine-v1.0.0", dest)
            self.assertFalse(dest.exists())
            with patch.object(fe, "get", side_effect=lambda url, limit, accept=None: good_sums if url.endswith("SUMS") else data):
                result = fe.fetch("engine-v1.0.0", dest)
                self.assertEqual(result["status"], "INSTALLED")
                self.assertTrue(os.access(dest / "ontologyex-feeds", os.X_OK))
                with self.assertRaises(fe.FetchError):  # no silent overwrite
                    fe.fetch("engine-v1.0.0", dest)
                fe.fetch("engine-v1.0.0", dest, force=True)

    def test_version_and_repo_are_validated(self):
        for version, repo in [("v1", fe.DEFAULT_REPO), ("engine-v1.0.0", "x/../y z")]:
            with self.assertRaises(fe.FetchError):
                fe.fetch(version, Path("/nonexistent"), repo)


if __name__ == "__main__":
    unittest.main()
