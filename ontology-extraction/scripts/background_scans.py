"""Background scans: the slow reads of a repository, off the request path, never applied stale.

The control room reads a repository in slow ways: the code surface (routes and tables),
the supplier map, a year of git history for owners, and every file for compliance
evidence. Done while answering a page, they made the page wait. Here a background
thread runs them, and pages read the last result that was committed.

A scan that finishes is not automatically safe to keep. Each one notes the repository's
revision before it starts (a fingerprint of every file's path, size and modification
time, and of git's HEAD and index) and again when it finishes; if the two differ, the
repository changed under it, so the result is discarded and the scan runs again. A
committed result always describes one revision of the repository, never a mix.

Before a scan has committed once, a request that needs it waits for that first result,
so nothing is ever shown as empty only because it is still being read. After that,
requests never wait. `status()` says what each scan last did, for /api/scans.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
from threading import Event, Lock, Thread
import time
from typing import Callable

SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", "target", ".next", ".cache",
        ".ontologyex", ".pytest_cache", ".mypy_cache", "vendor"}
MAX_FILES = 100_000
TICK_SECONDS = 5
FIRST_RESULT_SECONDS = 120
RETRY_AFTER_CHANGE = 3  # discarded runs in a row before waiting a full interval (a repository being rewritten)


def revision(repo: Path) -> str:
    """A cheap fingerprint of the repository as it is now: files' paths, sizes, modification times, git HEAD and index."""
    digest, count = hashlib.sha256(), 0
    base = len(str(repo).rstrip(os.sep)) + 1
    for root, dirs, files in os.walk(repo):
        dirs[:] = sorted(d for d in dirs if d not in SKIP)
        for name in sorted(files):
            path = os.path.join(root, name)
            try:
                stat = os.lstat(path)
            except OSError:
                continue
            digest.update(f"{path[base:]}\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode("utf-8", "replace"))
            count += 1
            if count >= MAX_FILES:
                return digest.hexdigest()
    for marker in (".git/HEAD", ".git/index", ".git/packed-refs"):
        try:
            stat = os.stat(repo / marker)
            digest.update(f"{marker}\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode())
        except OSError:
            continue
    return digest.hexdigest()


class Job:
    def __init__(self, name: str, interval: float, run: Callable[[], object]):
        self.name, self.interval, self.run = name, interval, run
        self.value: object = None
        self.error: str | None = None
        self.revision: str | None = None
        self.committed_at: datetime | None = None
        self.last_try = 0.0
        self.runs = self.discarded = self.discarded_in_a_row = 0
        self.duration_ms: int | None = None
        self.running = False
        self.ready = Event()


class Scanner:
    """Runs each job on its interval (sooner when the repository changed), commits only unchanged-revision results."""

    def __init__(self, repo: Path, jobs: list[tuple[str, float, Callable[[], object]]],
                 fingerprint: Callable[[Path], str] = revision, clock: Callable[[], float] = time.monotonic):
        self.repo, self.fingerprint, self.clock = repo, fingerprint, clock
        self.jobs = {name: Job(name, interval, run) for name, interval, run in jobs}
        self.lock = Lock()
        self.stop_event = Event()
        self.thread: Thread | None = None
        self.seen_revision: str | None = None

    def run_job(self, job: Job) -> str:
        """Prepare (note the revision), run, commit only if the revision is unchanged. Returns what happened."""
        before = self.fingerprint(self.repo)
        started = self.clock()
        job.running, job.last_try = True, started
        try:
            value, error = job.run(), None
        except (ValueError, OSError, RuntimeError) as exc:
            value, error = None, f"{type(exc).__name__}: {str(exc)[:200]}"
        finally:
            job.running = False
        after = self.fingerprint(self.repo)
        with self.lock:
            job.runs += 1
            job.duration_ms = int((self.clock() - started) * 1000)
            self.seen_revision = after
            if before != after:
                job.discarded += 1
                job.discarded_in_a_row += 1
                return "DISCARDED_REPOSITORY_CHANGED"
            job.value, job.error, job.revision = value, error, before
            job.committed_at = datetime.now(timezone.utc)
            job.discarded_in_a_row = 0
        job.ready.set()
        return "COMMITTED" if error is None else "COMMITTED_ERROR"

    def due(self, job: Job, current: str | None) -> bool:
        if job.revision is None:  # never committed: keep trying, but back off if the repository never holds still
            return job.discarded_in_a_row < RETRY_AFTER_CHANGE or self.clock() - job.last_try >= job.interval
        if self.clock() - job.last_try >= job.interval:
            return True
        changed = current is not None and current != job.revision
        return changed and job.discarded_in_a_row < RETRY_AFTER_CHANGE

    def run_pending(self) -> list[tuple[str, str]]:
        """One pass over the jobs in order; what ran and what happened."""
        current = self.fingerprint(self.repo)
        with self.lock:
            self.seen_revision = current
        done = []
        for job in self.jobs.values():
            if self.stop_event.is_set():
                break
            if self.due(job, current):
                done.append((job.name, self.run_job(job)))
        return done

    def start(self) -> None:
        def loop():
            while not self.stop_event.is_set():
                self.run_pending()
                self.stop_event.wait(TICK_SECONDS)
        self.thread = Thread(target=loop, name="ontologyex-scans", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None and self.thread.is_alive():
            self.thread.join(timeout=5)

    def result(self, name: str, wait: bool = True) -> tuple[object, str | None]:
        """The last committed value and error; the first time, waits for the first result."""
        job = self.jobs[name]
        if wait and not job.ready.is_set():
            job.ready.wait(FIRST_RESULT_SECONDS)
        with self.lock:
            return job.value, job.error if job.revision is not None else "NOT_SCANNED_YET"

    def status(self) -> dict:
        with self.lock:
            rows = [{"scan": j.name, "interval_seconds": j.interval, "running": j.running, "runs": j.runs,
                     "discarded_repository_changed": j.discarded,
                     "committed_at": j.committed_at.isoformat().replace("+00:00", "Z") if j.committed_at else None,
                     "revision": j.revision[:16] if j.revision else None, "duration_ms": j.duration_ms,
                     "error": j.error,
                     "describes_current_repository": (j.revision == self.seen_revision) if j.revision else False}
                    for j in self.jobs.values()]
        return {"schema_version": 1, "kind": "BACKGROUND_SCANS", "scans": rows,
                "semantics": "A_RESULT_IS_KEPT_ONLY_IF_THE_REPOSITORY_DID_NOT_CHANGE_WHILE_IT_WAS_READ",
                "execution_authorized": False}
