#!/usr/bin/env python3
"""Opt-in local journal of normalized feed observations and replay-case export.

The journal starts when the Python feed starts; it cannot backfill observations
or establish that a source was continuously reporting. It stores no raw spans,
request bodies, URLs, or credentials. See docs/observation-history.md.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import sys
from urllib.parse import quote

import control_room as room
import history_replay as replay

MAX_ROWS = 100_000
MAX_PAGES = 16_384  # typically 64 MiB at SQLite's default 4096-byte page size
SCHEMA_VERSION = 1
APPLICATION_ID = 0x4F455848  # OEXH


class RecordingError(ValueError):
    pass


def require(ok, message):
    if not ok:
        raise RecordingError(message)


def now():
    return datetime.now(timezone.utc)


def iso(moment):
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def micros(moment):
    delta = moment - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def private_directory(path: Path):
    """Create a private final directory, refusing links and shared directories."""
    if not path.exists() and not path.is_symlink():
        path.mkdir(mode=0o700)
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode), f"recording directory is not a real directory: {path}")
    if os.name == "posix":
        require(info.st_uid == os.geteuid() and info.st_mode & 0o077 == 0,
                f"recording directory must be owned by you and private (0700): {path}")


def default_path(repo: Path) -> Path:
    root = repo / ".ontologyex"
    require(not root.is_symlink(), "refusing symlinked .ontologyex directory")
    root.mkdir(exist_ok=True)
    private = root / "observation-history"
    private_directory(private)
    return private / "history.sqlite3"


def check_file(path: Path):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode), "history database must be a regular file, not a symlink")
    if os.name == "posix":
        require(info.st_uid == os.geteuid() and info.st_mode & 0o077 == 0,
                "history database must be owned by you and private (0600)")


def connect_readonly(path: Path):
    check_file(path)
    uri = "file:" + quote(str(path.absolute()), safe="/") + "?mode=ro"
    db = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        require(db.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID and
                db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION,
                "unsupported history database schema")
        require(db.execute("PRAGMA page_count").fetchone()[0] <= MAX_PAGES,
                "history database exceeds its page budget")
    except Exception:
        db.close()
        raise
    return db


def normalized(row):
    fields = {"schema_version", "component_id", "observed_at", "status", "source",
              "expires_after_seconds", "metrics"}
    require(type(row) is dict and set(row) == fields and type(row["schema_version"]) is int
            and row["schema_version"] == 1, "invalid observation shape")
    require(type(row["component_id"]) is str and room.IDENT_RE.fullmatch(row["component_id"]) is not None,
            "invalid component")
    require(type(row["status"]) is str and row["status"] in room.STATUS and
            type(row["source"]) is str and row["source"] in room.SOURCES, "invalid status or source")
    require(type(row["expires_after_seconds"]) is int and
            1 <= row["expires_after_seconds"] <= room.MAX_AGE, "invalid observation freshness")
    metrics = row["metrics"]
    require(type(metrics) is dict and set(metrics) <= room.METRICS, "invalid metrics")
    for metric, value in metrics.items():
        require(type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1e9 and
                (metric not in room.PERCENT_METRICS or value <= 100), "invalid metric value")
    return {**row, "observed_at": iso(room.timestamp(row["observed_at"]))}


def canonical(row):
    return json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False)


class Recorder:
    """One feed session. SQLite transactions preserve committed batches on restart."""

    def __init__(self, path: Path, max_rows=MAX_ROWS):
        self.path = path
        self.max_rows = max_rows
        private_directory(path.parent)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError:
            created = False
            check_file(path)
        else:
            created = True
            os.close(fd)
        if not created:
            # Check identity before changing SQLite's journal or page settings.
            with closing(connect_readonly(path)):
                pass
        self.db = sqlite3.connect(path, timeout=5)
        try:
            self.db.execute("PRAGMA journal_mode=DELETE")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute(f"PRAGMA max_page_count={MAX_PAGES}")
            if created:
                self.db.executescript("""
                    CREATE TABLE sessions (
                      id TEXT PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT,
                      state TEXT NOT NULL, batches INTEGER NOT NULL DEFAULT 0,
                      inserted INTEGER NOT NULL DEFAULT 0, duplicates INTEGER NOT NULL DEFAULT 0,
                      omitted INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE TABLE observations (
                      id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE,
                      session_id TEXT NOT NULL, component TEXT NOT NULL, source TEXT NOT NULL,
                      observed_at TEXT NOT NULL, observed_us INTEGER NOT NULL,
                      available_at TEXT NOT NULL, available_us INTEGER NOT NULL,
                      status TEXT NOT NULL, ttl INTEGER NOT NULL, metrics TEXT NOT NULL
                    );
                    CREATE INDEX observation_window ON observations(component, observed_us);
                    CREATE INDEX observation_availability ON observations(available_us);
                    PRAGMA application_id=1329944648;
                    PRAGMA user_version=1;
                """)
            else:
                require(self.db.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID and
                        self.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION,
                        "unsupported history database schema")
            # Also upgrades an earlier v1 database that did not yet retain batch
            # clock information. Its existing observations remain unchanged.
            self.db.execute("CREATE TABLE IF NOT EXISTS journal_clock "
                            "(id INTEGER PRIMARY KEY CHECK(id=1), available_us INTEGER)")
            self.session = secrets.token_hex(16)
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO journal_clock(id,available_us) "
                                "SELECT 1,MAX(available_us) FROM observations")
                self.db.execute("INSERT INTO sessions(id,started_at,state) VALUES(?,?,?)",
                                (self.session, iso(now()), "running"))
        except Exception:
            self.db.close()
            raise

    def record(self, rows, available_at, omitted=0):
        require(type(omitted) is int and omitted >= 0, "invalid omitted count")
        require(available_at.tzinfo is not None, "availability time requires a timezone")
        unique = {}
        for item in rows:
            row = normalized(item)
            observed = room.timestamp(row["observed_at"])
            require(observed <= available_at, "observation is later than local availability; check the system clock")
            data = canonical(row)
            fingerprint = hashlib.sha256(data.encode()).hexdigest()
            unique[fingerprint] = (row, observed)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            count = self.db.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
            latest = self.db.execute("SELECT available_us FROM journal_clock WHERE id=1").fetchone()[0]
            require(latest is None or micros(available_at) >= latest,
                    "local clock moved backwards since the last recorded reading; recording stopped")
            added = 0
            for fingerprint, (row, observed) in unique.items():
                if self.db.execute("SELECT 1 FROM observations WHERE fingerprint=?", (fingerprint,)).fetchone():
                    continue
                require(count + added < self.max_rows, "observation history is full; recording stopped")
                self.db.execute("""INSERT INTO observations
                    (fingerprint,session_id,component,source,observed_at,observed_us,available_at,
                     available_us,status,ttl,metrics) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (fingerprint, self.session, row["component_id"], row["source"], row["observed_at"],
                     micros(observed), iso(available_at), micros(available_at), row["status"],
                     row["expires_after_seconds"], canonical(row["metrics"])))
                added += 1
            self.db.execute("""UPDATE sessions SET batches=batches+1,inserted=inserted+?,
                            duplicates=duplicates+?,omitted=omitted+? WHERE id=?""",
                            (added, len(rows) - added, omitted, self.session))
            self.db.execute("UPDATE journal_clock SET available_us=? WHERE id=1", (micros(available_at),))

    def close(self, clean=True):
        try:
            with self.db:
                self.db.execute("UPDATE sessions SET ended_at=?,state=? WHERE id=?",
                                (iso(now()), "closed" if clean else "failed", self.session))
        finally:
            self.db.close()


