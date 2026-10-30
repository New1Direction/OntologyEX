#!/usr/bin/env python3
"""One command for the business screen with live health.

    python operate.py up --repo REPO --feeds feeds.json [--skill SKILL ...] [--links links.json]

Starts the feeds service (the Rust `ontologyex-feeds` when it is installed,
otherwise `feeds.py`) and the control room reading the observations it writes,
then waits. Ctrl-C stops both; if either stops on its own, the other is stopped
too and its exit code is returned. Without --feeds only the control room runs.

Make feeds.json with `feed_setup.py propose` and `feed_setup.py init`. Calls the feeds
see in traces are suggested as dependencies on the business screen; confirmed ones go
to --dependencies (default: dependencies.json beside feeds.json), and the screen
records decisions only with --allow-link-decisions. The command-center ranking is
recorded to REPO/.ontologyex/history.jsonl when it changes (and once a day), for the
weekly digest at /digest; --no-history turns that off, --history FILE moves it.
Nothing here changes a system: feeds only read health, and the control room
listens on 127.0.0.1.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import feeds
import native_view_bridge

HERE = Path(__file__).resolve().parent


def feeds_command(config: Path, engine: str) -> tuple[list[str], str]:
    """The feeds process to start, and which engine it is."""
    if engine in ("auto", "rust"):
        binary = native_view_bridge.engine_binary("ontologyex-feeds")
        try:
            native_view_bridge.checked_binary(binary)
            return [str(binary), "run", "--config", str(config)], "rust"
        except native_view_bridge.NativeViewError:
            if engine == "rust":
                raise
    return [sys.executable, str(HERE / "feeds.py"), "run", "--config", str(config)], "python"


def control_room_command(args, observations: Path | None) -> list[str]:
    command = [sys.executable, str(HERE / "control_room.py"), "--repo", str(args.repo), "--port", str(args.port)]
    if observations is not None:
        command += ["--observations", str(observations)]
    dependencies = args.dependencies or (args.feeds.parent / "dependencies.json" if args.feeds else None)
    if dependencies is not None:
        command += ["--dependencies", str(dependencies)]
    for skill in args.skill:
        command += ["--skill", str(skill)]
    if args.links is not None:
        command += ["--links", str(args.links)]
    if args.events is not None:
        command += ["--events", str(args.events)]
    if args.allow_link_decisions:
        command.append("--allow-link-decisions")
    if args.pulls:
        command.append("--pulls")
    if getattr(args, "environment", None):
        command += ["--environment", args.environment]
    history = history_path(args)
    if history is not None:
        command += ["--history", str(history)]
    return command


def history_path(args) -> Path | None:
    if args.no_history:
        return None
    return args.history or args.repo / ".ontologyex" / "history.jsonl"


def stop(processes: list[subprocess.Popen]) -> None:
    for process in processes:
        if process.poll() is None:
            process.terminate()
    deadline = time.monotonic() + 10
    for process in processes:
        try:
            process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            process.kill()


def up(args) -> int:
    processes: list[subprocess.Popen] = []
    observations = None
    engine = None
    if args.feeds is not None:
        config = feeds.load_config(args.feeds)  # refuse a bad config before starting anything
        observations = feeds.output_path(config)
        # The control room refuses a missing file; with traces only, none is written before traffic.
        observations.parent.mkdir(parents=True, exist_ok=True)
        observations.touch(exist_ok=True)
        command, engine = feeds_command(args.feeds, args.engine)
        processes.append(subprocess.Popen(command))
    processes.append(subprocess.Popen(control_room_command(args, observations)))
    history = history_path(args)
    print(json.dumps({"status": "UP", "command": f"http://127.0.0.1:{args.port}/command",
                      "digest": f"http://127.0.0.1:{args.port}/digest",
                      "history": str(history) if history else None,
                      "business": f"http://127.0.0.1:{args.port}/business",
                      "system_map": f"http://127.0.0.1:{args.port}/",
                      "feeds_engine": engine, "observations": str(observations) if observations else None,
                      "stop": "Ctrl-C"}, indent=2), flush=True)
    previous = signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        while True:
            for process in processes:
                code = process.poll()
                if code is not None:
                    name = "feeds" if engine and process is processes[0] else "control room"
                    print(json.dumps({"status": "STOPPED", "process": name, "exit_code": code}), flush=True)
                    return code or 1
            time.sleep(0.5)
    except KeyboardInterrupt:
        return 0
    finally:
        signal.signal(signal.SIGTERM, previous)
        stop(processes)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    u = commands.add_parser("up", help="start the feeds and the business screen")
    u.add_argument("--repo", type=Path, required=True)
    u.add_argument("--feeds", type=Path, help="feeds config from feed_setup.py init")
    u.add_argument("--skill", type=Path, action="append", default=[])
    u.add_argument("--links", type=Path)
    u.add_argument("--events", type=Path)
    u.add_argument("--dependencies", type=Path,
                   help="dependencies confirmed from observed calls (default: beside --feeds)")
    u.add_argument("--port", type=int, default=8765)
    u.add_argument("--allow-link-decisions", action="store_true")
    u.add_argument("--environment", help="the environment the health observations come from (production, staging...)")
    u.add_argument("--pulls", action="store_true",
                   help="run the read-only API pulls declared in REPO/.ontologyex/operations/pulls.json on their "
                        "intervals (they use the network; keys from environment variables only)")
    u.add_argument("--history", type=Path, help="where to record the ranking (default: REPO/.ontologyex/history.jsonl)")
    u.add_argument("--no-history", action="store_true", help="do not record the ranking for the weekly digest")
    u.add_argument("--engine", choices=("auto", "python", "rust"), default="auto",
                   help="feeds engine: auto uses ontologyex-feeds when it is installed")
    args = parser.parse_args()
    try:
        if not (0 < args.port <= 65535 and args.repo.is_dir()):
            raise ValueError("expected a repository directory and a port 1..65535")
        return up(args)
    except (feeds.FeedError, native_view_bridge.NativeViewError, OSError, ValueError) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}))
        return 1


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    raise SystemExit(main())
