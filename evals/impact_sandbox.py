#!/usr/bin/env python3
"""Build a small, local Docker image for the change-impact evaluation.

No packages are downloaded: installed command binaries, their shared libraries,
 and the system Python standard library are copied into a scratch filesystem.
The resulting image is intended for impact_ab.py, which runs it offline, with a
read-only root and a read-only public repository. Linux and Docker are required.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile

COMMANDS = ("bash", "cat", "ls", "find", "sed", "awk", "grep", "sort", "uniq", "head", "tail", "wc",
            "cut", "tr", "xargs", "git", "env", "pwd", "basename", "dirname", "realpath", "sleep",
            "timeout", "diff", "nl")


def copy_executable(source: Path, target: Path):
    if not source.exists():
        raise ValueError(f"Missing required command: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source.resolve(), target)


def build(tag: str) -> dict:
    if subprocess.run(["docker", "image", "inspect", tag], capture_output=True).returncode == 0:
        raise ValueError("Image tag already exists; choose an unused tag")
    system_python = Path("/usr/bin/python3")
    info = subprocess.check_output([str(system_python), "-c",
                                    "import json,sys,sysconfig; print(json.dumps({'exe':sys.executable,"
                                    "'stdlib':sysconfig.get_path('stdlib'),'version':sys.version.split()[0]}))"],
                                   text=True)
    py = json.loads(info)
    rg = shutil.which("rg")
    if rg is None:
        raise ValueError("ripgrep (rg) is required")
    with tempfile.TemporaryDirectory(prefix="ontologyex-impact-image-") as tmp:
        base = Path(tmp)
        root = base / "rootfs"
        (root / "usr/bin").mkdir(parents=True)
        (root / "bin").mkdir()
        (root / "tmp").mkdir()
        for name in COMMANDS:
            copy_executable(Path("/usr/bin") / name, root / "usr/bin" / name)
        copy_executable(Path(rg), root / "usr/bin/rg")
        copy_executable(Path(py["exe"]), root / "usr/bin/python3")
        (root / "bin/sh").symlink_to("../usr/bin/bash")
        (root / "bin/bash").symlink_to("../usr/bin/bash")
        stdlib = Path(py["stdlib"])
        target_stdlib = root / str(stdlib).lstrip("/")
        shutil.copytree(stdlib, target_stdlib, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        files = list((root / "usr/bin").iterdir()) + list((target_stdlib / "lib-dynload").glob("*.so"))
        libraries = set()
        for file in files:
            result = subprocess.run(["ldd", str(file)], capture_output=True, text=True)
            for line in result.stdout.splitlines():
                libraries.update(re.findall(r"(/(?:lib|usr/lib)[^\s()]+)", line))
        for name in sorted(libraries):
            source = Path(name)
            if source.exists():
                copy_executable(source, root / name.lstrip("/"))
        for folder, _, _ in os.walk(root):
            os.chmod(folder, 0o755)
        def normalize(info):
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            return info
        archive = base / "rootfs.tar"
        with tarfile.open(archive, "w") as tar:
            tar.add(root, arcname=".", filter=normalize)
        run = subprocess.run(["docker", "import", str(archive), tag], capture_output=True, text=True)
        if run.returncode:
            raise ValueError("Docker image import failed: " + run.stderr[-500:])
        return {"tag": tag, "image_id": run.stdout.strip(), "python": py["version"],
                "ripgrep": subprocess.check_output([rg, "--version"], text=True).splitlines()[0],
                "libraries": len(libraries)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="unused local Docker image tag")
    args = parser.parse_args()
    try:
        print(json.dumps(build(args.tag), indent=2))
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        parser.exit(2, f"impact_sandbox: {exc}\n")


if __name__ == "__main__":
    main()
