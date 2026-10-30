"""Live pulls: the read-only API pulls on a schedule, so the operations page stays current.

Declared once in the operations folder, `pulls.json`:

    {"pulls": [
      {"provider": "stripe", "key_env": "STRIPE_API_KEY", "every_minutes": 60, "days": 180},
      {"provider": "aws", "every_minutes": 360, "months": 6, "profile": "billing-read"},
      {"provider": "zendesk", "subdomain": "acme", "every_minutes": 60, "days": 120},
      {"provider": "hubspot", "key_env": "HUBSPOT_TOKEN", "every_minutes": 120}
    ]}

Keys come only from environment variables (named in the file, or the provider's default);
they are never written anywhere, and an error that echoes one has it masked. Each pull
writes the same aggregates-only file a hand-run `operations.py pull` writes, and
`pulls.status.json` beside it says, for each pull, when it last ran, whether it worked,
what it read and when it runs next. Nothing runs unless asked: the control room runs due
pulls only when started with --pulls (operate.py up --pulls), and `operations.py pull all`
runs every declared pull once (for cron).

A pull that fails, or has not succeeded for two of its intervals, is a signal on the
operations page and in the command center: the figures there are getting old.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import tempfile
from threading import Event, Thread
from typing import Callable

CONFIG = "pulls.json"
STATUS = "pulls.status.json"
PROVIDERS = {
    "stripe": {"key_env": "STRIPE_API_KEY", "days": 180},
    "aws": {"months": 6},
    "zendesk": {"days": 120},
    "hubspot": {"key_env": "HUBSPOT_TOKEN"},
}
ALLOWED = {"provider", "key_env", "every_minutes", "days", "months", "subdomain", "profile", "api_base"}
MIN_MINUTES, MAX_MINUTES = 15, 7 * 24 * 60
TICK_SECONDS = 60
ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
LOCAL_BASE = re.compile(r"^http://(127\.0\.0\.1|localhost)(:\d{1,5})?$")


class PullConfigError(ValueError):
    pass


def load(folder: Path) -> list[dict]:
    """The declared pulls, checked: known providers, sane intervals, keys only by environment variable name."""
    path = folder / CONFIG
    if not path.is_file():
        return []
    try:
        document = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise PullConfigError(f"{CONFIG}: {exc}") from exc
    if type(document) is not dict or set(document) != {"pulls"} or type(document["pulls"]) is not list:
        raise PullConfigError(f'{CONFIG}: expected {{"pulls": [...]}}')
    pulls, seen = [], set()
    for i, entry in enumerate(document["pulls"][:20]):
        where = f"{CONFIG} pull {i + 1}"
        if type(entry) is not dict or entry.get("provider") not in PROVIDERS:
            raise PullConfigError(f"{where}: provider is one of {', '.join(PROVIDERS)}")
        unknown = set(entry) - ALLOWED
        if unknown:
            raise PullConfigError(f"{where}: unknown field(s) {', '.join(sorted(unknown))}")
        out = {**PROVIDERS[entry["provider"]], **entry}
        minutes = out.get("every_minutes", 60)
        if type(minutes) is not int or not MIN_MINUTES <= minutes <= MAX_MINUTES:
            raise PullConfigError(f"{where}: every_minutes is {MIN_MINUTES} to {MAX_MINUTES}")
        out["every_minutes"] = minutes
        if "key_env" in out and (type(out["key_env"]) is not str or not ENV_NAME.match(out["key_env"])):
            raise PullConfigError(f"{where}: key_env names an environment variable (A-Z, 0-9, _); never a key")
        for field, top in (("days", 3650), ("months", 36)):
            if field in out and (type(out[field]) is not int or not 1 <= out[field] <= top):
                raise PullConfigError(f"{where}: {field} is 1 to {top}")
        if out["provider"] == "zendesk" and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", str(out.get("subdomain", ""))):
            raise PullConfigError(f"{where}: zendesk needs subdomain, the part before .zendesk.com")
        if "profile" in out and not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", str(out["profile"])):
            raise PullConfigError(f"{where}: profile is an aws profile name")
        if "api_base" in out and not LOCAL_BASE.match(str(out["api_base"])):
            raise PullConfigError(f"{where}: api_base may only point at this machine (for tests); keys never go "
                                  "anywhere else")
        name = out["provider"] + (f":{out['subdomain']}" if out.get("subdomain") else "")
        if name in seen:
            raise PullConfigError(f"{where}: {name} is declared twice")
        seen.add(name)
        out["name"] = name
        pulls.append(out)
    return pulls


def read_status(folder: Path) -> dict:
    try:
        document = json.loads((folder / STATUS).read_text("utf-8"))
        return document.get("pulls", {}) if type(document) is dict else {}
    except (OSError, ValueError):
        return {}


def write_status(folder: Path, status: dict) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=folder, prefix=".pulls-status-", delete=False, encoding="utf-8") as h:
        json.dump({"schema_version": 1, "kind": "ONTOLOGYEX_PULLS_STATUS", "pulls": status,
                   "semantics": "WHEN_EACH_PULL_LAST_RAN_NOT_WHAT_IT_READ"}, h, indent=2, sort_keys=True)
    Path(h.name).replace(folder / STATUS)


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(text: str | None) -> datetime | None:
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) if text else None
    except ValueError:
        return None


def scrub(text: str, secrets: list[str]) -> str:
    """An error message with any key value masked, and kept short."""
    for secret in secrets:
        if secret and len(secret) >= 6:
            text = text.replace(secret, "…")
    return re.sub(r"(?i)(bearer|basic|token|key)[=: ]+[A-Za-z0-9._~+/-]{12,}", r"\1 …", text)[:300]


def run_one(entry: dict, folder: Path, now: datetime, entities: list[dict] | None = None,
            environ: dict | None = None) -> dict:
    """One pull, through the same functions as `operations.py pull`. Returns what it read."""
    import operations
    environ = os.environ if environ is None else environ
    provider = entry["provider"]
    if provider == "stripe":
        key = environ.get(entry["key_env"], "")
        if not key:
            raise operations.OperationsError(f"{entry['key_env']} is not set in the control room's environment")
        return operations.pull_stripe(key, folder, entry["days"], entry.get("api_base", "https://api.stripe.com"),
                                      now=now)
    if provider == "aws":
        return operations.pull_aws(folder, entry["months"], now=now, profile=entry.get("profile"))
    if provider == "zendesk":
        oauth = environ.get("ZENDESK_OAUTH_TOKEN", "")
        email, token = environ.get("ZENDESK_EMAIL", ""), environ.get("ZENDESK_API_TOKEN", "")
        if oauth:
            auth = {"Authorization": f"Bearer {oauth}"}
        elif email and token:
            import base64
            auth = {"Authorization": "Basic " + base64.b64encode(f"{email}/token:{token}".encode()).decode()}
        else:
            raise operations.OperationsError("set ZENDESK_OAUTH_TOKEN, or ZENDESK_EMAIL and ZENDESK_API_TOKEN, in the "
                                             "control room's environment")
        base = entry.get("api_base") or f"https://{entry['subdomain']}.zendesk.com"
        return operations.pull_zendesk(base, auth, folder, entry["days"], now=now, entities=entities)
    key = environ.get(entry["key_env"], "")
    if not key:
        raise operations.OperationsError(f"{entry['key_env']} is not set in the control room's environment")
    return operations.pull_hubspot(key, folder, now=now, base=entry.get("api_base", "https://api.hubapi.com"))


def due(entry: dict, status: dict, now: datetime) -> bool:
    last = parse((status.get(entry["name"]) or {}).get("last_attempt"))
    return last is None or now - last >= timedelta(minutes=entry["every_minutes"])


def run_due(folder: Path, now: datetime | None = None, entities: Callable[[], list[dict]] | None = None,
            environ: dict | None = None, force: bool = False,
            runner: Callable[..., dict] = run_one) -> list[dict]:
    """Every declared pull that is due (or all, with force): run it, and record what happened."""
    now = now or datetime.now(timezone.utc)
    pulls = load(folder)
    status = read_status(folder)
    environ = os.environ if environ is None else environ
    done = []
    for entry in pulls:
        if not force and not due(entry, status, now):
            continue
        row = dict(status.get(entry["name"]) or {})
        row.update({"provider": entry["provider"], "every_minutes": entry["every_minutes"], "last_attempt": stamp(now),
                    "next_due": stamp(now + timedelta(minutes=entry["every_minutes"]))})
        secrets = [environ.get(entry.get("key_env", "")) or "", environ.get("ZENDESK_OAUTH_TOKEN", ""),
                   environ.get("ZENDESK_API_TOKEN", "")]
        try:
            result = runner(entry, folder, now, entities() if entities else None, environ)
            row.update({"ok": True, "last_success": stamp(now), "detail": None,
                        "objects_read": result.get("objects_read") or {"months": len(result.get("months") or [])},
                        "file": Path(result["file"]).name})
        except Exception as exc:  # noqa: BLE001 - every failure is recorded, none stops the others
            row.update({"ok": False, "detail": scrub(f"{type(exc).__name__}: {exc}", secrets)})
        status[entry["name"]] = row
        write_status(folder, status)
        done.append({"pull": entry["name"], **{k: row[k] for k in ("ok", "detail")}})
    return done


def health(folder: Path | None, now: datetime) -> dict | None:
    """What the page and the command center say about the pulls: each one's state and freshness, and signals."""
    if folder is None or not (folder / CONFIG).is_file():
        return None
    try:
        pulls = load(folder)
    except PullConfigError as exc:
        return {"pulls": [], "signals": [{"signal": "PULLS_MISCONFIGURED", "level": "HIGH", "stream": "pulls",
                                          "detail": str(exc)}], "error": str(exc)}
    status, rows, signals = read_status(folder), [], []
    for entry in pulls:
        row = status.get(entry["name"]) or {}
        success = parse(row.get("last_success"))
        stale = success is None or now - success > timedelta(minutes=2 * entry["every_minutes"])
        state = ("NOT_RUN_YET" if not row else "FAILING" if row.get("ok") is False else "STALE" if stale else "FRESH")
        rows.append({"pull": entry["name"], "provider": entry["provider"], "state": state,
                     "every_minutes": entry["every_minutes"], "last_success": row.get("last_success"),
                     "last_attempt": row.get("last_attempt"), "next_due": row.get("next_due"),
                     "objects_read": row.get("objects_read"), "detail": row.get("detail")})
        if state == "FAILING":
            signals.append({"signal": "PULL_FAILING", "level": "HIGH", "stream": "pulls", "topic": entry["name"],
                            "detail": f"the {entry['name']} pull failed at {row.get('last_attempt')}: {row.get('detail')}"})
        elif state == "STALE":
            signals.append({"signal": "PULL_STALE", "level": "MEDIUM", "stream": "pulls", "topic": entry["name"],
                            "detail": f"the {entry['name']} pull last succeeded at {row.get('last_success')}, more than "
                                      f"two intervals ago: its figures are getting old"})
    return {"pulls": rows, "signals": signals}


class Puller:
    """The control room's background thread: every minute, run the pulls that are due."""

    def __init__(self, folder: Path, entities: Callable[[], list[dict]] | None = None):
        self.folder, self.entities = folder, entities
        self.stop_event = Event()
        self.thread: Thread | None = None
        self.last_error: str | None = None

    def tick(self) -> list[dict]:
        try:
            self.last_error = None
            return run_due(self.folder, entities=self.entities)
        except Exception as exc:  # noqa: BLE001 - a bad pulls.json (shown by health()) or an unwritable folder;
            self.last_error = str(exc)  # the thread keeps going, and checks again next minute
            return []

    def start(self) -> None:
        def loop():
            while not self.stop_event.is_set():
                self.tick()
                self.stop_event.wait(TICK_SECONDS)
        self.thread = Thread(target=loop, name="ontologyex-pulls", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None and self.thread.is_alive():
            self.thread.join(timeout=5)