def status(path: Path):
    with closing(connect_readonly(path)) as db:
        db.execute("BEGIN")
        session_count, unfinished, failed, omitted = db.execute(
            "SELECT COUNT(*),SUM(ended_at IS NULL),SUM(state='failed'),SUM(omitted) FROM sessions").fetchone()
        sessions = [dict(zip(("started_at", "ended_at", "state", "batches", "inserted", "duplicates", "omitted"), row))
                    for row in db.execute("SELECT started_at,ended_at,state,batches,inserted,duplicates,omitted "
                                          "FROM sessions ORDER BY started_at DESC LIMIT 1000")]
        sessions.reverse()
        count, first, last = db.execute("SELECT COUNT(*),MIN(available_us),MAX(available_us) FROM observations").fetchone()
        groups = list(db.execute("SELECT component,source,COUNT(*),MIN(observed_us),MAX(observed_us) "
                                 "FROM observations GROUP BY component,source ORDER BY component,source LIMIT 257"))
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        components = [{"component": component, "source": source, "observations": n,
                       "first_observed_at": iso(epoch + timedelta(microseconds=first_us)),
                       "last_observed_at": iso(epoch + timedelta(microseconds=last_us))}
                      for component, source, n, first_us, last_us in groups[:256]]
    return {"kind": "ONTOLOGYEX_OBSERVATION_HISTORY", "observations": count,
            "first_available_at": iso(epoch + timedelta(microseconds=first)) if first is not None else None,
            "last_available_at": iso(epoch + timedelta(microseconds=last)) if last is not None else None,
            "sessions": sessions, "session_count": session_count, "sessions_truncated": session_count > 1000,
            "components": components, "components_truncated": len(groups) > 256,
            "unfinished_sessions": unfinished or 0, "failed_sessions": failed or 0,
            "omitted_observations": omitted or 0,
            "coverage": "UNKNOWN", "max_observations": MAX_ROWS,
            "semantics": "UNATTESTED_NORMALIZED_FEED_OBSERVATIONS_NOT_PROOF_OF_CONTINUOUS_COVERAGE"}


