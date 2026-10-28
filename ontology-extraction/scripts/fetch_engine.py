#!/usr/bin/env python3
"""Download the prebuilt Rust engine for this machine from a GitHub release.

    python fetch_engine.py                       # latest engine-v* release
    python fetch_engine.py --version engine-v0.2.0 --dest DIR

It fetches `ontologyex-engine-<target>.tar.gz` and `SHA256SUMS` from the
release over HTTPS (github.com hosts only), checks the archive's SHA-256,
and installs only the four expected binaries into DEST (default: this
skill's `bin/`, where the control room looks for them).

What the checksum proves: the archive is the one listed in the release.
It does not prove who built it. Every release also carries a GitHub build
provenance attestation; verify it with the printed `gh attestation verify`
command before relying on a binary. Nothing here runs the binaries.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_REPO = "New1Direction/OntologyEX"
BINARIES = ("ontologyex-core", "ontologyex-discover", "ontologyex-feeds", "ontologyex-view")
ALLOWED_HOSTS = {"github.com", "api.github.com", "objects.githubusercontent.com",
                 "release-assets.githubusercontent.com"}
MAX_ARCHIVE = 64 * 1024 * 1024
MAX_MEMBER = 64 * 1024 * 1024
MAX_SUMS = 64 * 1024
TAG = re.compile(r"engine-v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.]+)?")
REPO = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")


class FetchError(RuntimeError):
    pass


def check(ok: bool, message: str) -> None:
    if not ok:
        raise FetchError(message)


def target(system: str | None = None, machine: str | None = None) -> str:
    """The release target for this machine (static musl builds on Linux)."""
    system = (system or sys.platform).lower()
    machine = (machine or platform.machine()).lower()
    arch = {"x86_64": "x86_64", "amd64": "x86_64", "arm64": "aarch64", "aarch64": "aarch64"}.get(machine)
    check(arch is not None, f"no prebuilt engine for CPU {machine!r}; build it with cargo")
    if system.startswith("linux"):
        return f"{arch}-unknown-linux-musl"
    if system == "darwin":
        return f"{arch}-apple-darwin"
    raise FetchError(f"no prebuilt engine for {system!r}; build it with cargo (Windows: use WSL)")


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _check_url(url: str) -> None:
    parts = urllib.parse.urlsplit(url)
    check(parts.scheme == "https" and parts.hostname in ALLOWED_HOSTS, f"refusing to fetch {url!r}")


def get(url: str, limit: int, accept: str = "application/octet-stream") -> bytes:
    _check_url(url)
    opener = urllib.request.build_opener(_SafeRedirect)
    request = urllib.request.Request(url, headers={"User-Agent": "ontologyex-fetch-engine/1", "Accept": accept})
    try:
        with opener.open(request, timeout=60) as response:
            data = response.read(limit + 1)
    except urllib.error.HTTPError as exc:
        raise FetchError(f"{url} answered HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FetchError(f"cannot reach {urllib.parse.urlsplit(url).hostname}: {exc}") from exc
    check(len(data) <= limit, f"{url} is larger than expected")
    return data


def latest_tag(repo: str) -> str:
    data = json.loads(get(f"https://api.github.com/repos/{repo}/releases?per_page=30", 4 * 1024 * 1024,
                          "application/vnd.github+json"))
    for release in data if type(data) is list else []:
        tag = release.get("tag_name", "") if type(release) is dict else ""
        if TAG.fullmatch(tag) and not release.get("draft") and not release.get("prerelease"):
            return tag
    raise FetchError(f"no engine-v* release found in {repo}")


def expected_digest(sums: bytes, name: str) -> str:
    for line in sums.decode("utf-8").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == name and re.fullmatch(r"[0-9a-f]{64}", parts[0]):
            return parts[0]
    raise FetchError(f"SHA256SUMS does not list {name}")


def extract(archive: bytes, folder: str) -> dict[str, bytes]:
    """Only `folder/<binary>` and `folder/LICENSE`, regular files, bounded size."""
    wanted = {f"{folder}/{b}" for b in BINARIES} | {f"{folder}/LICENSE"}
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for member in tar:
            if member.isdir() and member.name.rstrip("/") == folder:
                continue
            check(member.name in wanted and member.isreg() and member.size <= MAX_MEMBER,
                  f"unexpected archive member {member.name!r}")
            handle = tar.extractfile(member)
            check(handle is not None, f"unreadable archive member {member.name!r}")
            files[member.name.split("/", 1)[1]] = handle.read(MAX_MEMBER + 1)
    check(set(BINARIES) <= set(files), "archive is missing an engine binary")
    return files


def install(files: dict[str, bytes], dest: Path, force: bool) -> list[str]:
    dest.mkdir(parents=True, exist_ok=True)
    check(not dest.is_symlink(), "destination must not be a symlink")
    written = []
    for name, data in sorted(files.items()):
        path = dest / name
        check(force or not path.exists(), f"{path} exists; pass --force to replace it")
        check(not path.is_symlink(), f"{path} is a symlink")
        with tempfile.NamedTemporaryFile(dir=dest, prefix=f".{name}-", delete=False) as handle:
            handle.write(data)
        os.chmod(handle.name, 0o755 if name in BINARIES else 0o644)
        Path(handle.name).replace(path)
        written.append(str(path))
    return written


def fetch(version: str, dest: Path, repo: str = DEFAULT_REPO, force: bool = False) -> dict:
    check(REPO.fullmatch(repo) is not None, "invalid --repo")
    tag = latest_tag(repo) if version == "latest" else version
    check(TAG.fullmatch(tag) is not None, "versions look like engine-v1.2.3")
    triple = target()
    name = f"ontologyex-engine-{triple}.tar.gz"
    base = f"https://github.com/{repo}/releases/download/{tag}"
    sums = get(f"{base}/SHA256SUMS", MAX_SUMS)
    archive = get(f"{base}/{name}", MAX_ARCHIVE)
    digest = hashlib.sha256(archive).hexdigest()
    check(digest == expected_digest(sums, name), f"checksum mismatch for {name}: refusing to install")
    written = install(extract(archive, name.removesuffix(".tar.gz")), dest, force)
    return {"status": "INSTALLED", "version": tag, "target": triple, "sha256": digest, "files": written,
            "verify_provenance": f"gh attestation verify {dest / 'ontologyex-feeds'} --repo {repo}",
            "next": f"control_room.py --view-engine rust ... finds {dest}; or set ONTOLOGYEX_ENGINE_DIR"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", default="latest", help="release tag, e.g. engine-v0.2.0 (default: latest)")
    parser.add_argument("--dest", type=Path, default=Path(__file__).resolve().parents[1] / "bin")
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--force", action="store_true", help="replace binaries that are already installed")
    args = parser.parse_args()
    try:
        print(json.dumps(fetch(args.version, args.dest, args.repo, args.force), indent=2))
        return 0
    except (FetchError, OSError, ValueError, tarfile.TarError) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
