#!/usr/bin/env python3
"""Send traces from your services without changing their code: one command each.

    python otel_setup.py propose --repo REPO
    python otel_setup.py run --service NAME [--feeds feeds.json] -- npm start
    python otel_setup.py env --service NAME --lang node [--feeds feeds.json]

`propose` lists the apps in a repository (Node, Python, Java, Go, .NET, Ruby, PHP),
each with the file that declares it, the name its traces will carry, the map
component that name stands for, and the command that starts it with tracing.
Node and Python need no code change; the others are listed with what they need.
It writes nothing.

`run` starts one service the way you always do, with OpenTelemetry's zero-code
instrumentation loaded and traces sent to the OntologyEX feeds (`--feeds`, or
http://127.0.0.1:4318):

- Node: @opentelemetry/auto-instrumentations-node, installed once with `npm ci`
  from the lockfile shipped beside this script (every package pinned with its
  integrity hash), loaded with NODE_OPTIONS --require.
- Python: opentelemetry-distro and the instrumentations for the libraries the
  app's own interpreter has installed, installed once with pip into a cache
  directory and loaded by a sitecustomize that puts them after the app's packages.

Nothing in the repository or its environments is changed. Packages go to
~/.cache/ontologyex/otel (or $ONTOLOGYEX_OTEL_CACHE), downloaded from npm or PyPI on
the first run of each kind. `env` prints the same settings for a Dockerfile,
Procfile, or shell. Traces are what the services report; nothing here proves health.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib

import yaml

import discover_repo
import feeds

HERE = Path(__file__).resolve().parent
NODE_PACKAGE = HERE / "otel" / "node"            # package.json + package-lock.json
NODE_EXIT = HERE / "otel" / "node-exit.cjs"
PYTHON_PACKAGES = ("opentelemetry-distro==0.66b1", "opentelemetry-exporter-otlp-proto-http==1.45.1")
SKIP_DIRS = {".git", ".hg", "node_modules", ".venv", "venv", "env", "__pycache__", "dist", "build", "target",
             "vendor", ".next", ".nuxt", ".tox", ".mypy_cache", "site-packages", "bower_components", ".cache"}
MAX_FILES = 50_000
MAX_MANIFEST = 1024 * 1024
LANGUAGES = {"package.json": "node", "pyproject.toml": "python", "requirements.txt": "python", "setup.py": "python",
             "Pipfile": "python", "pom.xml": "java", "build.gradle": "java", "build.gradle.kts": "java",
             "go.mod": "go", "Cargo.toml": "rust", "Gemfile": "ruby", "composer.json": "php"}
FRAMEWORKS = {
    "node": ("express", "fastify", "koa", "@nestjs/core", "next", "@hapi/hapi", "graphql", "@apollo/server",
             "axios", "pg", "mysql2", "mongodb", "redis", "ioredis", "kafkajs", "amqplib", "@prisma/client"),
    "python": ("flask", "django", "fastapi", "starlette", "aiohttp", "tornado", "falcon", "pyramid", "celery",
               "requests", "httpx", "urllib3", "sqlalchemy", "psycopg", "psycopg2", "pymongo", "redis", "pika",
               "kafka-python", "grpcio"),
}
INSTRUMENTED = {"node": ("@opentelemetry/sdk-node", "@opentelemetry/auto-instrumentations-node",
                         "@opentelemetry/sdk-trace-node"),
                "python": ("opentelemetry-sdk", "opentelemetry-distro")}
NEEDS = {
    "java": "attach the OpenTelemetry Java agent (-javaagent:opentelemetry-javaagent.jar); not automated here yet",
    "go": "Go has no zero-code agent; add the OpenTelemetry Go SDK and otelhttp to the code",
    "rust": "add the opentelemetry and tracing-opentelemetry crates to the code; not automated here yet",
    "dotnet": "use OpenTelemetry .NET automatic instrumentation; not automated here yet",
    "ruby": "add opentelemetry-sdk and opentelemetry-instrumentation-all to the Gemfile; not automated here yet",
    "php": "use OpenTelemetry PHP auto-instrumentation (the opentelemetry extension); not automated here yet",
}
NODE_COMMANDS = {"node", "nodejs", "npm", "npx", "yarn", "pnpm", "next", "nest", "nodemon", "ts-node", "tsx"}
PYTHON_COMMANDS = {"uvicorn", "gunicorn", "flask", "django-admin", "celery", "hypercorn", "daphne", "waitress-serve",
                   "streamlit", "granian"}
PYTHON_NAME = re.compile(r"python(\d+(\.\d+)?)?(\.exe)?")


class SetupError(ValueError):
    pass


def check(ok: bool, message: str) -> None:
    if not ok:
        raise SetupError(message)


def cache_root() -> Path:
    return Path(os.environ.get("ONTOLOGYEX_OTEL_CACHE") or Path.home() / ".cache" / "ontologyex" / "otel")


# ---------------------------------------------------------------- propose

def _manifests(repo: Path) -> list[str]:
    found, seen = [], 0
    for root, dirs, files in os.walk(repo):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(files):
            seen += 1
            check(seen <= MAX_FILES, f"more than {MAX_FILES} files; point --repo at a smaller folder")
            if name in LANGUAGES or name.endswith(".csproj"):
                path = Path(root) / name
                if not path.is_symlink() and path.stat().st_size <= MAX_MANIFEST:
                    found.append(path.relative_to(repo).as_posix())
    return found


def _line(text: str, needle: str) -> tuple[int, str]:
    for number, line in enumerate(text.splitlines(), 1):
        if needle in line:
            return number, line.strip()[:200]
    return 1, text.splitlines()[0].strip()[:200] if text.strip() else ""


def _normal(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _dependencies(rel: str, text: str) -> tuple[str | None, set[str], dict]:
    """(declared name, dependency names, package.json scripts) of one manifest."""
    name = Path(rel).name
    if name == "package.json":
        data = json.loads(text)
        check(isinstance(data, dict), f"{rel} is not a JSON object")
        deps = set()
        for field in ("dependencies", "devDependencies", "optionalDependencies"):
            if isinstance(data.get(field), dict):
                deps |= set(data[field])
        scripts = data["scripts"] if isinstance(data.get("scripts"), dict) else {}
        return (data.get("name") if isinstance(data.get("name"), str) else None), deps, \
            {**scripts, "__main__": data.get("main") if isinstance(data.get("main"), str) else None}
    if name == "pyproject.toml":
        data = tomllib.loads(text)
        project, poetry = data.get("project", {}), data.get("tool", {}).get("poetry", {})
        specs = list(project.get("dependencies", []) if isinstance(project.get("dependencies"), list) else [])
        specs += list(poetry.get("dependencies", {}) if isinstance(poetry.get("dependencies"), dict) else [])
        deps = {_normal(re.split(r"[\s<>=!~;\[(]", str(s), maxsplit=1)[0]) for s in specs if str(s).strip()}
        declared = project.get("name") or poetry.get("name")
        return (declared if isinstance(declared, str) else None), deps, {}
    if name == "Cargo.toml":
        package = tomllib.loads(text).get("package", {})
        return (package.get("name") if isinstance(package, dict) and isinstance(package.get("name"), str) else None), \
            set(), {}
    if name == "requirements.txt":
        deps = {_normal(re.split(r"[\s<>=!~;\[@]", line.strip(), maxsplit=1)[0]) for line in text.splitlines()
                if line.strip() and not line.strip().startswith(("#", "-"))}
        return None, deps, {}
    return None, set(), {}


def _service_name(text: str | None, folder: str, taken: set[str]) -> str:
    candidates = [text] if text else []
    candidates.append(Path(folder).name if folder != "." else "app")
    for raw in candidates:
        name = re.sub(r"[^A-Za-z0-9_.:/@-]+", "-", raw).strip("-")[:200]
        if name and feeds.SERVICE.fullmatch(name):
            break
    else:
        name = "app"
    unique, n = name, 2
    while unique in taken:
        unique, n = f"{name}-{n}", n + 1
    taken.add(unique)
    return unique


def load_compose(data: bytes):
    """A compose file, anchors and `<<` merges allowed (compose files lean on them).

    Discovery refuses anchors outright; here they are allowed, with tags still refused
    and the expanded document bounded in size and nesting, so an alias bomb or a
    self-referencing anchor stops early instead of growing.
    """
    for i, token in enumerate(yaml.scan(data)):
        if i >= discover_repo.MAX_YAML_TOKENS or isinstance(token, yaml.tokens.TagToken):
            raise SetupError("compose YAML exceeds the token limit or contains tags")
    budget = [discover_repo.MAX_YAML_TOKENS]

    def walk(node, depth):
        budget[0] -= 1
        if budget[0] < 0 or depth > discover_repo.MAX_YAML_NESTING:
            raise SetupError("compose YAML expands beyond the size or nesting limit")
        if isinstance(node, yaml.SequenceNode):
            for child in node.value:
                walk(child, depth + 1)
        elif isinstance(node, yaml.MappingNode):
            for key, value in node.value:
                walk(key, depth + 1)
                walk(value, depth + 1)

    root = yaml.compose(data, Loader=yaml.SafeLoader)
    if root is not None:
        walk(root, 0)
    return yaml.load(data, Loader=yaml.SafeLoader)


def _compose_builds(repo: Path, discovery: dict) -> dict[str, tuple[str, str]]:
    """app folder -> (compose service name, its component ID), from `build:` contexts."""
    builds = {}
    for evidence in discovery["source_evidence"]:
        rel = evidence["path"]
        if Path(rel).name not in discover_repo.COMPOSE_FILES:
            continue
        try:
            doc = load_compose((repo / rel).read_bytes())
        except (ValueError, OSError, yaml.YAMLError):
            continue
        services = doc.get("services") if isinstance(doc, dict) else None
        for name, spec in (services.items() if isinstance(services, dict) else []):
            build = spec.get("build") if isinstance(spec, dict) else None
            context = build.get("context", ".") if isinstance(build, dict) else build
            if type(context) is not str or "://" in context:
                continue
            folder = os.path.normpath((Path(rel).parent / context).as_posix())
            builds.setdefault(Path(folder).as_posix(), (str(name), discover_repo.canonical_id("service", rel, str(name))))
    return builds


def _start_command(language: str, folder: Path, scripts: dict) -> str | None:
    if language == "node":
        if "start" in scripts:
            return "npm start"
        if "dev" in scripts:
            return "npm run dev"
        main = scripts.get("__main__")
        for entry in ([main] if main else []) + ["server.js", "index.js", "app.js", "main.js"]:
            if (folder / entry).is_file():
                return f"node {entry}"
    if language == "python":
        if (folder / "manage.py").is_file():
            return "python manage.py runserver"
        for entry in ("app.py", "main.py", "server.py", "wsgi.py"):
            if (folder / entry).is_file():
                return f"python {entry}"
    return None


def find_apps(repo: Path, discovery: dict | None = None) -> list[dict]:
    """Every app in the repository, with the evidence for it and how to trace it."""
    repo = repo.resolve()
    check(repo.is_dir(), f"{repo} is not a directory")
    discovery = discovery if discovery is not None else discover_repo.discover(repo)
    known = {n["id"] for n in discovery["nodes"]}
    builds = _compose_builds(repo, discovery)
    by_folder: dict[str, dict] = {}
    for rel in _manifests(repo):
        name = Path(rel).name
        language = "dotnet" if name.endswith(".csproj") else LANGUAGES[name]
        folder = Path(rel).parent.as_posix()
        app = by_folder.setdefault(f"{folder}\0{language}", {"folder": folder, "language": language, "manifests": []})
        app["manifests"].append(rel)
    apps, taken = [], set()
    for key in sorted(by_folder):
        app = by_folder[key]
        folder, language = app["folder"], app["language"]
        declared, deps, scripts, texts = None, set(), {}, {}
        for rel in app["manifests"]:
            text = (repo / rel).read_text("utf-8", "replace")
            texts[rel] = text
            try:
                name, found, more = _dependencies(rel, text)
            except (ValueError, tomllib.TOMLDecodeError):
                continue  # a manifest discovery already reports as unreadable
            declared, deps, scripts = declared or name, deps | found, {**scripts, **more}
        first = app["manifests"][0]
        compose = builds.get(folder)
        service = _service_name(compose[0] if compose else declared, folder, taken)
        component = compose[1] if compose else discover_repo.canonical_id("manifest", first)
        frameworks = []
        for library in FRAMEWORKS.get(language, ()):
            hit = next((rel for rel in app["manifests"] if library in deps or _normal(library) in deps), None)
            if hit:
                number, text = _line(texts[hit], library)
                frameworks.append({"name": library, "evidence": {"path": hit, "line": number, "text": text}})
        instrumented = [lib for lib in INSTRUMENTED.get(language, ()) if lib in deps or _normal(lib) in deps]
        number, text = _line(texts[first], declared or "") if declared else (1, texts[first].splitlines()[0][:200]
                                                                               if texts[first].strip() else "")
        start = _start_command(language, repo / folder, scripts)
        entry = {"id": f"otel:{folder}:{language}", "folder": folder, "language": language, "service": service,
                 "component": component if component in known else None,
                 "component_source": "compose service" if compose else "manifest",
                 "evidence": {"path": first, "line": number, "text": text},
                 "frameworks": frameworks}
        if language not in ("node", "python"):
            entry.update(status="NOT_AUTOMATED", needs=NEEDS[language])
        elif instrumented:
            entry.update(status="ALREADY_INSTRUMENTED",
                         needs=f"it already depends on {', '.join(instrumented)}; point its exporter at the feeds "
                               f"with `otel_setup.py env --service {service} --lang {language}`")
        else:
            command = start or "<your start command>"
            entry.update(status="READY", start=start,
                         command=f"cd {shlex.quote(folder)} && python {shlex.quote(str(HERE / 'otel_setup.py'))} "
                                 f"run --service {shlex.quote(service)} -- {command}")
        apps.append(entry)
    return apps


def propose(repo: Path) -> dict:
    apps = find_apps(repo)
    mapped = {a["service"]: a["component"] for a in apps if a["status"] != "NOT_AUTOMATED" and a["component"]}
    return {"schema_version": 1, "kind": "OTEL_SETUP_PROPOSAL", "repo": str(repo.resolve()), "apps": apps,
            "otlp_services": mapped,
            "next": "Start each READY app with its command (add --feeds feeds.json if the feeds listen elsewhere "
                    "than 127.0.0.1:4318), and map its name in feeds.json: feed_setup.py propose lists these.",
            "semantics": "ZERO_CODE_INSTRUMENTATION_CHANGES_NO_FILES_A_PERSON_STARTS_EACH_SERVICE"}


# ---------------------------------------------------------------- run

def detect_language(command: list[str]) -> str:
    first = Path(command[0]).name
    if first in NODE_COMMANDS:
        return "node"
    if PYTHON_NAME.fullmatch(first) or first in PYTHON_COMMANDS:
        return "python"
    shebang = _shebang(command[0])
    if shebang and "node" in shebang:
        return "node"
    if shebang and "python" in shebang:
        return "python"
    raise SetupError(f"cannot tell what language {command[0]!r} runs; pass --lang node or --lang python")


def _shebang(program: str) -> str | None:
    path = shutil.which(program)
    if not path:
        return None
    try:
        with open(path, "rb") as handle:
            first = handle.readline(512)
    except OSError:
        return None
    return first[2:].decode("utf-8", "replace").strip() if first.startswith(b"#!") else None


def python_for(command: list[str], override: str | None) -> str:
    """The interpreter that will run the app, so the instrumentation matches it."""
    if override:
        path = shutil.which(override)
        check(path is not None, f"no Python at {override}")
        return path
    first = Path(command[0]).name
    check(first not in ("poetry", "uv", "pipenv", "pdm", "hatch", "rye"),
          f"{first} picks the interpreter itself; pass --python with the one it uses "
          f"(for example: --python \"$({first} run which python)\")")
    if PYTHON_NAME.fullmatch(first):
        path = shutil.which(command[0])
        check(path is not None, f"{command[0]} is not on PATH")
        return path
    shebang = _shebang(command[0])
    check(shebang is not None and "python" in shebang,
          f"cannot tell which Python runs {command[0]!r}; pass --python PATH")
    parts = shebang.split()
    interpreter = parts[1] if Path(parts[0]).name == "env" and len(parts) > 1 else parts[0]
    path = shutil.which(interpreter)
    check(path is not None, f"{interpreter} (from {command[0]}'s first line) is not on PATH; pass --python PATH")
    return path


def endpoint_from(feeds_config: Path | None, endpoint: str | None) -> tuple[str, str]:
    """(OTLP base URL, OTEL_EXPORTER_OTLP_HEADERS value)."""
    headers = ""
    if endpoint is None and feeds_config is not None:
        otlp = feeds.load_config(feeds_config).get("otlp")
        check(otlp is not None, f"{feeds_config} has no otlp section; add one with feed_setup.py")
        host = otlp.get("bind", "127.0.0.1")
        host = "127.0.0.1" if host in ("0.0.0.0", "") else "[::1]" if host == "::" else \
            f"[{host}]" if ":" in host else host
        endpoint = f"http://{host}:{otlp.get('port', 4318)}"
        if otlp.get("token_env"):
            token = os.environ.get(otlp["token_env"], "")
            check(token, f"the feeds expect a token in ${otlp['token_env']}, which is empty here")
            headers = "Authorization=Bearer%20" + token
    endpoint = endpoint or "http://127.0.0.1:4318"
    check(re.fullmatch(r"https?://[^\s/]+(/\S*)?", endpoint) is not None, f"not an http(s) URL: {endpoint}")
    return endpoint.rstrip("/"), headers


def otel_env(service: str, endpoint: str, headers: str) -> dict[str, str]:
    check(feeds.SERVICE.fullmatch(service) is not None, f"service name {service!r} must match {feeds.SERVICE.pattern}")
    env = {"OTEL_SERVICE_NAME": service, "OTEL_EXPORTER_OTLP_ENDPOINT": endpoint,
           "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf", "OTEL_TRACES_EXPORTER": "otlp",
           "OTEL_METRICS_EXPORTER": "none", "OTEL_LOGS_EXPORTER": "none",
           "OTEL_PROPAGATORS": "tracecontext,baggage",
           # Cloud metadata lookups hold every export until they time out on a laptop.
           "OTEL_NODE_RESOURCE_DETECTORS": "env,host,os,process"}
    if headers:
        env["OTEL_EXPORTER_OTLP_HEADERS"] = headers
    return env


def _quiet(command: list[str], cwd: Path | None = None, env: dict | None = None) -> str:
    run = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=900)
    check(run.returncode == 0, f"{' '.join(command[:4])} failed: {(run.stderr or run.stdout).strip()[-600:]}")
    return run.stdout


def _fresh(final: Path, build) -> Path:
    """Build into a temporary sibling, then move it into place (a concurrent first run is harmless)."""
    if final.exists():
        return final
    final.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{final.name}-", dir=final.parent))
    try:
        build(temporary)
        try:
            temporary.rename(final)
        except OSError:
            if not final.exists():
                raise
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
    return final


def ensure_node() -> Path:
    """The installed register hook, from the pinned lockfile."""
    lock = (NODE_PACKAGE / "package-lock.json").read_bytes()
    final = cache_root() / "node" / hashlib.sha256(lock).hexdigest()[:16]

    def build(folder: Path) -> None:
        npm = shutil.which("npm")
        check(npm is not None, "npm is needed once, to install the instrumentation; it is not on PATH")
        shutil.copy2(NODE_PACKAGE / "package.json", folder / "package.json")
        shutil.copy2(NODE_PACKAGE / "package-lock.json", folder / "package-lock.json")
        print("OntologyEX: installing @opentelemetry/auto-instrumentations-node (once) with npm ci",
              file=sys.stderr, flush=True)
        _quiet([npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund", "--omit=dev"], cwd=folder)

    folder = _fresh(final, build)
    register = folder / "node_modules" / "@opentelemetry" / "auto-instrumentations-node" / "dist" / "register.cjs"
    check(register.is_file(), f"{register} is missing; delete {folder} and run again")
    return register


def _pip(interpreter: str, target: Path, packages: list[str]) -> None:
    base = ([interpreter, "-m", "pip"] if subprocess.run([interpreter, "-m", "pip", "--version"],
                                                          capture_output=True).returncode == 0
            else [sys.executable, "-m", "pip", "--python", interpreter])
    _quiet([*base, "install", "--quiet", "--disable-pip-version-check", "--no-input", "--target", str(target),
            *packages])


HOOK = '''# Written by OntologyEX otel_setup.py: load OpenTelemetry for this interpreter.
import signal
import sys

sys.path.append({target!r})  # after the application's own packages, so its versions win
try:
    from opentelemetry.instrumentation.auto_instrumentation import initialize
    initialize()
except Exception as exc:  # tracing must never stop the application
    print(f"OntologyEX: tracing not started ({{exc}})", file=sys.stderr)
if signal.getsignal(signal.SIGTERM) is signal.SIG_DFL:
    # Exit through Python's shutdown on SIGTERM, so the last spans are sent.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
'''


def ensure_python(interpreter: str) -> Path:
    """A folder whose sitecustomize loads OpenTelemetry for this interpreter's libraries."""
    tag = _quiet([interpreter, "-c", "import sys, sysconfig; print(sys.implementation.cache_tag, "
                                     "sysconfig.get_platform())"]).split()
    check(len(tag) == 2, f"cannot read {interpreter}'s version")
    home = cache_root() / "python" / f"{tag[0]}-{tag[1]}"

    def build_base(folder: Path) -> None:
        print(f"OntologyEX: installing opentelemetry-distro for {tag[0]} (once) with pip", file=sys.stderr, flush=True)
        _pip(interpreter, folder, list(PYTHON_PACKAGES))

    base = _fresh(home / "base", build_base)
    # Which instrumentations fit the libraries this interpreter has (Flask, requests, ...).
    wanted = sorted(set(_quiet([interpreter, "-c", "import sys; sys.argv = ['bootstrap', '-a', 'requirements']; "
                                "from opentelemetry.instrumentation.bootstrap import run; run()"],
                               env={**os.environ, "PYTHONPATH": str(base)}).split()))
    key = hashlib.sha256("\n".join([*PYTHON_PACKAGES, *wanted]).encode()).hexdigest()[:16]

    def build(folder: Path) -> None:
        print(f"OntologyEX: installing {len(wanted)} instrumentations for this app (once)", file=sys.stderr, flush=True)
        # One pip run: --target does not merge into folders an earlier run created.
        _pip(interpreter, folder / "packages", [*PYTHON_PACKAGES, *wanted])
        (folder / "hook").mkdir()
        (folder / "hook" / "sitecustomize.py").write_text(HOOK.format(target=str(final / "packages")), "utf-8")

    final = home / key
    return _fresh(final, build) / "hook"


