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
import secrets
import signal
import subprocess
import sys
from threading import Event, Thread
import time

import feeds
import native_view_bridge
import observation_history

HERE = Path(__file__).resolve().parent
STARTUP_TIMEOUT_SECONDS = 30


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
    if getattr(args, "thresholds", None) is not None:
        command += ["--thresholds", str(args.thresholds)]
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
            process.wait()


def forward_control_room(process: subprocess.Popen, marker: str, ready: Event) -> None:
    """Forward the child's output and notice its private, per-launch readiness signal."""
    try:
        for line in process.stdout:
            if line.rstrip("\r\n") == marker:
                ready.set()
            else:
                print(line, end="", flush=True)
    finally:
        process.stdout.close()


def up(args) -> int:
    processes: list[subprocess.Popen] = []
    observations = None
    engine = None
    recording = None
    record_observations = getattr(args, "record_observations", False)
    if record_observations and args.feeds is None:
        raise ValueError("--record-observations requires --feeds")
    if record_observations and args.engine == "rust":
        raise ValueError("Observation recording currently requires the Python feeds engine; choose --engine python or auto")
    previous = signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    reader = None
    try:
        if args.feeds is not None:
            config = feeds.load_config(args.feeds)  # refuse a bad config before starting anything
            observations = feeds.output_path(config)
            # Recording is implemented at the Python feed publication point.
            command, engine = feeds_command(args.feeds, "python" if record_observations else args.engine)
            if record_observations:
                recording = observation_history.default_path(args.repo)
                command += ["--record-history", str(recording)]
            # The control room refuses a missing file; with traces only, none is written before traffic.
            observations.parent.mkdir(parents=True, exist_ok=True)
            observations.touch(exist_ok=True)
            processes.append(subprocess.Popen(command))
        # A live port alone cannot identify our child: another app might already own it.
        # The control room sends this unique marker only after it has bound and initialized.
        marker = "ONTOLOGYEX_READY_" + secrets.token_hex(16)
        env = {**os.environ, "ONTOLOGYEX_LAUNCH_READY": marker}
        room = subprocess.Popen(control_room_command(args, observations), stdout=subprocess.PIPE,
                                text=True, errors="replace", bufsize=1, env=env)
        processes.append(room)
        ready = Event()
        reader = Thread(target=forward_control_room, args=(room, marker, ready), daemon=True)
        reader.start()
        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        while not ready.wait(0.05):
            for process in processes:
                code = process.poll()
                if code is not None:
                    if process is room:
                        reader.join(timeout=1)  # show its explanation before the stop notice
                    name = "feeds" if process is not room else "control room"
                    print(json.dumps({"status": "STOPPED", "process": name, "exit_code": code}), flush=True)
                    return code or 1
            if time.monotonic() >= deadline:
                print(json.dumps({"status": "ERROR", "error": "Control room did not start within "
                                  f"{STARTUP_TIMEOUT_SECONDS} seconds."}), flush=True)
                return 1
        # Startup can fail just as the readiness signal arrives.
        for process in processes:
            code = process.poll()
            if code is not None:
                name = "feeds" if process is not room else "control room"
                print(json.dumps({"status": "STOPPED", "process": name, "exit_code": code}), flush=True)
                return code or 1
        history = history_path(args)
        print(json.dumps({"status": "UP", "twin": f"http://127.0.0.1:{args.port}/twin",
                          "business": f"http://127.0.0.1:{args.port}/business",
                          "command": f"http://127.0.0.1:{args.port}/command",
                          "digest": f"http://127.0.0.1:{args.port}/digest",
                          "history": str(history) if history else None,
                          "observation_history": str(recording) if recording else None,
                          "system_map": f"http://127.0.0.1:{args.port}/",
                          "feeds_engine": engine, "observations": str(observations) if observations else None,
                          "stop": "Ctrl-C"}, indent=2), flush=True)
        while True:
            for process in processes:
                code = process.poll()
                if code is not None:
                    name = "feeds" if process is not room else "control room"
                    print(json.dumps({"status": "STOPPED", "process": name, "exit_code": code}), flush=True)
                    return code or 1
            time.sleep(0.5)
    except KeyboardInterrupt:
        return 0
    finally:
        signal.signal(signal.SIGTERM, previous)
        stop(processes)
        if reader is not None:
            reader.join(timeout=2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    u = commands.add_parser("up", help="start the feeds and the business screen")
    u.add_argument("--repo", type=Path, required=True)
    u.add_argument("--feeds", type=Path, help="feeds config from feed_setup.py init")
    u.add_argument("--skill", type=Path, action="append", default=[])
    u.add_argument("--links", type=Path)
    u.add_argument("--events", type=Path)
    u.add_argument("--thresholds", type=Path,
                   help="user-configured metric thresholds for two-sample signals")
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
    u.add_argument("--record-observations", action="store_true",
                   help="opt in to private local feed observation history (uses the Python feeds engine)")
    u.add_argument("--engine", choices=("auto", "python", "rust"), default="auto",
                   help="feeds engine: auto uses ontologyex-feeds when it is installed")
    args = parser.parse_args()
    try:
        if not args.repo.is_dir() or args.repo.is_symlink():
            raise ValueError(f"Repository folder not found or is a symlink: {args.repo}. "
                             "Pass an existing folder with --repo.")
        if not 0 < args.port <= 65535:
            raise ValueError("Port must be between 1 and 65535.")
        return up(args)
    except (feeds.FeedError, native_view_bridge.NativeViewError, OSError, ValueError) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}))
        return 1


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    raise SystemExit(main())
