"""Builds the replay corpus in this folder from the synthetic demo: one moment, seven situations.

    python tests/replays/build.py          # rewrites the cases (they are committed; run when the demo changes)

Every case is what the control room could know at 2026-10-29T12:00:00Z, with what really happened written in
`expect`. Synthetic data only: no real incident is in this folder.
"""
import importlib.util
import json
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ontology-extraction/scripts"))
import replay  # noqa: E402

spec = importlib.util.spec_from_file_location("ontologyex_replay_demo", ROOT / "examples/operating-picture/demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)

AT = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
SERVICES = ("web", "api", "payments", "ledger", "worker", "queue", "db")
PAYMENTS = demo.SERVICE + "payments"


def samples(statuses: dict, age: timedelta = timedelta(minutes=2)) -> str:
    when = (AT - age).strftime("%Y-%m-%dT%H:%M:%SZ")
    return "".join(json.dumps({"schema_version": 1, "component_id": demo.SERVICE + name, "observed_at": when,
                               "status": status, "source": "synthetic", "expires_after_seconds": 3600,
                               "metrics": {}}, sort_keys=True) + "\n" for name, status in statuses.items())


ALL_OK = {name: "ok" for name in SERVICES}
CASES = {
    "payments-error": ({**ALL_OK, "payments": "error"}, {}, {"verdict": "FAULT", "cause": [PAYMENTS], "within": 3},
                       "Payments reports errors; refunds and the API rely on it."),
    "upstream-error": ({**ALL_OK, "payments": "error", "api": "error"}, {},
                       {"verdict": "FAULT", "cause": [PAYMENTS], "within": 1},
                       "The API fails because payments, which it depends on, fails: payments is the cause."),
    "degraded-queue": ({**ALL_OK, "queue": "warning"}, {},
                       {"verdict": "DEGRADED", "cause": [demo.SERVICE + "queue"], "within": 3},
                       "The queue warns of lag; nothing has failed yet."),
    "all-healthy": (ALL_OK, {}, {"verdict": "HEALTHY", "cause": [], "within": 3},
                    "Every service reports healthy within its freshness window."),
    "one-silent": ({k: v for k, v in ALL_OK.items() if k != "web"}, {},
                   {"verdict": "INCONCLUSIVE", "cause": [], "within": 3},
                   "Everything that reports is healthy, but web reports nothing: that is not healthy."),
    "expired": (ALL_OK, {"age": timedelta(days=2)}, {"verdict": "INCONCLUSIVE", "cause": [], "within": 3},
                "Every sample is two days old, past its one-hour freshness window."),
    "no-evidence": ({}, {}, {"verdict": "INCONCLUSIVE", "cause": [], "within": 3},
                    "No health observation at all: nothing can be concluded."),
}


def main() -> None:
    here = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory() as tmp:
        built = demo.build(Path(tmp) / "demo", now=AT)
        repo = Path(built["repo"])
        shutil.rmtree(repo / ".ontologyex" / "operations")  # the cases are about systems; keep them small
        for name, (statuses, options, expect, note) in CASES.items():
            if (here / name).exists():
                shutil.rmtree(here / name)
            observations = Path(tmp) / f"{name}.jsonl"
            observations.write_text(samples(statuses, options.get("age", timedelta(minutes=2))))
            replay.capture(repo, name, AT, observations, [Path(built["skill"])], 72, here, "production",
                           links=Path(built["links"]), events=Path(built["events"]))
            case = json.loads((here / name / "case.json").read_text())
            case["captured"]["at"] = case["at"]  # reproducible
            case["expect"], case["notes"] = expect, note
            (here / name / "case.json").write_text(json.dumps(case, indent=2, sort_keys=True) + "\n")
            print(name, "written")


if __name__ == "__main__":
    main()