def prepare(args, command: list[str] | None) -> tuple[dict[str, str], str]:
    language = args.lang or detect_language(command)
    endpoint, headers = endpoint_from(args.feeds, args.endpoint)
    env = otel_env(args.service, endpoint, headers)
    if language == "node":
        register = ensure_node()
        env["NODE_OPTIONS"] = " ".join(x for x in (os.environ.get("NODE_OPTIONS", ""),
                                                    f'--require "{register}" --require "{NODE_EXIT}"') if x)
    else:
        interpreter = python_for(command or ["python3"], args.python)
        hook = ensure_python(interpreter)
        env["PYTHONPATH"] = os.pathsep.join(x for x in (str(hook), os.environ.get("PYTHONPATH", "")) if x)
    return env, language


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("propose", help="list the apps and how to trace each (writes nothing)")
    p.add_argument("--repo", type=Path, required=True)
    for name in ("run", "env"):
        c = commands.add_parser(name, help="start a command with tracing" if name == "run" else
                                "print the environment that run sets")
        c.add_argument("--service", required=True, help="the name its traces carry (see propose)")
        c.add_argument("--feeds", type=Path, help="send to the OTLP receiver this feeds config defines")
        c.add_argument("--endpoint", help="send to this OTLP/HTTP base URL instead")
        c.add_argument("--lang", choices=("node", "python"), help="default: from the command")
        c.add_argument("--python", help="the interpreter that runs the app (default: from the command)")
        if name == "run":
            c.add_argument("argv", nargs=argparse.REMAINDER, help="-- then the command that starts the service")
    args = parser.parse_args()
    try:
        if args.command == "propose":
            print(json.dumps(propose(args.repo), indent=2))
            return 0
        if args.command == "env":
            check(args.lang is not None, "env needs --lang node or --lang python")
            env, _ = prepare(args, None)
            print("\n".join(f"export {key}={shlex.quote(value)}" for key, value in env.items()))
            return 0
        argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
        check(argv, "pass the command that starts the service after --, for example: -- npm start")
        env, language = prepare(args, argv)
        print(f"OntologyEX: tracing {args.service} ({language}) to {env['OTEL_EXPORTER_OTLP_ENDPOINT']}",
              file=sys.stderr, flush=True)
        os.execvpe(argv[0], argv, {**os.environ, **env})
    except (SetupError, feeds.FeedError, discover_repo.DiscoveryError, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