def export_case(path: Path, *, name, component, metric, start, end, follow_up_end,
                step_seconds, threshold, selected_at, training_data_available_through=None):
    first, last = replay.instant(start, "start"), replay.instant(end, "end")
    require(metric in room.METRICS, "unsupported metric")
    try:
        lookback = first - timedelta(seconds=room.MAX_AGE)
    except OverflowError as exc:
        raise RecordingError("start is outside the supported lookback range") from exc
    with closing(connect_readonly(path)) as db:
        rows = list(db.execute("""SELECT id,source,observed_at,available_at,status,ttl,metrics
            FROM observations WHERE component=? AND observed_us>=? AND observed_us<=?
            AND (observed_us>=? OR observed_us + ttl*1000000>=?)
            ORDER BY observed_us,id""", (component, micros(lookback), micros(last),
                                          micros(first), micros(first))))
    # Preserve the live engine's ambiguity handling: a single-metric export
    # cannot safely represent differences in another metric, so refuse it.
    variants = {}
    observations = []
    for ident, source, observed, available, state, ttl, metric_json in rows:
        key = (source, observed)
        variant = (state, ttl, metric_json)
        if key in variants and variants[key] != variant:
            raise RecordingError(f"conflicting readings at {observed} from {source}; review them before export")
        variants[key] = variant
        metrics = json.loads(metric_json)
        if metric in metrics:
            observations.append({"ref": f"obs:{ident}", "source": source, "observed_at": observed,
                                 "available_at": available, "status": state, "ttl_seconds": ttl,
                                 "value": metrics[metric]})
    require(len(observations) <= replay.MAX_OBSERVATIONS, "too many readings; choose a narrower window")
    dataset = {"schema_version": 1, "kind": replay.KIND, "name": name, "metric": metric,
               "threshold": {"above": threshold, "selected_at": selected_at,
                             "training_data_available_through": training_data_available_through},
               "cases": [{"id": name, "component": component,
                          "window": {"start": start, "end": end, "follow_up_end": follow_up_end,
                                     "step_seconds": step_seconds},
                          "outcome": {"state": "unreviewed", "incident_at": None, "evidence_ref": "review:outcome"},
                          "baseline": {"coverage": "unknown", "alerts": []},
                          "capture_coverage": {"state": "unknown", "evidence_ref": None},
                          "observations": observations}]}
    return replay.validate(dataset)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    s = commands.add_parser("status", help="inspect local recording sessions and gaps")
    s.add_argument("--db", type=Path, required=True)
    e = commands.add_parser("export-case", help="make an unreviewed replay dataset; never overwrites")
    e.add_argument("--db", type=Path, required=True)
    e.add_argument("--name", required=True)
    e.add_argument("--component", required=True)
    e.add_argument("--metric", required=True, choices=sorted(room.METRICS))
    e.add_argument("--start", required=True)
    e.add_argument("--end", required=True)
    e.add_argument("--follow-up-end", required=True)
    e.add_argument("--step-seconds", required=True, type=int)
    e.add_argument("--threshold", required=True, type=float)
    e.add_argument("--selected-at", required=True)
    e.add_argument("--training-data-available-through")
    e.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            print(json.dumps(status(args.db), indent=2))
        else:
            result = export_case(args.db, name=args.name, component=args.component, metric=args.metric,
                                 start=args.start, end=args.end, follow_up_end=args.follow_up_end,
                                 step_seconds=args.step_seconds, threshold=args.threshold, selected_at=args.selected_at,
                                 training_data_available_through=args.training_data_available_through)
            with args.out.open("x", encoding="utf-8") as handle:
                json.dump(result, handle, indent=2)
                handle.write("\n")
            print(f"Unreviewed replay case written to {args.out}")
        return 0
    except (RecordingError, replay.HistoryError, room.RoomError, sqlite3.Error, OSError, ValueError) as exc:
        print(f"Observation history refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
