"""Opt-in, bounded execution bridge to the existing Rust graph-view binary.

The installed native binary is a *local developer artifact*, not an isolated
sandbox, credential boundary, hosted worker or authenticated telemetry source.
There is deliberately no fallback to Python when the native command fails:
an error must be visible rather than silently changing graph semantics.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
from threading import BoundedSemaphore

MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_BINARY_BYTES = 64 * 1024 * 1024
MAX_CONCURRENT = 2
TIMEOUT_SECONDS = 5.0
COMMANDS = frozenset({"summary", "page", "neighborhood", "node", "incident", "suggest-links",
                      "context", "explain", "search", "change-impact", "decide"})
CONTEXT_COMMANDS = frozenset({"context", "explain", "search", "change-impact"})  # read a bundle: {"state", "business", ...}
KINDS = {
    "summary": "LOCAL_CONTROL_ROOM_SUMMARY",
    "page": "BOUNDED_GRAPH_PAGE",
    "neighborhood": "BOUNDED_GRAPH_NEIGHBORHOOD",
    "node": "BOUNDED_NODE_DETAIL",
    "incident": "UNATTESTED_INCIDENT_INVESTIGATION",
    "suggest-links": "LINK_SUGGESTIONS",
    "context": "ENTITY_CONTEXT",
    "explain": "RELATIONSHIP_EXPLANATION",
    "search": "ENTITY_SEARCH",
    "change-impact": "CHANGE_IMPACT",
    "decide": "DECISION_COMMIT",
}


class NativeViewError(RuntimeError):
    """Read-only native projection is unavailable or violates its contract."""


def no_duplicates(pairs):
    value = {}
    for key, entry in pairs:
        if key in value:
            raise NativeViewError("native JSON response contains duplicate fields")
        value[key] = entry
    return value


ENGINE_DIR_ENV = "ONTOLOGYEX_ENGINE_DIR"
MAX_DISCOVERY_OUTPUT = 16 * 1024 * 1024
DISCOVERY_TIMEOUT_SECONDS = 60.0
# Native subprocesses never inherit the server's tokens, credentials, or session environment.
CLEAN_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}


def engine_binary(name: str) -> Path:
    """Where the Rust binary `name` lives: $ONTOLOGYEX_ENGINE_DIR, the installed
    skill's bin/ (see fetch_engine.py), then this checkout's release or debug build."""
    configured = os.environ.get(ENGINE_DIR_ENV)
    if configured:
        return Path(configured) / name
    here = Path(__file__).resolve()
    candidates = [here.parents[1] / "bin" / name]
    if len(here.parents) > 2:
        target = here.parents[2] / "engine" / "target"
        candidates += [target / "release" / name, target / "debug" / name]
    return next((c for c in candidates if c.exists()), candidates[-1])


def checked_binary(binary: Path) -> Path:
    """A regular, executable, bounded file that is not a symlink."""
    if binary.is_symlink():
        raise NativeViewError("native binary symlink not allowed")
    try:
        st = os.stat(binary, follow_symlinks=False)
    except OSError as exc:
        raise NativeViewError(
            f"Rust binary missing: build the engine or run fetch_engine.py ({binary.name})"
        ) from exc
    if not (stat.S_ISREG(st.st_mode) and 0 < st.st_size <= MAX_BINARY_BYTES
            and os.access(binary, os.X_OK)):
        raise NativeViewError("invalid or non-executable Rust binary")
    return binary


def discovery_engine(choice: str) -> str:
    """"python" or "rust". `auto` picks Rust when ontologyex-discover is installed
    (it is byte-identical and faster); a broken install still fails loudly later."""
    if choice not in ("auto", "python", "rust"):
        raise NativeViewError("unsupported discovery engine")
    if choice != "auto":
        return choice
    try:
        checked_binary(engine_binary("ontologyex-discover"))
        return "rust"
    except NativeViewError:
        return "python"


def native_discover(repo: Path, binary: Path | None = None) -> dict:
    """`discover_repo.discover(repo)` computed by `ontologyex-discover` (byte-identical output)."""
    import discover_repo

    binary = checked_binary(Path(binary) if binary is not None else engine_binary("ontologyex-discover"))
    try:
        process = subprocess.run([str(binary), "--repo", str(repo)], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, timeout=DISCOVERY_TIMEOUT_SECONDS, check=False, env=CLEAN_ENV)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NativeViewError("native discovery unavailable or timed out") from exc
    if len(process.stdout) > MAX_DISCOVERY_OUTPUT:
        raise NativeViewError("native discovery output byte budget exceeded")
    try:
        result = json.loads(process.stdout.decode("utf-8"), object_pairs_hook=no_duplicates)
    except (UnicodeError, ValueError, TypeError) as exc:
        raise NativeViewError("native discovery returned invalid JSON") from exc
    if process.returncode == 1 and type(result) is dict and result.get("status") == "ERROR":
        raise discover_repo.DiscoveryError(str(result.get("error", "discovery failed")))
    if (process.returncode != 0 or type(result) is not dict
            or result.get("kind") != "UNREVIEWED_BUSINESS_DISCOVERY_PREVIEW"
            or result.get("execution_authorized") is not False):
        raise NativeViewError("native discovery violates trust contract")
    return result


class NativeView:
    def __init__(self, binary: Path | None = None):
        self.binary = checked_binary(Path(binary) if binary is not None else engine_binary("ontologyex-view"))
        self._slots = BoundedSemaphore(MAX_CONCURRENT)

    def execute(self, full: dict, command: str, *args: str,
                observation_rows: list[dict] | None = None) -> dict:
        if command not in COMMANDS or any(type(arg) is not str for arg in args):
            raise NativeViewError("invalid native view invocation")
        expected_args = {"summary": 0, "page": 3, "neighborhood": 2,
                         "node": 1, "incident": 1, "suggest-links": 0,
                         "context": 1, "explain": 2, "search": 1, "change-impact": 0, "decide": 0}
        if len(args) != expected_args[command] or any(
            len(arg) > 1024 or (not arg and (command != "page" or i != 1))
            for i, arg in enumerate(args)
        ):
            raise NativeViewError("invalid native view arguments")
        subject = full.get("state") if command in CONTEXT_COMMANDS else full
        if type(subject) is not dict or subject.get("execution_authorized") is not False:
            raise NativeViewError("native input cannot authorize execution")
        if command == "incident":
            if type(observation_rows) is not list or len(observation_rows) > 4096:
                raise NativeViewError("invalid or oversized incident rows")
            input_record = {"full": full, "rows": observation_rows}
        else:
            if observation_rows is not None:
                raise NativeViewError("unexpected observations for graph view")
            input_record = full
        try:
            payload = json.dumps(
                input_record, ensure_ascii=False, allow_nan=False, sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError, RecursionError) as exc:
            raise NativeViewError("unable to serialize source-backed graph") from exc
        if len(payload) > MAX_INPUT_BYTES:
            raise NativeViewError("native graph input byte budget exceeded")
        if not self._slots.acquire(blocking=False):
            raise NativeViewError("native graph query concurrency limit reached")
        try:
            # The server's GitHub token, credentials and session environment are
            # NEVER inherited by a native view subprocess. No shell is used.
            process = subprocess.run(
                [str(self.binary), command, *args],
                input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=TIMEOUT_SECONDS, check=False, env=CLEAN_ENV,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise NativeViewError("native view process unavailable or timed out") from exc
        finally:
            self._slots.release()
        if process.returncode != 0:
            # Never reflect raw command stderr into the browser: it could
            # contain source names, paths or information from a broken binary.
            raise NativeViewError("native view rejected source/query")
        if len(process.stdout) > MAX_OUTPUT_BYTES:
            raise NativeViewError("native view output byte budget exceeded")
        try:
            result = json.loads(
                process.stdout.decode("utf-8"), object_pairs_hook=no_duplicates,
                parse_constant=lambda _: (_ for _ in ()).throw(
                    NativeViewError("nonfinite native JSON value")),
            )
        except (UnicodeError, ValueError, TypeError) as exc:
            raise NativeViewError("native view returned invalid JSON") from exc
        if (type(result) is not dict or result.get("kind") != KINDS[command]
                or result.get("execution_authorized") is not False):
            raise NativeViewError("native response violates trust contract")
        if command in {"page", "neighborhood"}:
            nodes, edges = result.get("nodes"), result.get("edges")
            if (type(nodes) is not list or type(edges) is not list
                    or len(nodes) > 90 or len(edges) > 300
                    or type(result.get("summary")) is not dict
                    or result["summary"].get("execution_authorized") is not False):
                raise NativeViewError("native graph projection budget violated")
        elif command == "node":
            edges = result.get("edges")
            if type(edges) is not list or len(edges) > 40:
                raise NativeViewError("native detail budget violated")
        elif command == "incident":
            timeline = result.get("timeline")
            exposed = result.get("possible_dependents")
            direct = result.get("direct_relationships")
            history = result.get("source_history")
            if (type(timeline) is not list or len(timeline) > 32
                    or type(exposed) is not list or len(exposed) > 40
                    or type(direct) is not list or len(direct) > 40
                    or type(history) is not list or len(history) > 8
                    or result.get("root_cause") != "UNDETERMINED"
                    or result.get("observation_semantics") !=
                      "SOURCE_REPORTED_NOT_AUTHENTICATED_OR_VERIFIED"):
                raise NativeViewError("native incident safety contract violated")
        elif command == "suggest-links":
            rows = result.get("suggestions")
            if (type(rows) is not list or len(rows) > 5 * len(full.get("bindings", []))
                    or result.get("auto_applied") != 0
                    or any(type(r) is not dict or r.get("status") != "PROPOSED" for r in rows)):
                raise NativeViewError("native suggestions violate the confirmation contract")
        elif command in CONTEXT_COMMANDS:  # bounded answers about entities, never the whole graph
            if command == "context":
                ok = (type(result.get("relationships")) is list and len(result["relationships"]) <= 200
                      and type(result.get("relied_on_by")) is list and len(result["relied_on_by"]) <= 100)
            elif command == "explain":
                ok = type(result.get("paths")) is list and len(result["paths"]) <= 10
            elif command == "change-impact":
                ok = (type(result.get("touched")) is list and len(result["touched"]) <= 500
                      and type(result.get("impacted")) is list and len(result["impacted"]) <= 100)
            else:
                ok = type(result.get("results")) is list and len(result["results"]) <= 25
            if not ok or "nodes" in result or "edges" in result:
                raise NativeViewError("native entity answer budget violated")
        elif "nodes" in result or "edges" in result or "source_evidence" in result:
            raise NativeViewError("native summary contains full graph")
        return result
