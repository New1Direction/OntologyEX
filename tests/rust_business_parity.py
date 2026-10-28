"""Rust link-suggestion matcher and business kernels against the Python reference.

Runs after `cargo build --bin ontologyex-view`. Deterministic fuzzing (fixed seed)
plus the operating-picture demo and edge cases. No network or production data.
"""
from __future__ import annotations

import importlib.util
import json
import random
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ontology-extraction/scripts"))
import control_room
import discover_repo
import link_suggest
import native_view_bridge

BINARY = ROOT / "engine/target/debug/ontologyex-view"
spec = importlib.util.spec_from_file_location("ontologyex_parity_demo", ROOT / "examples/operating-picture/demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)

WORDS = ["payments", "Payment", "ledger", "refund", "applyRefund", "api", "db", "web", "billing",
         "charge_card", "src", "services", "Zahlungsdienst", "é", "a", "x1", "OrderService",
         "orders", "order", "inventory", "py", "main", "index", "v2", "HTTPServer", "über-api"]
KINDS = list(link_suggest.KINDS)


def native(data: dict, *args: str) -> dict:
    out = subprocess.run([str(BINARY), *args], input=json.dumps(data).encode(),
                         capture_output=True, timeout=20)
    if out.returncode != 0:
        raise AssertionError(f"Rust rejected input: {out.stderr[:400]!r}")
    return json.loads(out.stdout)


def same(label: str, data: dict) -> None:
    expected, actual = link_suggest.suggest_from(data), native(data, "suggest-links")
    if expected != actual:
        raise AssertionError(f"{label}: Python {json.dumps(expected)[:900]}\nRust {json.dumps(actual)[:900]}")


def word(rng: random.Random) -> str:
    return rng.choice(WORDS)


def path(rng: random.Random) -> str:
    parts = [word(rng) for _ in range(rng.randint(0, 3))]
    name = word(rng) + rng.choice(["", ".py", ".ts", ".tar.gz", "."])
    return "/".join([*parts, name]) if rng.random() > 0.1 else ""


def fuzz_input(rng: random.Random) -> dict:
    bindings = [{"skill": rng.choice(["s1", "s2"]),
                 "binding": ".".join(word(rng) for _ in range(rng.randint(1, 3))),
                 "binds": word(rng), "system": rng.choice(["", "acme-" + word(rng), word(rng)]),
                 "path": path(rng)} for _ in range(rng.randint(1, 6))]
    components = [{"id": f"c{i}", "label": " ".join(word(rng) for _ in range(rng.randint(1, 2))),
                   "kind": rng.choice(KINDS), "path": path(rng)} for i in range(rng.randint(0, 12))]
    pairs = [[b["skill"], b["binding"], c["id"]] for b in bindings for c in components]
    rng.shuffle(pairs)
    cut = rng.randint(0, len(pairs) // 3) if pairs else 0
    return {"schema_version": 1, "kind": "LINK_MATCH_INPUT", "bindings": bindings,
            "components": components, "linked": pairs[:cut // 2], "rejected": pairs[cut // 2:cut],
            "execution_authorized": False}


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="ontologyex-business-parity-") as tmp:
        built = demo.build(Path(tmp) / "demo", datetime(2026, 10, 28, tzinfo=timezone.utc), unlinked=True)
        state = control_room.build_state(discover_repo.discover(Path(built["repo"])), [], {})
        data = link_suggest.prepare([Path(built["skill"])], state, [], [])
        same("demo", data)
        bridge = native_view_bridge.NativeView(BINARY)
        assert bridge.execute(data, "suggest-links") == link_suggest.suggest_from(data)
    edge = {"schema_version": 1, "kind": "LINK_MATCH_INPUT", "linked": [], "rejected": [],
            "execution_authorized": False,
            "bindings": [{"skill": "s", "binding": "m.f", "binds": "", "system": "", "path": "a/charge.py"}],
            "components": [{"id": "m", "label": "charge", "kind": "source_module", "path": "src/a/charge.py"},
                           {"id": "q\"\\", "label": "Ünicode \"quoted\"", "kind": "api_route", "path": "charge.py"}]}
    same("edge", edge)
    rng = random.Random(20261028)
    for i in range(300):
        same(f"fuzz {i}", fuzz_input(rng))
    print("Rust link matcher equals Python: demo, edge cases, 300 seeded random inputs")
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(native({}, "conformance"), handle)
    result = subprocess.run([sys.executable, str(ROOT / "tests/conformance.py"), "check", handle.name],
                            capture_output=True, text=True)
    print(result.stdout.strip())
    if result.returncode != 0:
        raise SystemExit("Rust conformance table differs from Python")


if __name__ == "__main__":
    main()
